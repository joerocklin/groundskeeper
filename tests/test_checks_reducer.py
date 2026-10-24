"""Reducer table tests for the Checks framework (spec §9.1, §9.2)."""

from dataclasses import replace
from datetime import timezone

import pytest

from custom_components.groundskeeper.adapter import NewError
from custom_components.groundskeeper.checks import (
    default_check_records,
    work_hours_open,
)
from custom_components.groundskeeper.core import (
    ChecksEdited,
    PauseSchedule,
    ResumeSchedule,
    SignalReceived,
    SnapshotReceived,
    StartRunEffect,
    TimerFired,
)
from tests.test_core_reducer import (
    DOCKED,
    ENV,
    T0,
    at,
    events,
    make_plan,
    step,
    waiting_state,
)


def custom(cid, name="Garage", *, timeout_s=None, policy="notify", enabled=True):
    return {"id": cid, "type": "custom", "name": name, "enabled": enabled,
            "config": {"mode": "template", "template": "x"},
            "timeout_s": timeout_s, "policy": policy}


def env_with(*extra, results=None, battery=None, work_hours=None):
    records = default_check_records()
    for rec in records:
        if rec["type"] == "battery" and battery is not None:
            rec["enabled"] = True
            rec["config"] = {"threshold_pct": battery}
        if rec["type"] == "work_hours":
            rec["enabled"] = work_hours is not None
            if work_hours is not None:
                rec["config"] = {"days": work_hours}
    return replace(ENV, checks=tuple(records) + tuple(extra), custom_results=results or {},
                   tz=timezone.utc)


def start(env, snapshot=DOCKED):
    from custom_components.groundskeeper.core import AddPlan, initial_core_state
    state, _ = step(initial_core_state(), AddPlan(at(0), *make_plan(), start=True), env)
    state, fx = step(state, SnapshotReceived(at(0), snapshot), env)
    return state, fx


def failing_ids(state):
    return {w["check"]: w["reason"] for w in state["state"]["waiting_on"]}


# -- pass rule, custom Checks, disabled Checks -------------------------------


def test_custom_check_failing_blocks_and_names_reason():
    env = env_with(custom("c1"), results={"c1": "unavailable"})
    state, _ = start(env)
    assert failing_ids(state) == {"c1": "unavailable"}
    assert "stability" not in state["state"]["deadlines"]


def test_custom_check_passing_lets_stability_window_run():
    env = env_with(custom("c1"), results={"c1": None})
    state, _ = start(env)
    assert state["state"]["waiting_on"] == []
    assert state["state"]["deadlines"]["stability"] == at(60).isoformat()


def test_disabled_custom_check_does_not_block():
    env = env_with(custom("c1", enabled=False), results={"c1": "false"})
    state, _ = start(env)
    assert state["state"]["waiting_on"] == []


def test_tick_is_armed_while_waiting_and_rearmed_when_it_fires():
    env = env_with(custom("c1"), results={"c1": "false"})
    state, _ = start(env)
    assert state["state"]["deadlines"]["check_tick"] == at(60).isoformat()
    state, _ = step(state, TimerFired(at(60)), env)
    assert state["state"]["deadlines"]["check_tick"] == at(120).isoformat()


def test_tick_does_not_slide_on_frequent_snapshots():
    env = env_with(custom("c1"), results={"c1": "false"})
    state, _ = start(env)
    state, _ = step(state, SnapshotReceived(at(30), DOCKED), env)
    assert state["state"]["deadlines"]["check_tick"] == at(60).isoformat()


def test_stability_window_restarts_when_a_check_fails_then_recovers():
    env = env_with(custom("c1"), results={"c1": None})
    state, _ = start(env)
    bad = replace(env, custom_results={"c1": "false"})
    state, _ = step(state, TimerFired(at(30)), bad)
    assert "stability" not in state["state"]["deadlines"]
    state, _ = step(state, TimerFired(at(40)), env)
    assert state["state"]["deadlines"]["stability"] == at(100).isoformat()
    state, fx = step(state, TimerFired(at(100)), env)
    assert [e for e in fx if isinstance(e, StartRunEffect)]


# -- Battery ---------------------------------------------------------------------


def test_battery_is_off_by_default():
    state, _ = start(env_with(), replace(DOCKED, battery=30))
    assert state["state"]["waiting_on"] == []


def test_battery_blocks_first_run_below_threshold_and_passes_above():
    env = env_with(battery=80)
    state, _ = start(env, replace(DOCKED, battery=50))
    assert failing_ids(state) == {"battery": "below_threshold"}
    state, _ = step(state, SnapshotReceived(at(10), replace(DOCKED, battery=80)), env)
    assert state["state"]["waiting_on"] == []


