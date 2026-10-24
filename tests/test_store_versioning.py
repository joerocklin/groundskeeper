"""MowerStore versioning, backups and safe idle (spec §14.7, #31)."""

import json
import os
from pathlib import Path

import pytest

from homeassistant.helpers import issue_registry as ir

from custom_components.groundskeeper import store as store_mod
from custom_components.groundskeeper.store import MowerStore, empty_store_data

KEY = "groundskeeper.e1"


@pytest.fixture(autouse=True)
def clean_storage_dir(hass):
    """The test config dir is shared: leave no real files behind."""
    folder = Path(hass.config.path(".storage"))

    def wipe():
        for f in folder.glob(f"{KEY}*"):
            f.unlink()

    wipe()
    yield
    wipe()


def raw(version, minor, data):
    return {"version": version, "minor_version": minor, "key": KEY, "data": data}


async def test_same_major_older_minor_passes_through_unchanged(hass, hass_storage) -> None:
    data = empty_store_data()
    data["config"]["zones"] = [{"id": "z1"}]
    hass_storage[KEY] = raw(1, 0, data)
    store = MowerStore(hass, "e1")
    loaded = await store.async_data()
    assert loaded["config"]["zones"] == [{"id": "z1"}]
    assert store.safe_idle is None


async def test_same_major_newer_minor_passes_through(hass, hass_storage) -> None:
    hass_storage[KEY] = raw(1, 99, empty_store_data())
    store = MowerStore(hass, "e1")
    await store.async_data()
    assert store.safe_idle is None


async def test_major_bump_runs_the_chained_migrations_in_order(
    hass, hass_storage, monkeypatch
) -> None:
    monkeypatch.setattr(store_mod, "STORAGE_VERSION", 3)
    monkeypatch.setattr(
        store_mod,
        "MIGRATIONS",
        {
            1: lambda d: {**d, "trail": ["1to2"]},
            2: lambda d: {**d, "trail": [*d["trail"], "2to3"]},
        },
    )
    hass_storage[KEY] = raw(1, 0, empty_store_data())
    store = MowerStore(hass, "e1")
    loaded = await store.async_data()
    assert loaded["trail"] == ["1to2", "2to3"]
    assert hass_storage[KEY]["version"] == 3


async def test_major_bump_copies_the_raw_file_to_a_versioned_bak_first(
    hass, hass_storage, monkeypatch
) -> None:
    path = hass.config.path(".storage", KEY)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    hass_storage[KEY] = raw(1, 0, empty_store_data())
    original = json.dumps(hass_storage[KEY])
    with open(path, "w") as fh:
        fh.write(original)
    monkeypatch.setattr(store_mod, "STORAGE_VERSION", 2)
    monkeypatch.setattr(store_mod, "MIGRATIONS", {1: lambda d: d})
    await MowerStore(hass, "e1").async_data()
    with open(f"{path}.v1.bak") as fh:
        assert fh.read() == original


async def test_a_store_from_a_newer_major_is_safe_idle_and_never_written(
    hass, hass_storage
) -> None:
    hass_storage[KEY] = raw(2, 0, {"future": True})
    store = MowerStore(hass, "e1")
    data = await store.async_data()
    assert store.safe_idle == "newer_version"
    assert data == empty_store_data()
    await store.async_save(data)
    store.async_delay_save_shared(0)
    assert hass_storage[KEY]["data"] == {"future": True}
    assert hass_storage[KEY]["version"] == 2


async def test_invalid_config_is_safe_idle_and_never_written(hass, hass_storage) -> None:
    bad = empty_store_data()
    bad["config"]["zones"] = "nope"
    hass_storage[KEY] = raw(1, 0, bad)
    store = MowerStore(hass, "e1")
    await store.async_data()
    assert store.safe_idle == "invalid_config"
    await store.async_save(empty_store_data())
    assert hass_storage[KEY]["data"]["config"]["zones"] == "nope"


async def test_a_corrupt_file_returned_as_none_is_safe_idle_not_a_fresh_install(
    hass, hass_storage
) -> None:
    ir.async_create_issue(
        hass,
        "homeassistant",
        f"storage_corruption_{KEY}_2026-01-01T00:00:00",
        is_fixable=True,
        severity=ir.IssueSeverity.CRITICAL,
        translation_key="storage_corruption",
    )
    store = MowerStore(hass, "e1")
    await store.async_data()
    assert store.safe_idle == "corrupt"
    assert KEY not in hass_storage  # nothing seeded over the user's data


async def test_a_genuinely_new_install_is_seeded(hass, hass_storage) -> None:
    store = MowerStore(hass, "e1")
    assert await store.async_data() == empty_store_data()
    assert store.safe_idle is None
    assert hass_storage[KEY]["data"] == empty_store_data()


async def test_only_state_invalid_keeps_config_empties_state_and_backs_up_raw_file(
    hass, hass_storage
) -> None:
    path = hass.config.path(".storage", KEY)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bad = empty_store_data()
    bad["config"]["zones"] = [{"id": "z1"}]
    bad["state"] = {"runs": "garbage"}
    hass_storage[KEY] = raw(1, 0, bad)
    original = json.dumps(hass_storage[KEY])
    with open(path, "w") as fh:
        fh.write(original)
    store = MowerStore(hass, "e1")
    data = await store.async_data()
    assert store.safe_idle is None and store.state_discarded
    assert data["state"] == {} and data["config"]["zones"] == [{"id": "z1"}]
    backups = [f for f in os.listdir(os.path.dirname(path)) if f.startswith(KEY) and f.endswith(".bak")]
    assert len(backups) == 1
    with open(os.path.join(os.path.dirname(path), backups[0])) as fh:
        assert fh.read() == original


async def test_start_fresh_unlocks_and_writes_an_empty_store(hass, hass_storage) -> None:
    hass_storage[KEY] = raw(2, 0, {"future": True})
    store = MowerStore(hass, "e1")
    await store.async_data()
    await store.async_start_fresh()
    assert store.safe_idle is None
    assert hass_storage[KEY]["data"] == empty_store_data()
    assert hass_storage[KEY]["version"] == 1


async def test_bad_history_record_is_dropped_on_load(hass, hass_storage, caplog) -> None:
    d = empty_store_data()
    d["history"] = {"runs": [{"id": "x"}], "plans": {}}
    hass_storage[KEY] = raw(1, 0, d)
    store = MowerStore(hass, "e1")
    assert (await store.async_data())["history"]["runs"] == []
    assert store.safe_idle is None
    assert "Dropped" in caplog.text
