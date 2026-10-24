"""HA integration tests for restart reconciliation with FakeMower (#20)."""

from dataclasses import replace
from datetime import timedelta

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.groundskeeper.const import DOMAIN, EVENT_TYPE
from tests.fake_mower import DEFAULT_SNAPSHOT
from tests.test_tracer_bullet import (  # noqa: F401
    MOWING,
    add_plan,
    advance,
    director,
    state_of,
)

CURRENT = "sensor.example_mower_current_run"
SCHEDULE = "sensor.example_mower_schedule"
GONE = replace(DEFAULT_SNAPSHOT, available=False, activity=None)


async def down_and_up(hass, freezer, entry, downtime=0):
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=downtime))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def commands(mower):
    return [c[0] for c in mower.commands]


async def mowing_run(hass, mower, freezer):
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert state_of(hass, CURRENT).state == "mowing"


async def test_every_load_fires_one_reconciled_event(hass, director) -> None:
    entry, _, _ = director
    events = async_capture_events(hass, EVENT_TYPE)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    [event] = [e.data for e in events if e.data["type"] == "reconciled"]
    assert event["before"] == event["after"] == {"schedule": "idle", "run": None}
    assert event["config_entry_id"] == entry.entry_id


async def test_reload_while_starting_never_resends_start_and_waits_out_window(
    hass, director, freezer
) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    await down_and_up(hass, freezer, entry, downtime=30)
    assert commands(mower) == ["start_run"]
    assert state_of(hass, CURRENT).state == "starting"
    await advance(hass, freezer, 301)  # the remaining start window expires
    assert commands(mower) == ["start_run", "abort_run"]
    assert state_of(hass, SCHEDULE).state == "paused"


async def test_reducer_is_frozen_until_first_usable_snapshot_then_evidence_decides(
    hass, director, freezer
) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(GONE)
    await hass.async_block_till_done()
    await down_and_up(hass, freezer, entry, downtime=400)  # window long expired
    await advance(hass, freezer, 200)  # still waiting for the Mower
    assert commands(mower) == ["start_run"]  # frozen: nothing decided, nothing sent
    assert state_of(hass, CURRENT).state == "starting"

    mower.emit_snapshot(MOWING)  # the evidence: it did start after all
    await hass.async_block_till_done()
    assert state_of(hass, CURRENT).state == "mowing"
    assert commands(mower) == ["start_run"]  # the expired window never fired


async def test_wait_for_mower_gives_up_after_5_min_and_grace_counts_from_load(
    hass, director, freezer
) -> None:
    entry, mower, _ = director
    await mowing_run(hass, mower, freezer)
    mower.emit_snapshot(GONE)
    await hass.async_block_till_done()
    await down_and_up(hass, freezer, entry, downtime=7200)  # HA was down for 2 h
    await advance(hass, freezer, 301)  # wait over, no snapshot
    assert state_of(hass, CURRENT).state == "mowing"  # downtime is not lost contact
    await advance(hass, freezer, 300)  # 10 min from load
    assert state_of(hass, CURRENT).state == "on_hold"
    assert state_of(hass, CURRENT).attributes["on_hold_reason"] == "contact_lost"
    assert commands(mower) == ["start_run"]


async def test_missed_job_swap_while_down_is_superseded_without_command(
    hass, director, freezer
) -> None:
    entry, mower, _ = director
    await mowing_run(hass, mower, freezer)
    mower.emit_snapshot(GONE)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    mower.emit_snapshot(replace(MOWING, job_ref="someone-else"))
    events = async_capture_events(hass, EVENT_TYPE)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, SCHEDULE).state == "paused"
    assert commands(mower) == ["start_run"]
    [event] = [e.data for e in events if e.data["type"] == "reconciled"]
    assert event["after"]["run"] == "failed" and "superseded" in event["reasons"]


async def test_unusable_store_raises_repairs_and_leaves_schedule_idle_and_empty(
    hass, hass_storage, director, freezer
) -> None:
    entry, mower, _ = director
    await mowing_run(hass, mower, freezer)
    assert await hass.config_entries.async_unload(entry.entry_id)
    key = f"groundskeeper.{entry.entry_id}"
    hass_storage[key]["data"]["state"]["runs"] = "garbage"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, SCHEDULE).state == "idle"
    assert state_of(hass, CURRENT).state == "none"
    assert commands(mower) == ["start_run"]  # never commands, never adopts the live job
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"store_unusable_{entry.entry_id}")
    assert issue is not None