def test_battery_default_threshold_is_80_percent():
    records = {r["type"]: r for r in default_check_records()}
    assert records["battery"]["enabled"] is False
    assert records["battery"]["config"]["threshold_pct"] == 80


def test_battery_not_rechecked_after_resume_from_paused():
    env = env_with(battery=80)
    state, _ = start(env, replace(DOCKED, battery=90))
    state, _ = step(state, PauseSchedule(at(5)), env)
    state, _ = step(state, SnapshotReceived(at(6), replace(DOCKED, battery=40)), env)
    state, _ = step(state, ResumeSchedule(at(7)), env)
    assert state["state"]["schedule"]["status"] == "waiting"
    assert state["state"]["waiting_on"] == []


def test_battery_flag_clears_when_first_run_starts():
    env = env_with(battery=80)
    state, _ = start(env, replace(DOCKED, battery=90))
    assert state["state"]["battery_pending"] is True
    state, _ = step(state, TimerFired(at(61)), env)
    assert state["state"]["battery_pending"] is False


# -- Work hours ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [("07:59", False), ("08:00", True), ("23:59", True), ("00:00", False)],
)
def test_default_work_hours_window_is_08_to_midnight(hhmm, expected):
    from datetime import datetime

    cfg = next(r for r in default_check_records() if r["type"] == "work_hours")["config"]
    hour, minute = map(int, hhmm.split(":"))
    now = datetime(2026, 10, 21, hour, minute, tzinfo=timezone.utc)  # a Wednesday
    assert work_hours_open(cfg, now) is expected


def test_work_hours_per_weekday_window():
    from datetime import datetime

    cfg = {"days": {"wed": ["10:00", "12:00"]}}
    wed = lambda h: datetime(2026, 10, 21, h, 0, tzinfo=timezone.utc)  # noqa: E731
    assert work_hours_open(cfg, wed(11)) is True
    assert work_hours_open(cfg, wed(12)) is False
    # days not listed fall back to the default window
    thu = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
    assert work_hours_open(cfg, thu) is True


def test_work_hours_outside_window_blocks_start_only():
    env = env_with(work_hours={"wed": ["13:00", "17:00"]})  # T0 is Wed 12:00
    state, _ = start(env)
    assert failing_ids(state) == {"work_hours": "outside_work_hours"}
    state, _ = step(state, TimerFired(at(3600)), env)
    assert state["state"]["deadlines"].get("stability") == at(3660).isoformat()


# -- Timeouts and policies ---------------------------------------------------------


def test_notify_policy_fires_once_and_keeps_waiting():
    env = env_with(custom("c1", timeout_s=600), results={"c1": "false"})
    state, _ = start(env)
    assert state["state"]["deadlines"]["check_timeout:c1"] == at(600).isoformat()
    state, fx = step(state, TimerFired(at(599)), env)
    assert not events(fx, "check_timeout")
    state, fx = step(state, TimerFired(at(600)), env)
    (event,) = events(fx, "check_timeout")
    assert event.payload["check_id"] == "c1"
    assert event.payload["name"] == "Garage"
    assert event.payload["policy"] == "notify"
    assert state["state"]["schedule"]["status"] == "waiting"
    state, fx = step(state, TimerFired(at(5000)), env)
    assert not events(fx, "check_timeout")


def test_pause_policy_pauses_with_reason_and_resume_gets_fresh_clock():
    env = env_with(custom("c1", timeout_s=600, policy="pause"), results={"c1": "false"})
    state, _ = start(env)
    state, fx = step(state, TimerFired(at(600)), env)
    assert events(fx, "check_timeout")
    sched = state["state"]["schedule"]
    assert (sched["status"], sched["reason"]) == ("paused", "Check Garage timed out")
    assert not any(k.startswith("check_timeout") for k in state["state"]["deadlines"])
    state, fx = step(state, ResumeSchedule(at(1000)), env)
    assert state["state"]["schedule"]["status"] == "waiting"
    assert state["state"]["deadlines"]["check_timeout:c1"] == at(1600).isoformat()
    state, fx = step(state, TimerFired(at(1600)), env)
    assert events(fx, "check_timeout")  # a new wait notifies again


def test_clock_starts_when_that_check_begins_blocking():
    env_ok = env_with(custom("c1", timeout_s=600), results={"c1": None})
    state, _ = start(env_ok)
    bad = replace(env_ok, custom_results={"c1": "false"})
    state, _ = step(state, TimerFired(at(100)), bad)
    assert state["state"]["deadlines"]["check_timeout:c1"] == at(700).isoformat()


def test_clock_is_dropped_when_check_passes():
    env = env_with(custom("c1", timeout_s=600), results={"c1": "false"})
    state, _ = start(env)
    state, _ = step(state, TimerFired(at(10)), replace(env, custom_results={"c1": None}))
    assert "check_timeout:c1" not in state["state"]["deadlines"]


