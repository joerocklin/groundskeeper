"""HA integration test: one maintenance Run, `add_plan` to done, with FakeMower."""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_DEVICE
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.mowing_director import adapters
from custom_components.mowing_director.adapter import Accepted, Refused, ZoneTaskComplete
from custom_components.mowing_director.const import DOMAIN, EVENT_TYPE
from custom_components.mowing_director.logbook import describe
from tests.conftest import make_mammotion_mower
from tests.fake_mower import DEFAULT_SNAPSHOT, FakeMower

MOWING = replace(DEFAULT_SNAPSHOT, activity=LawnMowerActivity.MOWING, job_active=True,
                 job_ref="job-1")


class RecordingMower(FakeMower):
    """Records the Store content visible at the moment start_run is called."""

    def __init__(self, hass_storage, key_getter):
        super().__init__()
        self.storage = hass_storage
        self.key_getter = key_getter
        self.seen_at_start = None

    async def start_run(self, zone, settings):
        data = self.storage[self.key_getter()]["data"]["state"]
        self.seen_at_start = {"intent": data["intent"], "status": data["schedule"]["status"]}
        return await super().start_run(zone, settings)


@pytest.fixture
async def director(hass, hass_storage, monkeypatch):
    """A loaded Mowing Director entry driven by a RecordingMower."""
    holder = {}
    mower = RecordingMower(hass_storage, lambda: holder["key"])
    monkeypatch.setitem(adapters.ADAPTER_REGISTRY, "mammotion", lambda h, e: mower)
    _, device, _ = await make_mammotion_mower(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DEVICE: device.id})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    entry = result["result"]
    holder["key"] = f"mowing_director.{entry.entry_id}"
    await hass.async_block_till_done()
    return entry, mower, device


async def advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def add_plan(hass, **extra):
    return await hass.services.async_call(
        DOMAIN,
        "add_plan",
        {"zone": "front lawn", "start_height_mm": 70, "end_height_mm": 70, **extra},
        blocking=True,
        return_response=True,
    )


def state_of(hass, entity_id):
    return hass.states.get(entity_id)


async def test_add_plan_to_done_end_to_end(hass, hass_storage, director, freezer) -> None:
    entry, mower, device = director
    events = async_capture_events(hass, EVENT_TYPE)

    response = await add_plan(hass, start=True)
    assert response["started"] is True
    assert response["schedule_status"] == "waiting"
    assert len(response["runs"]) == 1
    assert response["runs"][0]["height_mm"] == 70
    plan_id = response["plan_id"]
    assert state_of(hass, "sensor.example_mower_schedule").state == "waiting"
    assert state_of(hass, "sensor.example_mower_next_run").state == "Front Lawn"

    # Checks pass but the 60 s stability window has not elapsed: no command.
    assert mower.commands == []
    await advance(hass, freezer, 61)

    # Write-ahead: the intent was already in the Store when start_run ran.
    assert [c[0] for c in mower.commands] == ["start_run"]
    assert mower.seen_at_start["intent"]["command"] == "start_run"
    assert mower.seen_at_start["status"] == "running"
    zone_ref, settings = mower.commands[0][1]
    assert zone_ref == "11111111"
    assert settings.blade_height_mm == 70
    assert state_of(hass, "sensor.example_mower_schedule").state == "running"
    assert state_of(hass, "sensor.example_mower_current_run").state == "starting"

    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    current = state_of(hass, "sensor.example_mower_current_run")
    assert current.state == "mowing"
    assert current.attributes["zone"] == "Front Lawn"
    assert current.attributes["height_mm"] == 70
    assert current.attributes["n"] == 1 and current.attributes["m"] == 1
    assert current.attributes["started_at"] is not None

    mower.emit_signal(ZoneTaskComplete("11111111"))
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.example_mower_schedule").state == "idle"
    assert state_of(hass, "sensor.example_mower_current_run").state == "none"

    stored = hass_storage[f"mowing_director.{entry.entry_id}"]["data"]
    assert stored["state"]["runs"] == []
    assert [r["status"] for r in stored["history"]["runs"]] == ["done"]
    assert stored["history"]["plans"][plan_id]["origin"] == "service"

    types = [e.data["type"] for e in events]
    assert types == [
        "plan_added", "schedule_status_changed", "run_status_changed",
        "schedule_status_changed", "run_status_changed", "run_status_changed",
        "schedule_status_changed", "schedule_completed",
    ]
    for e in events:
        assert e.data["config_entry_id"] == entry.entry_id
        assert e.data["device_id"] == device.id
    assert "Schedule completed" == describe(events[-1].data)


async def test_reload_while_mowing_never_commands_and_rearms_from_state(
    hass, director, freezer
) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert [c[0] for c in mower.commands] == ["start_run"]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert [c[0] for c in mower.commands] == ["start_run"]  # nothing re-sent
    assert state_of(hass, "sensor.example_mower_current_run").state == "mowing"
    mower.emit_signal(ZoneTaskComplete("11111111"))
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.example_mower_schedule").state == "idle"


async def test_add_plan_without_start_stays_idle(hass, director) -> None:
    response = await add_plan(hass)
    assert response["started"] is False and "reason" not in response
    assert response["schedule_status"] == "idle"
    assert state_of(hass, "sensor.example_mower_schedule").attributes["runs_remaining"] == 1


