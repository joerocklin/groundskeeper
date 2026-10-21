"""Debug recording wired into the Director and entry lifecycle (#35, spec §15)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from homeassistant.components import persistent_notification
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director import async_remove_entry
from custom_components.mowing_director.adapter import ZoneTaskComplete
from custom_components.mowing_director.const import DOMAIN
from custom_components.mowing_director.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.mowing_director.recording import (
    async_get_recording,
    async_post_backup,
    async_pre_backup,
)
from custom_components.mowing_director.runtime import async_get_director
from tests.test_tracer_bullet import (  # noqa: F401
    MOWING,
    add_plan,
    advance,
    director,
)


def db_path(hass, entry) -> Path:
    return Path(hass.config.path("mowing_director", f"recording_{entry.entry_id}.db"))


async def flush(hass, entry) -> None:
    await hass.async_add_executor_job(async_get_recording(hass, entry.entry_id).writer.flush)


async def read(hass, entry, sql="select kind, run_id, payload from records order by seq"):
    await flush(hass, entry)

    def _read():
        con = sqlite3.connect(db_path(hass, entry))
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    return await hass.async_add_executor_job(_read)


def of_kind(rows, kind):
    return [json.loads(p) for k, _r, p in rows if k == kind]


async def test_config_flow_stores_flag_and_creates_file_with_meta(hass, director) -> None:
    entry, _, _ = director
    assert entry.options["debug_recording"] is True  # explicit per entry
    path = db_path(hass, entry)
    assert path.exists()
    await flush(hass, entry)
    con = sqlite3.connect(path)
    meta = dict(con.execute("select key, value from meta"))
    con.close()
    assert meta["adapter_domain"] == "fake_mower"
    assert meta["model"] == "Fake 9000"
    assert meta["firmware_version"] == "1.2.3"
    assert meta["integration_version"] == "0.1.0"
    assert "SN-FAKE-1" not in json.dumps(meta)


async def test_run_leaves_every_on_change_record_kind(hass, director, freezer) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    mower.emit_signal(ZoneTaskComplete("11111111"))
    await hass.async_block_till_done()

    rows = await read(hass, entry)
    kinds = {k for k, _r, _p in rows}
    assert {"snapshot", "command", "transition", "run_summary", "lifecycle"} <= kinds

    [cmd] = of_kind(rows, "command")
    assert cmd["command"] == "start_run"
    assert cmd["result"] == "accepted"
    assert cmd["latency_ms"] >= 0
    assert cmd["args"]["zone_ref"] == "11111111"

    transitions = of_kind(rows, "transition")
    assert {"scope": "run", "from": "starting", "to": "mowing"}.items() <= next(
        t for t in transitions if t["to"] == "mowing"
    ).items()
    assert any(t["scope"] == "schedule" and t["to"] == "running" for t in transitions)
    assert all("cause" in t for t in transitions)

    [summary] = of_kind(rows, "run_summary")
    assert summary["status"] == "done"
    assert summary["settings"]["blade_height_mm"] == 70
    assert "angle_draw" in summary and summary["angle_deg"] is not None

    events = [p["event"] for p in of_kind(rows, "lifecycle")]
    assert "start" in events and "reconciled" in events
    # transitions are tagged with the Run they belong to
    assert any(r for k, r, _p in rows if k == "transition" and r)


async def test_snapshot_recorded_on_change_only(hass, director) -> None:
    entry, mower, _ = director
    before = len(of_kind(await read(hass, entry), "snapshot"))
    mower.emit_snapshot(mower.snapshot)  # identical
    await hass.async_block_till_done()
    assert len(of_kind(await read(hass, entry), "snapshot")) == before
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert len(of_kind(await read(hass, entry), "snapshot")) == before + 1


async def test_raw_state_only_for_entities_on_the_mowers_device(hass, director) -> None:
    entry, _, device = director
    from homeassistant.helpers import entity_registry as er

    [mower_entity] = [
        e for e in er.async_entries_for_device(er.async_get(hass), device.id)
    ]
    hass.states.async_set(
        mower_entity.entity_id, "mowing", {"serial": "SN-FAKE-1 here", "battery": 50}
    )
    hass.states.async_set("sensor.unrelated", "5")
    await hass.async_block_till_done()
    rows = await read(hass, entry)
    raw = of_kind(rows, "raw_state")
    assert [r["entity_id"] for r in raw] == [mower_entity.entity_id]
    assert raw[0]["state"] == "mowing"
    assert raw[0]["attributes"]["battery"] == 50
    assert "SN-FAKE-1" not in json.dumps(raw)  # adapter redaction list


async def test_check_and_detector_api_records_only_on_change(hass, director) -> None:
    entry, _, _ = director
    d = async_get_director(hass, entry.entry_id)
    d.record_check("rain", "failing", "raining")
    d.record_check("rain", "failing", "raining")  # unchanged: nothing
    d.record_check("rain", "failing", "wet")  # reason changed
    d.record_check("rain", "passing", None)
    d.record_detector("stall", "estimate", {"minutes": 12}, run_id="r1")
    rows = await read(hass, entry)
    assert of_kind(rows, "check") == [
        {"check": "rain", "result": "failing", "reason": "raining"},
        {"check": "rain", "result": "failing", "reason": "wet"},
        {"check": "rain", "result": "passing", "reason": None},
    ]
    assert of_kind(rows, "detector") == [
        {"detector": "stall", "phase": "estimate", "minutes": 12}
    ]
    assert [r for k, r, _p in rows if k == "detector"] == ["r1"]


async def test_failing_checks_in_core_state_are_recorded_as_check_records(
    hass, director, freezer
) -> None:
    entry, mower, _ = director
    from dataclasses import replace

    mower.emit_snapshot(replace(mower.snapshot, battery=5))
    await hass.async_block_till_done()
    await add_plan(hass, start=True)
    await hass.async_block_till_done()
    checks = of_kind(await read(hass, entry), "check")
    assert checks and checks[0]["result"] == "failing"


async def test_write_error_raises_repair_and_never_reaches_core(hass, director, freezer) -> None:
    entry, mower, _ = director
    rec = async_get_recording(hass, entry.entry_id)
    rec.writer._con.close()  # sabotage under the writer thread
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    await hass.async_add_executor_job(rec.writer.wait_idle)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"recording_failed_{entry.entry_id}")
    assert issue is not None
    # the core carried on regardless
    assert hass.states.get("sensor.example_mower_schedule").state == "idle"
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()


async def test_size_and_low_disk_repairs(hass, director) -> None:
    entry, _, _ = director
    rec = async_get_recording(hass, entry.entry_id)
    reg = ir.async_get(hass)
    size_id = f"recording_size_{entry.entry_id}"
    disk_id = f"recording_low_disk_{entry.entry_id}"

    rec.handle_event("status", {"size": 2 * 2**30, "free": 50 * 2**30, "total": 100 * 2**30})
    assert reg.async_get_issue(DOMAIN, size_id).severity == ir.IssueSeverity.WARNING
    assert reg.async_get_issue(DOMAIN, disk_id) is None

    rec.handle_event("status", {"size": 1, "free": 5 * 2**30, "total": 100 * 2**30})  # <10 %
    assert reg.async_get_issue(DOMAIN, size_id) is None
    assert reg.async_get_issue(DOMAIN, disk_id) is not None

    rec.handle_event("status", {"size": 1, "free": 50 * 2**30, "total": 100 * 2**30})
    assert reg.async_get_issue(DOMAIN, disk_id) is None


async def test_size_threshold_is_adjustable(hass, director) -> None:
    entry, _, _ = director
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "recording_size_warn_bytes": 100}
    )
    rec = async_get_recording(hass, entry.entry_id)
    rec.handle_event("status", {"size": 500, "free": 50 * 2**30, "total": 100 * 2**30})
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"recording_size_{entry.entry_id}")


async def test_flag_off_means_no_file_and_no_thread(hass, monkeypatch) -> None:
    from custom_components.mowing_director import adapters
    from tests.conftest import make_mammotion_mower
    from tests.fake_mower import FakeMower
    from homeassistant.const import CONF_DEVICE

    monkeypatch.setitem(adapters.ADAPTER_REGISTRY, "mammotion", lambda h, e: FakeMower())
    _, device, _ = await make_mammotion_mower(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Example Mower", unique_id=device.id,
        data={CONF_DEVICE: device.id}, options={"debug_recording": False},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert async_get_recording(hass, entry.entry_id) is None
    assert not db_path(hass, entry).exists()
    assert (await async_get_config_entry_diagnostics(hass, entry))["debug_recording"] == {
        "enabled": False
    }


async def test_diagnostics_carries_summary_only(hass, director) -> None:
    entry, _, _ = director
    await flush(hass, entry)
    data = await async_get_config_entry_diagnostics(hass, entry)
    summary = data["debug_recording"]
    assert set(summary) == {"enabled", "size_bytes", "records", "oldest", "newest", "dropped"}
    assert summary["enabled"] is True and summary["records"] > 0


async def test_entry_removal_keeps_file_and_posts_notification_with_path(hass, director) -> None:
    entry, _, _ = director
    path = db_path(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await async_remove_entry(hass, entry)
    assert path.exists()
    notes = persistent_notification._async_get_or_create_notifications(hass)
    [note] = [n for k, n in notes.items() if entry.entry_id in k]
    assert str(path) in note["message"]


async def test_backup_hooks_checkpoint_and_pause_writes(hass, director) -> None:
    entry, mower, _ = director
    await async_pre_backup(hass)
    wal = Path(f"{db_path(hass, entry)}-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    await async_post_backup(hass)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert any(s["activity"] == "mowing" for s in of_kind(await read(hass, entry), "snapshot"))


async def test_unload_stops_writer_and_reload_appends_to_same_file(hass, director) -> None:
    entry, _, _ = director
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    events = [p["event"] for p in of_kind(await read(hass, entry), "lifecycle")]
    assert events.count("start") == 2 and "unload" in events
