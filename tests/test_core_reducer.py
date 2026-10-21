"""Reducer table tests for every transition in the tracer-bullet slice (spec §6.3).

Pure: no hass, no clock. Each test feeds inputs to `reduce` and asserts on
the returned state and effects.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity

from custom_components.mowing_director.adapter import (
    Accepted,
    JobStarted,
    NewError,
    Refused,
    SelfCheckReason,
    Unconfirmed,
    ZoneTaskAborted,
    ZoneTaskComplete,
)
from custom_components.mowing_director.core import (
    AbortRunEffect,
    AddPlan,
    CancelSchedule,
    CommandOutcome,
    Env,
    PlanStatus,
    RetryRun,
    SignalReceived,
    SkipPlan,
    SnapshotReceived,
    StartRunEffect,
    StartSchedule,
    TimerFired,
    ZoneView,
    initial_core_state,
    plan_status,
    reduce,
)
from tests.fake_mower import DEFAULT_SNAPSHOT

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
ENV = Env(
    zones={
        "z1": ZoneView("11111111", "Front Lawn", bearing=30.0),
        "z2": ZoneView("22222222", "Backyard", bearing=None),
        "zx": ZoneView("33333333", "Gone", missing=True, bearing=10.0),
    },
    min_start_battery_pct=15,
)
DOCKED = DEFAULT_SNAPSHOT
MOWING = replace(DOCKED, activity=LawnMowerActivity.MOWING, job_active=True, job_ref="j1")


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make_plan(plan_id="p1", zone="z1", n=1):
    plan = {"id": plan_id, "zone_id": zone, "zone_name": ENV.zones[zone].name,
            "origin": "service"}
    runs = [
        {
            "id": f"{plan_id}-r{i}", "plan_id": plan_id, "zone_id": zone,
            "zone_name": plan["zone_name"], "status": "pending", "reason": None,
            "index": i, "of": n, "height_mm": 70, "angle_deg": 0.0,
            "settings": {"blade_height_mm": 70, "spacing_cm": 25.0, "speed_m_s": 0.4,
                         "perimeter_laps": 1, "extras": {"ultra_wave": False}},
        }
        for i in range(1, n + 1)
    ]
    return plan, runs


def step(state, event, env=ENV):
    red = reduce(state, event, env)
    return red.state, list(red.effects)


def events(effects, type_=None):
    out = [e for e in effects if type(e).__name__ == "EmitEvent"]
    return [e for e in out if type_ is None or e.type == type_]


def waiting_state(n=1, zone="z1", snapshot=DOCKED):
    state = initial_core_state()
    plan, runs = make_plan(n=n, zone=zone)
    state, _ = step(state, AddPlan(at(0), plan, runs, start=True))
    state, _ = step(state, SnapshotReceived(at(0), snapshot))
    return state


def starting_state(n=1):
    state = waiting_state(n)
    state, _ = step(state, TimerFired(at(61)))
    return state


def mowing_state(n=1):
    state = starting_state(n)
    state, _ = step(state, SnapshotReceived(at(70), MOWING))
    return state


def run_status(state):
    return [r["status"] for r in state["state"]["runs"]]


def test_add_plan_without_start_stays_idle_and_announces_plan():
    state, fx = step(initial_core_state(), AddPlan(at(0), *make_plan()))
    assert state["state"]["schedule"]["status"] == "idle"
    assert run_status(state) == ["pending"]
    assert [e.type for e in events(fx)] == ["plan_added"]


def test_add_plan_start_moves_idle_to_waiting():
    state, fx = step(initial_core_state(), AddPlan(at(0), *make_plan(), start=True))
    assert state["state"]["schedule"]["status"] == "waiting"
    assert [e.type for e in events(fx)] == ["plan_added", "schedule_status_changed"]


def test_start_schedule_needs_runs():
    state, _ = step(initial_core_state(), StartSchedule(at(0)))
    assert state["state"]["schedule"]["status"] == "idle"
    state, _ = step(state, AddPlan(at(0), *make_plan()))
    state, _ = step(state, StartSchedule(at(1)))
    assert state["state"]["schedule"]["status"] == "waiting"


def test_add_plan_start_while_waiting_only_appends():
    state = waiting_state()
    state, _ = step(state, AddPlan(at(5), *make_plan("p2"), start=True))
    assert state["state"]["schedule"]["status"] == "waiting"
    assert run_status(state) == ["pending", "pending"]


def test_passing_checks_arm_stability_window_without_starting():
    state = waiting_state()
    assert state["state"]["deadlines"]["stability"] == at(60).isoformat()
    state, fx = step(state, TimerFired(at(30)))
    assert not [e for e in fx if isinstance(e, StartRunEffect)]
    assert state["state"]["schedule"]["status"] == "waiting"


@pytest.mark.parametrize(
    ("snapshot", "zone", "check", "reason"),
    [
        (replace(DOCKED, available=False), "z1", "mower_ready", "unavailable"),
        (replace(DOCKED, job_active=True), "z1", "mower_ready", "job_active"),
        (replace(DOCKED, self_check=SelfCheckReason.DEVICE_NON_WORK_HOURS), "z1",
         "mower_ready", "device_non_work_hours"),
        (replace(DOCKED, battery=14), "z1", "mower_ready", "battery_low"),
        (DOCKED, "z2", "zone_ready", "bearing_unset"),
    ],
)
def test_failing_check_blocks_start_and_names_reason(snapshot, zone, check, reason):
    state = waiting_state(zone=zone, snapshot=snapshot)
    assert {"check": check, "reason": reason} in state["state"]["waiting_on"]
    assert "stability" not in state["state"]["deadlines"]
    state, fx = step(state, TimerFired(at(500)))
    assert not [e for e in fx if isinstance(e, StartRunEffect)]


def test_new_error_while_waiting_blocks_and_old_error_is_deduped():
    state = waiting_state()
    err = NewError("2748", "blocked", at(-100))
    state, _ = step(state, SignalReceived(at(1), err))
    assert {"check": "mower_ready", "reason": "new_error"} in state["state"]["waiting_on"]
    assert "stability" not in state["state"]["deadlines"]
    # same time again changes nothing
    again, _ = step(state, SignalReceived(at(2), err))
    assert again == state


def test_failing_check_resets_stability_window():
    state = waiting_state()
    state, _ = step(state, SnapshotReceived(at(30), replace(DOCKED, job_active=True)))
    assert "stability" not in state["state"]["deadlines"]
    state, _ = step(state, SnapshotReceived(at(40), DOCKED))
    assert state["state"]["deadlines"]["stability"] == at(100).isoformat()


def test_missing_zone_fails_head_run_and_pauses_without_command():
    state = waiting_state(zone="zx")
    s = state["state"]
    assert s["schedule"]["status"] == "paused"
    assert s["runs"][0]["status"] == "failed"
    assert s["runs"][0]["reason"] == "zone_missing"


def test_stable_checks_start_run_persisting_intent_with_start_command():
    state = waiting_state()
    state, fx = step(state, TimerFired(at(61)))
    s = state["state"]
    assert s["schedule"]["status"] == "running"
    assert run_status(state) == ["starting"]
    assert s["intent"]["command"] == "start_run"
    assert s["command_log"][-1]["run_id"] == "p1-r1"
    assert s["deadlines"]["start_window"] == at(61 + 300).isoformat()
    (start,) = [e for e in fx if isinstance(e, StartRunEffect)]
    assert start.zone_ref == "11111111"
    assert start.settings.bearing_deg == 30.0  # zone bearing + angle 0
    assert start.settings.blade_height_mm == 70
    types = [e.type for e in events(fx)]
    assert types == ["run_status_changed", "schedule_status_changed"]


@pytest.mark.parametrize("result", [Accepted(), Unconfirmed()])
def test_accepted_or_unconfirmed_start_waits_without_failing(result):
    state = starting_state()
    state, fx = step(state, CommandOutcome(at(62), "p1-r1", "start_run", result))
    assert run_status(state) == ["starting"]
    assert state["state"]["command_log"][-1]["result"] == result.kind.value
    assert not fx


def test_refused_start_fails_run_and_pauses_without_abort():
    state = starting_state()
    state, fx = step(state, CommandOutcome(at(62), "p1-r1", "start_run", Refused("no")))
    assert run_status(state) == ["failed"]
    assert state["state"]["schedule"]["status"] == "paused"
    assert not [e for e in fx if isinstance(e, AbortRunEffect)]


def test_start_window_expiry_aborts_and_fails():
    state = starting_state()
    state, fx = step(state, TimerFired(at(61 + 301)))
    assert run_status(state) == ["failed"]
    assert state["state"]["runs"][0]["reason"] == "did_not_start"
    assert [e for e in fx if isinstance(e, AbortRunEffect)]


def test_first_mowing_job_active_snapshot_starts_run():
    state = starting_state()
    state, fx = step(state, SnapshotReceived(at(70), MOWING))
    run = state["state"]["runs"][0]
    assert run["status"] == "mowing"
    assert run["job_ref"] == "j1"
    assert "start_window" not in state["state"]["deadlines"]
    assert state["state"]["schedule"]["status"] == "running"


def test_docked_snapshot_does_not_start_run():
    state = starting_state()
    state, _ = step(state, SnapshotReceived(at(70), DOCKED))
    assert run_status(state) == ["starting"]


def test_zone_task_complete_finishes_run_atomically_into_history():
    state = mowing_state()
    state, fx = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")))
    assert state["state"]["runs"] == []
    assert state["state"]["active_run_id"] is None
    assert [r["status"] for r in state["history"]["runs"]] == ["done"]
    assert "p1" in state["history"]["plans"] and not state["state"]["plans"]
    assert state["state"]["schedule"]["status"] == "idle"
    assert [e.type for e in events(fx)] == [
        "run_status_changed", "schedule_status_changed", "schedule_completed",
    ]
    assert plan_status(state, "p1") is PlanStatus.COMPLETE
    assert len(state["learned"]["durations"]["z1"]) == 1


def test_zone_task_complete_for_other_zone_is_ignored():
    state = mowing_state()
    state, _ = step(state, SignalReceived(at(900), ZoneTaskComplete("99")))
    assert run_status(state) == ["mowing"]


def test_returning_with_no_job_needs_stable_60s_then_done():
    state = mowing_state()
    returning = replace(DOCKED, activity=LawnMowerActivity.RETURNING)
    state, _ = step(state, SnapshotReceived(at(1000), returning))
    assert state["state"]["deadlines"]["done_stable"] == at(1060).isoformat()
    state, _ = step(state, TimerFired(at(1030)))
    assert run_status(state) == ["mowing"]
    state, _ = step(state, TimerFired(at(1061)))
    assert state["state"]["runs"] == []
    assert state["state"]["schedule"]["status"] == "idle"


def test_flicker_resets_done_stability_window():
    state = mowing_state()
    returning = replace(DOCKED, activity=LawnMowerActivity.RETURNING)
    state, _ = step(state, SnapshotReceived(at(1000), returning))
    state, _ = step(state, SnapshotReceived(at(1020), MOWING))
    assert "done_stable" not in state["state"]["deadlines"]
    state, _ = step(state, TimerFired(at(1100)))
    assert run_status(state) == ["mowing"]


@pytest.mark.parametrize("activity", [LawnMowerActivity.RETURNING, LawnMowerActivity.DOCKED])
def test_returning_or_docked_with_job_active_continues(activity):
    state = mowing_state()
    snap = replace(MOWING, activity=activity)
    state, _ = step(state, SnapshotReceived(at(1000), snap))
    state, _ = step(state, TimerFired(at(2000)))
    assert run_status(state) == ["mowing"]
    assert "done_stable" not in state["state"]["deadlines"]


def test_progress_is_never_a_done_signal():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(1000), replace(MOWING, progress=100)))
    state, _ = step(state, TimerFired(at(2000)))
    assert run_status(state) == ["mowing"]


def test_done_with_runs_left_returns_schedule_to_waiting_and_rechecks():
    state = mowing_state(n=2)
    state, fx = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")))
    assert state["state"]["schedule"]["status"] == "waiting"
    assert run_status(state) == ["pending"]
    assert not events(fx, "schedule_completed")
    assert plan_status(state, "p1") is PlanStatus.ACTIVE


def test_cancel_active_run_aborts_and_empties_schedule():
    state = mowing_state(n=2)
    state, fx = step(state, CancelSchedule(at(900)))
    assert state["state"]["runs"] == [] and state["state"]["deadlines"] == {}
    assert sorted(r["status"] for r in state["history"]["runs"]) == ["cancelled", "cancelled"]
    assert state["state"]["schedule"]["status"] == "idle"
    assert [e for e in fx if isinstance(e, AbortRunEffect)]
    assert plan_status(state, "p1") is PlanStatus.ENDED_EARLY


def test_cancel_waiting_does_not_abort():
    state = waiting_state()
    state, fx = step(state, CancelSchedule(at(10)))
    assert state["state"]["schedule"]["status"] == "idle"
    assert not [e for e in fx if isinstance(e, AbortRunEffect)]
    assert "stability" not in state["state"]["deadlines"]


def test_reduce_never_mutates_its_input():
    state = waiting_state()
    import copy

    before = copy.deepcopy(state)
    reduce(state, TimerFired(at(61)), ENV)
    assert state == before


def failed_state(n=1, zone="z1"):
    """Schedule paused after the head Run failed `did_not_start` (refused)."""
    state = starting_state(n) if zone == "z1" else waiting_state(n, zone)
    if zone == "z1":
        state, _ = step(state, CommandOutcome(at(62), "p1-r1", "start_run", Refused("no")))
    return state


def test_any_run_failure_pauses_schedule_and_stores_reason():
    state = failed_state()
    assert state["state"]["schedule"]["status"] == "paused"
    assert state["state"]["schedule"]["reason"] == "did_not_start"
    assert state["state"]["runs"][0]["reason"] == "did_not_start"


def test_retry_run_returns_failed_run_to_pending_head_and_waits():
    state = failed_state(n=2)
    state, fx = step(state, RetryRun(at(100)))
    assert run_status(state) == ["pending", "pending"]
    assert state["state"]["runs"][0]["id"] == "p1-r1"
    assert state["state"]["runs"][0]["reason"] is None
    assert state["state"]["schedule"]["status"] == "waiting"
    assert not [e for e in fx if isinstance(e, (StartRunEffect, AbortRunEffect))]
    state, fx = step(state, SnapshotReceived(at(101), DOCKED))
    state, fx = step(state, TimerFired(at(170)))
    assert run_status(state)[0] == "starting"


def test_retry_run_keeps_schedule_paused_when_pause_requested():
    state = failed_state()
    state["state"]["schedule"]["pause_requested"] = True
    state, _ = step(state, RetryRun(at(100)))
    assert run_status(state) == ["pending"]
    assert state["state"]["schedule"]["status"] == "paused"


def test_retry_on_still_missing_zone_fails_again_once():
    state = waiting_state(zone="zx")
    assert run_status(state) == ["failed"]
    state, fx = step(state, RetryRun(at(100)))
    assert run_status(state) == ["failed"]
    assert state["state"]["runs"][0]["reason"] == "zone_missing"
    assert state["state"]["schedule"]["status"] == "paused"
    assert not [e for e in fx if isinstance(e, (StartRunEffect, AbortRunEffect))]
    assert len(events(fx, "run_status_changed")) == 2  # failed -> pending -> failed


def test_retry_without_a_failed_run_changes_nothing():
    state = waiting_state()
    after, fx = step(state, RetryRun(at(100)))
    assert after == state and not fx


def test_skip_plan_skips_pending_runs_keeps_failed_and_goes_idle_if_empty():
    state = failed_state(n=3)
    state, fx = step(state, SkipPlan(at(100)))
    s = state["state"]
    assert s["runs"] == [] and s["schedule"]["status"] == "idle"
    assert [(r["id"], r["status"]) for r in state["history"]["runs"]] == [
        ("p1-r1", "failed"), ("p1-r2", "skipped"), ("p1-r3", "skipped")]
    assert plan_status(state, "p1") is PlanStatus.ENDED_EARLY
    assert not [e for e in fx if isinstance(e, (StartRunEffect, AbortRunEffect))]


def test_skip_plan_leaves_other_plans_and_schedule_waiting():
    state = initial_core_state()
    for pid in ("p1", "p2"):
        plan, runs = make_plan(plan_id=pid, n=2)
        state, _ = step(state, AddPlan(at(0), plan, runs, start=pid == "p1"))
    state, _ = step(state, SnapshotReceived(at(0), DOCKED))
    state, _ = step(state, TimerFired(at(61)))
    state, _ = step(state, CommandOutcome(at(62), "p1-r1", "start_run", Refused("no")))
    state, _ = step(state, SkipPlan(at(100)))
    assert [r["id"] for r in state["state"]["runs"]] == ["p2-r1", "p2-r2"]
    assert state["state"]["schedule"]["status"] == "waiting"


def test_skip_plan_without_a_failed_run_changes_nothing():
    state = waiting_state()
    after, fx = step(state, SkipPlan(at(100)))
    assert after == state and not fx


def test_cancel_from_paused_cancels_everything():
    state = failed_state(n=2)
    state, fx = step(state, CancelSchedule(at(100)))
    s = state["state"]
    assert s["runs"] == [] and s["schedule"]["status"] == "idle"
    assert [r["status"] for r in state["history"]["runs"]] == ["failed", "cancelled"]
    assert not [e for e in fx if isinstance(e, AbortRunEffect)]


def test_unanswered_failure_stays_paused_no_clock_decides():
    state = failed_state()
    for t in (500, 5000, 86400 * 7):
        state, fx = step(state, TimerFired(at(t)))
        state, _ = step(state, SnapshotReceived(at(t), DOCKED))
        assert state["state"]["schedule"]["status"] == "paused"
        assert run_status(state) == ["failed"]


# --- Failure rules while mowing (spec §7.3, §7.7; ticket #16) -----------------


def failed_with(state, reason):
    run = state["state"]["runs"][0]
    assert (run["status"], run["reason"]) == ("failed", reason)
    assert state["state"]["schedule"]["status"] == "paused"
    assert state["state"]["active_run_id"] is None


def no_abort(fx):
    assert not [e for e in fx if isinstance(e, AbortRunEffect)]


@pytest.mark.parametrize("code", ["2741", "1003", "9999", "unknown"])
def test_new_error_while_mowing_fails_run_with_no_command(code):
    state = mowing_state()
    state, fx = step(state, SignalReceived(at(500), NewError(code, "boom", at(499))))
    failed_with(state, "error")
    no_abort(fx)
    assert state["state"]["last_error_time"] == at(499).isoformat()


def test_replayed_error_older_than_persisted_does_not_fail_run():
    state = mowing_state()
    state["state"]["last_error_time"] = at(600).isoformat()
    state, _ = step(state, SignalReceived(at(700), NewError("1", "old", at(599))))
    assert run_status(state) == ["mowing"]


def test_zone_task_aborted_fails_run_externally_aborted_no_command():
    state = mowing_state()
    state, fx = step(state, SignalReceived(at(500), ZoneTaskAborted("11111111")))
    failed_with(state, "externally_aborted")
    no_abort(fx)


def test_zone_task_aborted_for_other_zone_is_ignored():
    state = mowing_state()
    state, _ = step(state, SignalReceived(at(500), ZoneTaskAborted("99")))
    assert run_status(state) == ["mowing"]


def test_external_job_start_fails_run_superseded_no_command():
    state = mowing_state()
    state, fx = step(state, SignalReceived(at(500), JobStarted("j2", "11111111", at(499))))
    failed_with(state, "superseded")
    no_abort(fx)


def test_job_started_matching_recorded_job_ref_is_our_own():
    state = mowing_state()
    state, _ = step(state, SignalReceived(at(500), JobStarted("j1", "11111111", at(499))))
    assert run_status(state) == ["mowing"]


def test_job_started_while_starting_is_the_directors_own_start():
    state = starting_state()
    state, _ = step(state, SignalReceived(at(62), JobStarted("j1", "11111111", at(62))))
    assert run_status(state) == ["starting"]


def test_job_started_without_job_ref_is_external_only_for_another_zone():
    state = starting_state()
    state, _ = step(state, SnapshotReceived(at(70), replace(MOWING, job_ref=None)))
    own, _ = step(state, SignalReceived(at(80), JobStarted(None, "11111111", at(79))))
    assert run_status(own) == ["mowing"]
    other, _ = step(state, SignalReceived(at(80), JobStarted(None, "99", at(79))))
    failed_with(other, "superseded")


def test_signals_while_not_mowing_do_not_fail_the_waiting_schedule():
    state = waiting_state()
    state, _ = step(state, SignalReceived(at(5), ZoneTaskAborted("11111111")))
    state, _ = step(state, SignalReceived(at(6), JobStarted("j9", "11111111", at(6))))
    assert state["state"]["schedule"]["status"] == "waiting"


IDLE_OFF_JOB = replace(DOCKED, activity=LawnMowerActivity.IDLE, job_active=False)


def test_idle_without_job_stable_60s_fails_stopped_off_dock_no_command():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(1000), IDLE_OFF_JOB))
    assert state["state"]["deadlines"]["stopped_stable"] == at(1060).isoformat()
    state, fx = step(state, TimerFired(at(1030)))
    assert run_status(state) == ["mowing"]
    state, fx = step(state, TimerFired(at(1061)))
    failed_with(state, "stopped_off_dock")
    no_abort(fx)
    assert "stopped_stable" not in state["state"]["deadlines"]


def test_idle_flicker_resets_stopped_off_dock_window():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(1000), IDLE_OFF_JOB))
    state, _ = step(state, SnapshotReceived(at(1030), MOWING))
    assert "stopped_stable" not in state["state"]["deadlines"]
    state, _ = step(state, SnapshotReceived(at(1040), IDLE_OFF_JOB))
    state, _ = step(state, TimerFired(at(1070)))
    assert run_status(state) == ["mowing"]
    state, _ = step(state, TimerFired(at(1101)))
    failed_with(state, "stopped_off_dock")


def test_idle_with_job_active_is_not_stopped_off_dock():
    state = mowing_state()
    snap = replace(IDLE_OFF_JOB, job_active=True)
    state, _ = step(state, SnapshotReceived(at(1000), snap))
    state, _ = step(state, TimerFired(at(2000)))
    assert run_status(state) == ["mowing"]


def moving(progress=None, area=None, lat=None, activity=LawnMowerActivity.MOWING):
    from custom_components.mowing_director.adapter import Movement, Position

    pos = Position(lat, 0.0) if lat is not None else None
    return replace(MOWING, activity=activity, movement=Movement(progress, area, pos))


def test_entering_mowing_arms_stall_window_default_60_min():
    state = mowing_state()
    assert state["state"]["deadlines"]["stall"] == at(70 + 3600).isoformat()


def test_stall_window_is_per_mower_via_env():
    env = replace(ENV, stall_window=timedelta(minutes=20))
    state, _ = step(initial_core_state(), AddPlan(at(0), *make_plan(), start=True), env)
    state, _ = step(state, SnapshotReceived(at(0), DOCKED), env)
    state, _ = step(state, TimerFired(at(61)), env)
    state, _ = step(state, SnapshotReceived(at(70), MOWING), env)
    assert state["state"]["deadlines"]["stall"] == at(70 + 1200).isoformat()


def test_unchanged_movement_for_the_window_aborts_and_fails_stalled():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), moving(progress=5)))
    state, fx = step(state, TimerFired(at(100 + 3599)))
    assert run_status(state) == ["mowing"]
    state, fx = step(state, TimerFired(at(100 + 3601)))
    failed_with(state, "stalled")
    assert [e for e in fx if isinstance(e, AbortRunEffect)] == [AbortRunEffect("stalled")]
    assert "stall" not in state["state"]["deadlines"]


@pytest.mark.parametrize(
    "first,second",
    [
        (moving(progress=5), moving(progress=6)),
        (moving(area=1.0), moving(area=1.5)),
        (moving(lat=10.0), moving(lat=10.0001)),
    ],
)
def test_any_movement_indicator_change_resets_stall_clock(first, second):
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), first))
    state, _ = step(state, SnapshotReceived(at(3000), second))
    assert state["state"]["deadlines"]["stall"] == at(3000 + 3600).isoformat()
    state, _ = step(state, TimerFired(at(3000 + 3599)))
    assert run_status(state) == ["mowing"]


def test_repeated_identical_snapshots_do_not_reset_stall_clock():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), moving(progress=5)))
    armed = state["state"]["deadlines"]["stall"]
    state, _ = step(state, SnapshotReceived(at(2000), moving(progress=5)))
    assert state["state"]["deadlines"]["stall"] == armed


def test_returning_counts_for_the_stall_rule():
    state = mowing_state()
    ret = moving(progress=5, activity=LawnMowerActivity.RETURNING)
    state, _ = step(state, SnapshotReceived(at(100), ret))
    state, fx = step(state, SnapshotReceived(at(100 + 3601), ret))
    failed_with(state, "stalled")


def test_midjob_recharge_stays_mowing_and_never_stalls():
    state = mowing_state()
    charging = replace(
        MOWING, activity=LawnMowerActivity.DOCKED, recharging=True, battery=20,
    )
    state, _ = step(state, SnapshotReceived(at(100), charging))
    for t, battery in ((5000, 30), (10000, 40), (20000, 50)):  # charging, so rising
        state, fx = step(state, SnapshotReceived(at(t), replace(charging, battery=battery)))
        state, fx = step(state, TimerFired(at(t + 1)))
        assert run_status(state) == ["mowing"]
        no_abort(fx)
    assert state["state"]["snapshot"]["recharging"] is True


def test_mowing_with_progress_has_no_run_time_limit():
    state = mowing_state()
    for i in range(1, 11):
        state, _ = step(state, SnapshotReceived(at(i * 3000), moving(progress=i)))
        state, _ = step(state, TimerFired(at(i * 3000 + 1)))
    assert run_status(state) == ["mowing"]


def test_stall_deadline_due_but_fresh_movement_wins_over_the_clock():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), moving(progress=5)))
    state, _ = step(state, SnapshotReceived(at(3699), moving(progress=6)))
    state, _ = step(state, TimerFired(at(3702)))
    assert run_status(state) == ["mowing"]


# -- Prompts (spec §12, ticket #17) ---------------------------------------------

from custom_components.mowing_director.core import AnswerPrompt, RaisePrompt  # noqa: E402


def prompt_env(entry_id="entry1"):
    ids = iter(f"PROMPT{i}" for i in range(1, 100))
    return replace(ENV, entry_id=entry_id, new_id=lambda: next(ids))


def prompts(state):
    return state["state"]["prompts"]


def test_run_failure_raises_a_pending_failure_prompt():
    env = prompt_env()
    state = initial_core_state()
    plan, runs = make_plan()
    state, _ = step(state, AddPlan(at(0), plan, runs, start=True), env)
    state, _ = step(state, SnapshotReceived(at(0), DOCKED), env)
    state, _ = step(state, TimerFired(at(61)), env)
    state, fx = step(state, CommandOutcome(at(62), "p1-r1", "start_run", Refused("no")), env)

    [prompt] = prompts(state)
    assert prompt["prompt_id"] == "PROMPT1"
    assert prompt["kind"] == "run_failed"
    assert prompt["subject"] is None
    assert prompt["tag"] == "mowdir_entry1_run_failed"
    assert prompt["actions"] == ["retry", "skip_plan", "abort"]
    assert prompt["status"] == "pending"
    assert prompt["sent_at"] == at(62).isoformat()
    assert prompt["next_reminder_at"] == at(62 + 3600).isoformat()
    ctx = prompt["context"]
    assert ctx["zone_name"] == "Front Lawn" and ctx["plan_id"] == "p1"
    assert (ctx["n"], ctx["m"], ctx["height_mm"]) == (1, 1, 70)
    assert ctx["reason"] == "did_not_start"
    [sent] = events(fx, "prompt_sent")
    assert sent.payload["prompt_id"] == "PROMPT1"
    assert sent.payload["kind"] == "run_failed"


def failed_with_prompt(n=1):
    env = prompt_env()
    state = initial_core_state()
    plan, runs = make_plan(n=n)
    state, _ = step(state, AddPlan(at(0), plan, runs, start=True), env)
    state, _ = step(state, SnapshotReceived(at(0), DOCKED), env)
    state, _ = step(state, TimerFired(at(61)), env)
    state, _ = step(state, CommandOutcome(at(62), "p1-r1", "start_run", Refused("no")), env)
    return state, env


@pytest.mark.parametrize(
    ("answer", "expected_status"),
    [
        (lambda pid: RetryRun(at(100)), "waiting"),
        (lambda pid: SkipPlan(at(100)), "idle"),
        (lambda pid: CancelSchedule(at(100)), "idle"),
        (lambda pid: AnswerPrompt(at(100), pid, "retry"), "waiting"),
        (lambda pid: AnswerPrompt(at(100), pid, "skip_plan"), "idle"),
        (lambda pid: AnswerPrompt(at(100), pid, "abort"), "idle"),
    ],
)
def test_every_answer_path_answers_the_failure_prompt_once(answer, expected_status):
    state, env = failed_with_prompt()
    state, fx = step(state, answer("PROMPT1"), env)
    assert prompts(state) == []
    assert state["state"]["schedule"]["status"] == expected_status
    [answered] = events(fx, "prompt_answered")
    assert answered.payload["prompt_id"] == "PROMPT1"
    assert answered.payload["kind"] == "run_failed"
    assert answered.payload["action"] in {"retry", "skip_plan", "abort"}


def test_answering_a_prompt_twice_changes_nothing():
    state, env = failed_with_prompt()
    state, _ = step(state, AnswerPrompt(at(100), "PROMPT1", "retry"), env)
    after, fx = step(state, AnswerPrompt(at(101), "PROMPT1", "skip_plan"), env)
    assert after == state and not fx


def test_answering_with_an_action_the_prompt_does_not_offer_changes_nothing():
    state, env = failed_with_prompt()
    after, fx = step(state, AnswerPrompt(at(100), "PROMPT1", "resume"), env)
    assert after == state and not fx


def test_a_new_prompt_of_same_kind_and_subject_supersedes_the_old_one():
    state, env = failed_with_prompt()
    state, fx = step(state, RaisePrompt(at(200), "gate", "g1", ["closed"], {}), env)
    state, fx = step(state, RaisePrompt(at(201), "gate", "g1", ["closed"], {}), env)
    gate = [p for p in prompts(state) if p["kind"] == "gate"]
    assert [p["prompt_id"] for p in gate] == ["PROMPT3"]
    assert gate[0]["tag"] == "mowdir_entry1_gate_g1"
    # an older prompt of the same kind is stale: answering it is a no-op
    after, fx = step(state, AnswerPrompt(at(202), "PROMPT2", "closed"), env)
    assert after == state and not fx


def test_prompts_of_different_kinds_or_subjects_coexist():
    state, env = failed_with_prompt()
    state, _ = step(state, RaisePrompt(at(200), "gate", "g1", ["closed"], {}), env)
    state, _ = step(state, RaisePrompt(at(201), "gate", "g2", ["closed"], {}), env)
    assert [p["tag"] for p in prompts(state)] == [
        "mowdir_entry1_run_failed",
        "mowdir_entry1_gate_g1",
        "mowdir_entry1_gate_g2",
    ]


def test_a_second_failure_after_retry_leaves_a_single_failure_prompt():
    state, env = failed_with_prompt()
    state, _ = step(state, RetryRun(at(100)), env)
    state, _ = step(state, TimerFired(at(200)), env)  # re-evaluate; checks pass
    state, _ = step(state, SnapshotReceived(at(201), DOCKED), env)
    state, _ = step(state, TimerFired(at(262)), env)
    state, _ = step(state, CommandOutcome(at(263), "p1-r1", "start_run", Refused("no")), env)
    assert [p["prompt_id"] for p in prompts(state)] == ["PROMPT2"]