async def test_cancel_active_run_aborts(hass, director, freezer) -> None:
    _, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    await hass.services.async_call(DOMAIN, "cancel", {}, blocking=True)
    assert [c[0] for c in mower.commands] == ["start_run", "abort_run"]
    assert state_of(hass, "sensor.example_mower_schedule").state == "idle"


async def test_start_service_starts_idle_schedule(hass, director) -> None:
    await add_plan(hass)
    await hass.services.async_call(DOMAIN, "start", {}, blocking=True)
    assert state_of(hass, "sensor.example_mower_schedule").state == "waiting"


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("start", {}),  # nothing to start
        ("cancel", {}),  # nothing to cancel
        ("add_plan", {"zone": "Nope", "start_height_mm": 70, "end_height_mm": 70}),
        ("add_plan", {"zone": "front lawn", "start_height_mm": 90, "end_height_mm": 60}),
        ("add_plan", {"zone": "front lawn", "start_height_mm": 70, "end_height_mm": 70,
                      "config_entry_id": "nope"}),
    ],
)
async def test_services_raise_validation_errors(hass, director, service, data) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)


async def test_add_plan_accepts_zone_ulid_and_rejects_ambiguous_names(hass, director) -> None:
    entry, mower, _ = director
    d = hass.data[DOMAIN]["director"][entry.entry_id]
    zones = d.data["config"]["zones"]
    response = await add_plan(hass, zone=zones[0]["id"])
    assert response["plan_id"]
    zones[1]["name"] = zones[0]["name"].upper()
    with pytest.raises(ServiceValidationError):
        await add_plan(hass)


async def test_add_plan_start_true_while_waiting_only_appends(hass, director) -> None:
    await add_plan(hass, start=True)
    response = await add_plan(hass, start=True)
    assert response["started"] is False
    assert response["reason"] == "already_active"
    assert response["schedule_status"] == "waiting"


async def test_deadlines_persist_as_absolute_utc(hass, hass_storage, director) -> None:
    from datetime import datetime

    entry, _, _ = director
    await add_plan(hass, start=True)
    stored = hass_storage[f"mowing_director.{entry.entry_id}"]["data"]["state"]
    deadline = datetime.fromisoformat(stored["deadlines"]["stability"])
    assert deadline.utcoffset() == timedelta(0)


async def test_snapshot_only_changes_use_60s_throttled_save(
    hass, hass_storage, director, freezer
) -> None:
    entry, mower, _ = director
    await add_plan(hass)  # idle; saved write-ahead with snapshot battery 80
    key = f"mowing_director.{entry.entry_id}"
    battery = lambda: hass_storage[key]["data"]["state"]["snapshot"]["battery"]  # noqa: E731
    assert battery() == 80

    mower.emit_snapshot(replace(DEFAULT_SNAPSHOT, battery=55))
    await hass.async_block_till_done()
    assert battery() == 80  # not yet written

    await advance(hass, freezer, 61)
    assert battery() == 55


async def test_refused_start_pauses_then_retry_and_second_attempt_starts(
    hass, director, freezer
) -> None:
    _, mower, _ = director
    mower.script_result("start_run", Refused("no"))
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    assert state_of(hass, "sensor.example_mower_schedule").state == "paused"
    assert [c[0] for c in mower.commands] == ["start_run"]  # no abort, no re-issue
    await advance(hass, freezer, 3600)  # no clock decides
    assert state_of(hass, "sensor.example_mower_schedule").state == "paused"
    assert [c[0] for c in mower.commands] == ["start_run"]

    await hass.services.async_call(DOMAIN, "retry_run", {}, blocking=True)
    assert state_of(hass, "sensor.example_mower_schedule").state == "waiting"
    await advance(hass, freezer, 61)
    assert [c[0] for c in mower.commands] == ["start_run", "start_run"]
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.example_mower_current_run").state == "mowing"


async def test_start_window_expiry_aborts_then_skip_plan_goes_idle(
    hass, director, freezer
) -> None:
    _, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    await advance(hass, freezer, 301)  # accepted, but no job ever appears
    assert [c[0] for c in mower.commands] == ["start_run", "abort_run"]
    assert state_of(hass, "sensor.example_mower_schedule").state == "paused"

    await hass.services.async_call(DOMAIN, "skip_plan", {}, blocking=True)
    assert state_of(hass, "sensor.example_mower_schedule").state == "idle"
    assert [c[0] for c in mower.commands] == ["start_run", "abort_run"]


async def test_cancel_from_paused_failure_cancels_everything(hass, director, freezer) -> None:
    _, mower, _ = director
    mower.script_result("start_run", Refused("no"))
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    await hass.services.async_call(DOMAIN, "cancel", {}, blocking=True)
    assert state_of(hass, "sensor.example_mower_schedule").state == "idle"
    assert [c[0] for c in mower.commands] == ["start_run"]


@pytest.mark.parametrize("service", ["retry_run", "skip_plan"])
async def test_answer_services_need_a_failure_to_answer(hass, director, service) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, service, {}, blocking=True)
