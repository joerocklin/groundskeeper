"""Where the debug recording flag lives and how it is read (#42, ADR 0001)."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_DEVICE
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.groundskeeper import adapters
from custom_components.groundskeeper.const import DOMAIN
from custom_components.groundskeeper.recording import async_get_recording, recording_enabled
from custom_components.groundskeeper.runtime import async_get_director
from custom_components.groundskeeper.store import (
    STORAGE_VERSION,
    STORAGE_VERSION_MINOR,
    MowerStore,
    empty_store_data,
)
from tests.conftest import make_mammotion_mower
from tests.fake_mower import FakeMower
from tests.test_recording_ha import of_kind, read
from tests.test_tracer_bullet import MOWING, director  # noqa: F401


def fake_entry(**options):
    return SimpleNamespace(options=options)


@pytest.mark.parametrize(
    ("options", "config", "expected"),
    [
        ({}, {}, False),  # an entry from before the feature reads as off
        ({"debug_recording": True}, {}, True),  # an existing entry keeps what it had
        ({"debug_recording": False}, {}, False),
        ({"debug_recording": True}, {"debug_recording": False}, False),  # the Store wins
        ({"debug_recording": False}, {"debug_recording": True}, True),
        ({}, {"debug_recording": True}, True),
    ],
)
def test_the_store_flag_wins_and_entry_options_are_the_fallback(options, config, expected) -> None:
    assert recording_enabled(fake_entry(**options), config) is expected


async def load_mower(hass, hass_storage, monkeypatch, *, options, config=None):
    """Set up a Mower entry with `options`, its Store already holding `config` if given."""
    monkeypatch.setitem(adapters.ADAPTER_REGISTRY, "mammotion", lambda h, e: FakeMower())
    _, device, _ = await make_mammotion_mower(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Example Mower", unique_id=device.id,
        data={CONF_DEVICE: device.id}, options=options,
    )
    if config is not None:
        key = f"groundskeeper.{entry.entry_id}"
        data = empty_store_data()
        data["config"].update(config)
        hass_storage[key] = {
            "version": STORAGE_VERSION, "minor_version": STORAGE_VERSION_MINOR,
            "key": key, "data": data,
        }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def db_exists(hass, entry) -> bool:
    return Path(hass.config.path("groundskeeper", f"recording_{entry.entry_id}.db")).exists()


async def test_an_existing_entry_with_no_store_flag_keeps_its_option(
    hass, hass_storage, monkeypatch
) -> None:
    entry = await load_mower(hass, hass_storage, monkeypatch,
                             options={"debug_recording": True}, config={})
    assert async_get_recording(hass, entry.entry_id) is not None


async def test_the_store_flag_turns_recording_off_whatever_the_entry_option_says(
    hass, hass_storage, monkeypatch
) -> None:
    entry = await load_mower(hass, hass_storage, monkeypatch,
                             options={"debug_recording": True}, config={"debug_recording": False})
    assert async_get_recording(hass, entry.entry_id) is None
    assert not db_exists(hass, entry)


async def test_the_store_flag_turns_recording_on_whatever_the_entry_option_says(
    hass, hass_storage, monkeypatch
) -> None:
    entry = await load_mower(hass, hass_storage, monkeypatch,
                             options={"debug_recording": False}, config={"debug_recording": True})
    assert async_get_recording(hass, entry.entry_id) is not None


async def test_a_new_mower_records_by_default_and_says_so_in_its_store(
    hass, hass_storage, monkeypatch
) -> None:
    entry = await load_mower(hass, hass_storage, monkeypatch, options={})
    assert hass_storage[f"groundskeeper.{entry.entry_id}"]["data"]["config"]["debug_recording"] is True
    assert async_get_recording(hass, entry.entry_id) is not None


async def test_a_new_store_follows_an_explicit_entry_option(
    hass, hass_storage, monkeypatch
) -> None:
    entry = await load_mower(hass, hass_storage, monkeypatch, options={"debug_recording": False})
    assert hass_storage[f"groundskeeper.{entry.entry_id}"]["data"]["config"]["debug_recording"] is False
    assert async_get_recording(hass, entry.entry_id) is None


async def test_only_a_brand_new_store_file_is_seeded(hass, hass_storage) -> None:
    seed = {"debug_recording": True}
    fresh = await MowerStore(hass, "seed1", new_config=seed).async_data()
    assert fresh["config"]["debug_recording"] is True
    assert hass_storage["groundskeeper.seed1"]["data"]["config"]["debug_recording"] is True

    key = "groundskeeper.seed2"
    existing = empty_store_data()
    hass_storage[key] = {"version": STORAGE_VERSION, "minor_version": STORAGE_VERSION_MINOR,
                         "key": key, "data": existing}
    loaded = await MowerStore(hass, "seed2", new_config=seed).async_data()
    assert "debug_recording" not in loaded["config"]  # an old Mower keeps reading its entry option

    key = "groundskeeper.seed3"
    hass_storage[key] = {"version": STORAGE_VERSION + 1, "minor_version": 0,
                         "key": key, "data": empty_store_data()}
    store = MowerStore(hass, "seed3", new_config=seed)
    assert "debug_recording" not in (await store.async_data())["config"]  # safe idle is not a new Mower
    assert store.safe_idle


async def test_recovering_a_store_keeps_a_new_mower_recording(hass, hass_storage) -> None:
    key = "groundskeeper.seed4"
    hass_storage[key] = {"version": STORAGE_VERSION + 1, "minor_version": 0,
                         "key": key, "data": empty_store_data()}
    store = MowerStore(hass, "seed4", new_config={"debug_recording": True})
    await store.async_data()
    assert store.safe_idle

    await store.async_start_fresh()  # the owner chose to start over
    assert (await store.async_data())["config"]["debug_recording"] is True

    await store.async_restore({"zones": []}, None)  # or to restore an export, which has no flag
    config = (await store.async_data())["config"]
    assert config["debug_recording"] is True and config["zones"] == []

    await store.async_restore({"debug_recording": False}, None)  # a choice already in it stands
    assert (await store.async_data())["config"]["debug_recording"] is False


def stored_flag(hass_storage, entry):
    return hass_storage[f"groundskeeper.{entry.entry_id}"]["data"]["config"].get("debug_recording")


async def test_turning_recording_off_stops_it_on_the_live_director_and_keeps_the_file(
    hass, hass_storage, director
) -> None:
    entry, _, _ = director
    live = async_get_director(hass, entry.entry_id)
    assert async_get_recording(hass, entry.entry_id) is not None

    await live.async_set_recording(False)

    assert live.recording is None and async_get_recording(hass, entry.entry_id) is None
    assert db_exists(hass, entry)  # stopping never deletes what was recorded
    assert stored_flag(hass_storage, entry) is False
    assert entry.options == {}  # an options write would have reloaded the entry
    assert entry.state is ConfigEntryState.LOADED
    assert async_get_director(hass, entry.entry_id) is live  # same Director: no reload


async def test_turning_recording_back_on_records_again_into_the_same_file(
    hass, hass_storage, director
) -> None:
    entry, mower, device = director
    live = async_get_director(hass, entry.entry_id)
    await live.async_set_recording(False)
    mower.emit_snapshot(MOWING)  # while off: not recorded
    await hass.async_block_till_done()

    await live.async_set_recording(True)
    assert stored_flag(hass_storage, entry) is True
    mower.emit_snapshot(mower.snapshot)  # the first snapshot after turning on is recorded
    [mower_entity] = er.async_entries_for_device(er.async_get(hass), device.id)
    hass.states.async_set(mower_entity.entity_id, "mowing", {"battery": 50})
    await hass.async_block_till_done()

    rows = await read(hass, entry)
    events = [p["event"] for p in of_kind(rows, "lifecycle")]
    assert events.index("recording_disabled") < events.index("recording_enabled")
    assert len(of_kind(rows, "snapshot")) >= 2  # before it was turned off, and again after
    assert [r["entity_id"] for r in of_kind(rows, "raw_state")] == [mower_entity.entity_id]


async def test_toggles_in_a_row_leave_the_recording_matching_the_stored_flag(
    hass, hass_storage, director
) -> None:
    entry, _, _ = director
    live = async_get_director(hass, entry.entry_id)
    await asyncio.gather(
        live.async_set_recording(False), live.async_set_recording(True),
        live.async_set_recording(False), live.async_set_recording(True),
    )
    assert (live.recording is not None) is stored_flag(hass_storage, entry)
    assert (async_get_recording(hass, entry.entry_id) is not None) is stored_flag(hass_storage, entry)


async def test_a_toggle_that_lands_after_unload_starts_no_recording(hass, director) -> None:
    entry, _, _ = director
    live = async_get_director(hass, entry.entry_id)
    await live.async_set_recording(False)
    live.async_unload()  # the entry is going away; nothing would stop a writer started now
    await live.async_set_recording(True)
    assert live.recording is None and async_get_recording(hass, entry.entry_id) is None


async def test_a_toggle_caught_mid_start_by_an_unload_leaves_no_writer_running(
    hass, director, monkeypatch
) -> None:
    from custom_components.groundskeeper.recording import DebugRecording

    entry, _, _ = director
    live = async_get_director(hass, entry.entry_id)
    await live.async_set_recording(False)
    started = []
    real_start = DebugRecording.async_start

    async def unload_while_starting(self):
        live.async_unload()  # the entry unloads just as the recording is opening
        await real_start(self)
        started.append(self)

    monkeypatch.setattr(DebugRecording, "async_start", unload_while_starting)
    await live.async_set_recording(True)

    [recording] = started
    assert recording.writer is None  # opened, then closed again: nothing left to leak
    assert async_get_recording(hass, entry.entry_id) is None
    assert live.recording is None


async def test_a_wholesale_config_swap_resyncs_the_recording(hass, director) -> None:
    entry, _, _ = director
    live = async_get_director(hass, entry.entry_id)
    live.data["config"]["debug_recording"] = False  # as an undo would restore it
    await live.config_replaced()
    assert async_get_recording(hass, entry.entry_id) is None
    live.data["config"]["debug_recording"] = True
    await live.config_replaced()
    assert async_get_recording(hass, entry.entry_id) is not None
