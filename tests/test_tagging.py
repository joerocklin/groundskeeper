"""Tags, signals, source version and job settings in the debug recording (#44).

Stage 1 watches while the owner mows from the Mammotion app, so the recording
has to carry what telemetry cannot say. Everything here only writes to the
recording: no test may see a Mower command.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.groundskeeper.adapter import NewError, ZoneTaskComplete
from custom_components.groundskeeper.const import DOMAIN
from custom_components.groundskeeper.runtime import async_get_director
from tests.test_recording_ha import of_kind, read
from tests.test_tracer_bullet import MOWING, advance, director  # noqa: F401


async def add_tag(hass, **data):
    return await hass.services.async_call(
        DOMAIN, "add_tag", data, blocking=True, return_response=True
    )


async def tag_rows(hass, entry):
    return await read(hass, entry, "select ts, payload from records where kind = 'tag' order by seq")


async def test_tag_is_recorded_with_labels_and_the_ha_view(hass, director, freezer) -> None:
    entry, mower, _ = director
    mower.emit_snapshot(replace(MOWING, progress=12, battery=61))
    await hass.async_block_till_done()

    reply = await add_tag(
        hass,
        label="job_start",
        zone="front lawn",
        height_mm=60,
        edge_laps=True,
        laps=2,
        note="pressed start in the app",
    )

    assert reply["recorded"] is True
    [(ts, raw)] = await tag_rows(hass, entry)
    tag = json.loads(raw)
    assert ts == int(dt_util.utcnow().timestamp() * 1000)
    assert tag["label"] == "job_start"
    assert tag["source"] == "service"
    assert tag["note"] == "pressed start in the app"
    assert tag["fields"] == {"zone": "front lawn", "height_mm": 60, "edge_laps": True, "laps": 2}
    # HA's own view at tap time, so "app says 40 %, HA says 12 %" is self-contained
    assert tag["ha"]["progress"] == 12
    assert tag["ha"]["battery"] == 61
    assert tag["ha"]["job_active"] is True
    assert tag["ha"]["activity"] == str(MOWING.activity)
    # a whitelist: no position, no entity ids, nothing beyond the named scalars
    assert set(tag["ha"]) == {
        "activity",
        "available",
        "job_active",
        "recharging",
        "battery",
        "progress",
        "blade_height_mm",
    }


async def test_tag_can_be_backdated(hass, director, freezer) -> None:
    entry, _, _ = director
    await add_tag(hass, label="edge_laps_begin", minutes_ago=20)
    [(ts, raw)] = await tag_rows(hass, entry)
    assert ts == int(dt_util.utcnow().timestamp() * 1000) - 20 * 60_000
    tag = json.loads(raw)
    assert tag["minutes_ago"] == 20
    assert "logged_at" in tag  # when it was entered, apart from when it happened


async def test_tagging_and_signals_never_command_the_mower(hass, director, freezer) -> None:
    entry, mower, _ = director
    await add_tag(hass, label="note", note="gate was open")
    mower.emit_signal(ZoneTaskComplete("11111111"))
    await hass.async_block_till_done()
    assert mower.commands == []


async def test_tag_reports_not_recorded_when_recording_is_off(hass, director) -> None:
    entry, _, _ = director
    await async_get_director(hass, entry.entry_id).async_set_recording(False)
    reply = await add_tag(hass, label="note", note="nobody is listening")
    assert reply["recorded"] is False


@pytest.mark.parametrize(
    "bad",
    [
        {"label": "not_a_label"},
        {"label": "note", "note": "x" * 201},
        {"label": "note", "minutes_ago": 1441},
        {"label": "note", "minutes_ago": -1},
        {"label": "progress_check", "app_progress_pct": 101},
        {"label": "job_start", "laps": 11},
        # Numbers that Coerce(int) or Coerce(float) alone let through or crash on.
        {"label": "note", "minutes_ago": float("inf")},
        {"label": "note", "minutes_ago": float("nan")},
        {"label": "note", "minutes_ago": True},
        {"label": "note", "minutes_ago": 2.5},
        {"label": "job_start", "laps": True},
        {"label": "job_start", "height_mm": True},
        {"label": "job_start", "height_mm": float("nan")},
        {"label": "progress_check", "app_progress_pct": float("inf")},
    ],
)
async def test_tag_input_is_validated(hass, director, bad) -> None:
    with pytest.raises(vol.Invalid):
        await add_tag(hass, **bad)


async def test_a_whole_number_sent_as_a_float_is_accepted(hass, director) -> None:
    reply = await add_tag(hass, label="job_start", laps=2.0, minutes_ago=5.0)
    assert reply["recorded"] is True


async def test_tag_calls_are_rate_limited(hass, director, freezer) -> None:
    for _ in range(30):
        await add_tag(hass, label="note")
    with pytest.raises(ServiceValidationError):
        await add_tag(hass, label="note")
    await advance(hass, freezer, 61)
    assert (await add_tag(hass, label="note"))["recorded"] is True


async def test_adapter_signals_are_recorded_as_inputs(hass, director, freezer) -> None:
    entry, mower, _ = director
    mower.emit_signal(ZoneTaskComplete("11111111"))
    mower.emit_signal(NewError("1003", "rain", dt_util.utcnow()))
    await hass.async_block_till_done()
    signals = of_kind(await read(hass, entry), "signal")
    assert signals[0] == {"type": "ZoneTaskComplete", "zone": "11111111"}
    assert signals[1]["type"] == "NewError"
    assert signals[1]["code"] == "1003"
    assert signals[1]["message"] == "rain"


async def test_recording_meta_carries_the_source_version(hass, director) -> None:
    entry, _, _ = director
    rows = await read(hass, entry, "select key, value from meta")
    assert dict(rows)["source_version"] == "0.6.18"


async def test_source_version_learned_at_adapter_start_reaches_the_meta(hass, director) -> None:
    """Mammotion resolves its version in `async_start`, after the recording opened."""
    entry, mower, _ = director
    mower.source_ver = None
    mower.source_ver_on_start = "0.6.19"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    rows = await read(hass, entry, "select key, value from meta")
    assert dict(rows)["source_version"] == "0.6.19"


async def test_job_settings_are_recorded_on_change(hass, director, freezer) -> None:
    entry, mower, _ = director
    mower.job_settings_value = {"knife_height": 60, "edge_mode": 2, "zone_hashs": [111]}
    mower.emit_snapshot(replace(MOWING, progress=1))
    await hass.async_block_till_done()
    # an unrelated snapshot change with the same settings adds nothing
    mower.emit_snapshot(replace(MOWING, progress=2))
    await hass.async_block_till_done()
    # a settings change is recorded at the next snapshot
    mower.job_settings_value = {"knife_height": 60, "edge_mode": 0, "zone_hashs": [111]}
    mower.emit_snapshot(replace(MOWING, progress=3))
    await hass.async_block_till_done()

    records = of_kind(await read(hass, entry), "job_settings")
    assert [r["settings"]["edge_mode"] for r in records] == [2, 0]
    assert records[0]["job_active"] is True
    assert records[0]["settings"]["knife_height"] == 60


async def test_a_failing_settings_peek_never_reaches_the_core(hass, director, freezer) -> None:
    entry, mower, _ = director
    mower.job_settings_error = RuntimeError("boom")
    mower.emit_snapshot(replace(MOWING, progress=5))
    await hass.async_block_till_done()
    rows = await read(hass, entry)
    assert of_kind(rows, "job_settings") == []
    assert of_kind(rows, "snapshot")  # the snapshot itself still went in


async def test_no_settings_means_no_record(hass, director, freezer) -> None:
    entry, mower, _ = director
    mower.job_settings_value = None
    mower.emit_snapshot(replace(MOWING, progress=5))
    await hass.async_block_till_done()
    assert of_kind(await read(hass, entry), "job_settings") == []