def test_leaving_waiting_clears_check_state():
    env = env_with(custom("c1", timeout_s=600), results={"c1": "false"})
    state, _ = start(env)
    state, _ = step(state, PauseSchedule(at(10)), env)
    assert "check_timeout:c1" not in state["state"]["deadlines"]
    assert "check_tick" not in state["state"]["deadlines"]
    assert state["state"]["check_clocks"] == {}


def test_mower_ready_notifies_immediately_on_error_and_never_pauses():
    env = env_with()
    state, _ = start(env)
    state, fx = step(state, SignalReceived(at(1), NewError("2748", "blocked", at(0))), env)
    (event,) = events(fx, "check_timeout")
    assert event.payload["check_id"] == "mower_ready"
    assert state["state"]["schedule"]["status"] == "waiting"


def test_mower_ready_other_failures_notify_once_after_15_minutes():
    env = env_with()
    state, _ = start(env, replace(DOCKED, job_active=True))
    assert state["state"]["deadlines"]["check_timeout:mower_ready"] == at(900).isoformat()
    state, fx = step(state, TimerFired(at(900)), env)
    assert len(events(fx, "check_timeout")) == 1
    assert state["state"]["schedule"]["status"] == "waiting"
    state, fx = step(state, TimerFired(at(4000)), env)
    assert not events(fx, "check_timeout")


def test_mower_ready_pause_policy_in_record_is_ignored():
    env = env_with()
    env = replace(env, checks=tuple({**r, "policy": "pause"} if r["id"] == "mower_ready" else r
                                    for r in env.checks))
    state, _ = start(env, replace(DOCKED, job_active=True))
    state, _ = step(state, TimerFired(at(900)), env)
    assert state["state"]["schedule"]["status"] == "waiting"


def test_zone_ready_pauses_after_30_minutes():
    from custom_components.groundskeeper.core import AddPlan, initial_core_state

    env = env_with()
    state, _ = step(initial_core_state(), AddPlan(at(0), *make_plan(zone="z2"), start=True), env)
    state, _ = step(state, SnapshotReceived(at(0), DOCKED), env)
    assert state["state"]["deadlines"]["check_timeout:zone_ready"] == at(1800).isoformat()
    state, fx = step(state, TimerFired(at(1800)), env)
    sched = state["state"]["schedule"]
    assert (sched["status"], sched["reason"]) == ("paused", "Check Zone ready timed out")
    assert events(fx, "check_timeout")


def test_built_in_always_on_checks_ignore_disable():
    env = env_with()
    env = replace(env, checks=tuple({**r, "enabled": False} if r["id"] == "mower_ready" else r
                                    for r in env.checks))
    state, _ = start(env, replace(DOCKED, job_active=True))
    assert "mower_ready" in failing_ids(state)


# -- Edits while waiting ------------------------------------------------------------


def test_edit_restarts_only_the_edited_clock_and_the_stability_window():
    env = env_with(custom("c1", timeout_s=600), custom("c2", "Door", timeout_s=600),
                   results={"c1": "false", "c2": "false"})
    state, _ = start(env)
    edited = replace(env, checks=tuple(
        {**r, "timeout_s": 900} if r["id"] == "c1" else r for r in env.checks))
    state, _ = step(state, ChecksEdited(at(300), ("c1",)), edited)
    assert state["state"]["deadlines"]["check_timeout:c1"] == at(1200).isoformat()
    assert state["state"]["deadlines"]["check_timeout:c2"] == at(600).isoformat()


def test_any_edit_restarts_the_stability_window():
    env = env_with()
    state, _ = start(env)
    assert state["state"]["deadlines"]["stability"] == at(60).isoformat()
    state, _ = step(state, ChecksEdited(at(30), ()), env)
    assert state["state"]["deadlines"]["stability"] == at(90).isoformat()


def test_edit_re_arms_a_notification_that_already_fired():
    env = env_with(custom("c1", timeout_s=600), results={"c1": "false"})
    state, _ = start(env)
    state, fx = step(state, TimerFired(at(600)), env)
    assert events(fx, "check_timeout")
    state, _ = step(state, ChecksEdited(at(700), ("c1",)), env)
    state, fx = step(state, TimerFired(at(1300)), env)
    assert events(fx, "check_timeout")


def test_removed_check_drops_its_clock():
    env = env_with(custom("c1", timeout_s=600), results={"c1": "false"})
    state, _ = start(env)
    removed = replace(env, checks=tuple(r for r in env.checks if r["id"] != "c1"))
    state, _ = step(state, ChecksEdited(at(10), ("c1",)), removed)
    assert "check_timeout:c1" not in state["state"]["deadlines"]
    assert "c1" not in state["state"]["check_clocks"]
