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
    NewError,
    Refused,
    SelfCheckReason,
    Unconfirmed,
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


# ---------------------------------------------------------------------------
# Schedule controls (spec §6.3, §6.4): Pause, Stop now, Resume, Retry, Skip Plan
# ---------------------------------------------------------------------------

from custom_components.mowing_director.core import (  # noqa: E402
    PauseSchedule,
    ResumeSchedule,
    RetryRun,
    SkipPlan,
    StopNow,
)


def paused_failed_state(n=2, *, pause_requested=False):
    """Schedule `paused` after the head Run failed ("did not start")."""
    state = starting_state(n)
    if pause_requested:
        state, _ = step(state, PauseSchedule(at(62)))
    state, _ = step(state, TimerFired(at(400)))
    assert run_status(state)[0] == "failed"
    return state


def sched(state):
    return state["state"]["schedule"]


def test_pause_while_waiting_pauses_at_once_and_never_commands():
    state = waiting_state()
    state, fx = step(state, PauseSchedule(at(10)))
    assert sched(state)["status"] == "paused"
    assert sched(state)["pause_requested"] is True
    assert "stability" not in state["state"]["deadlines"]
    assert not [e for e in fx if isinstance(e, (AbortRunEffect, StartRunEffect))]
    assert run_status(state) == ["pending"]
    # paused Schedule does not start the Run however long it waits
    state, fx = step(state, TimerFired(at(500)))
    assert run_status(state) == ["pending"]


@pytest.mark.parametrize("make", [starting_state, mowing_state])
def test_pause_while_running_only_sets_flag_then_pauses_when_run_ends(make):
    state = make(n=2)
    state, fx = step(state, PauseSchedule(at(100)))
    assert sched(state)["status"] == "running"
    assert sched(state)["pause_requested"] is True
    assert fx == []
    state, _ = step(state, SnapshotReceived(at(110), MOWING))
    state, fx = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")))
    assert sched(state)["status"] == "paused"
    assert sched(state)["pause_requested"] is True
    assert run_status(state) == ["pending"]
    assert not [e for e in fx if isinstance(e, AbortRunEffect)]


def test_run_done_with_pause_requested_and_schedule_empty_goes_idle():
    state = mowing_state(n=1)
    state, _ = step(state, PauseSchedule(at(100)))
    state, fx = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")))
    assert sched(state)["status"] == "idle"
    assert sched(state)["pause_requested"] is False
    assert events(fx, "schedule_completed")


@pytest.mark.parametrize("status", ["idle", "paused"])
def test_pause_is_a_no_op_when_idle_or_paused(status):
    state = initial_core_state() if status == "idle" else step(
        waiting_state(), PauseSchedule(at(5)))[0]
    after, fx = step(state, PauseSchedule(at(20)))
    assert sched(after)["status"] == status and fx == []


@pytest.mark.parametrize("make", [starting_state, mowing_state])
def test_stop_now_returns_active_run_to_head_pending_pauses_and_aborts(make):
    state = make(n=3)
    expected_from = state["state"]["runs"][0]["status"]
    state, fx = step(state, StopNow(at(200)))
    assert run_status(state) == ["pending", "pending", "pending"]
    assert state["state"]["runs"][0]["id"] == "p1-r1"
    assert sched(state)["status"] == "paused"
    assert state["state"]["active_run_id"] is None
    assert state["state"]["deadlines"] == {}
    assert [type(e).__name__ for e in fx if isinstance(e, AbortRunEffect)] == ["AbortRunEffect"]
    assert state["state"]["runs"][0].get("job_ref") is None
    changes = [(e.payload["from"], e.payload["to"]) for e in events(fx, "run_status_changed")]
    assert changes == [(expected_from, "pending")]


def test_stop_now_is_not_a_failure_and_run_is_not_archived():
    state = mowing_state()
    state, _ = step(state, StopNow(at(200)))
    assert state["history"]["runs"] == []
    assert plan_status(state, "p1") is PlanStatus.ACTIVE


def test_stop_now_from_on_hold():
    state = mowing_state(n=2)
    state["state"]["runs"][0]["status"] = "on_hold"
    state, fx = step(state, StopNow(at(300)))
    assert run_status(state) == ["pending", "pending"]
    assert sched(state)["status"] == "paused"
    assert [e for e in fx if isinstance(e, AbortRunEffect)]


@pytest.mark.parametrize("make", [waiting_state, lambda: initial_core_state()])
def test_stop_now_without_active_run_is_a_no_op(make):
    state = make()
    after, fx = step(state, StopNow(at(5)))
    assert after == state and fx == []


def test_resume_from_paused_goes_waiting_and_clears_pause_requested():
    state, _ = step(waiting_state(), PauseSchedule(at(5)))
    state, fx = step(state, ResumeSchedule(at(10)))
    assert sched(state)["status"] == "waiting"
    assert sched(state)["pause_requested"] is False
    state, fx = step(state, TimerFired(at(80)))  # Checks stable again: starts
    assert run_status(state) == ["starting"]


def test_start_from_paused_also_clears_pause_requested():
    state, _ = step(waiting_state(), PauseSchedule(at(5)))
    state, _ = step(state, StartSchedule(at(10)))
    assert sched(state)["status"] == "waiting"
    assert sched(state)["pause_requested"] is False


def test_resume_after_stop_now_restarts_the_stopped_run():
    state = mowing_state()
    state, _ = step(state, StopNow(at(200)))
    state, _ = step(state, ResumeSchedule(at(210)))
    assert sched(state)["status"] == "waiting"
    state, _ = step(state, SnapshotReceived(at(215), DOCKED))
    state, fx = step(state, TimerFired(at(280)))
    assert run_status(state) == ["starting"]
    assert [e for e in fx if isinstance(e, StartRunEffect)]


def test_resume_is_refused_while_a_failed_run_awaits_an_answer():
    state = paused_failed_state()
    after, fx = step(state, ResumeSchedule(at(500)))
    assert after == state and fx == []


@pytest.mark.parametrize("make", [waiting_state, mowing_state])
def test_resume_when_not_paused_is_a_no_op(make):
    state = make()
    after, fx = step(state, ResumeSchedule(at(500)))
    assert after == state and fx == []


def test_retry_puts_failed_run_back_at_head_and_schedule_waiting():
    state = paused_failed_state()
    state, fx = step(state, RetryRun(at(500)))
    assert run_status(state) == ["pending", "pending"]
    assert state["state"]["runs"][0]["id"] == "p1-r1"
    assert sched(state)["status"] == "waiting"
    assert not [e for e in fx if isinstance(e, (StartRunEffect, AbortRunEffect))]


def test_retry_leaves_schedule_paused_when_pause_requested():
    state = paused_failed_state(pause_requested=True)
    assert sched(state)["pause_requested"] is True
    state, _ = step(state, RetryRun(at(500)))
    assert run_status(state) == ["pending", "pending"]
    assert sched(state)["status"] == "paused"
    state, _ = step(state, TimerFired(at(900)))
    assert run_status(state) == ["pending", "pending"]  # still not started


def test_skip_plan_skips_other_runs_and_goes_waiting_when_runs_left():
    state = paused_failed_state(n=2)
    plan, runs = make_plan("p2", n=1)
    state, _ = step(state, AddPlan(at(450), plan, runs))
    state, fx = step(state, SkipPlan(at(500)))
    assert sched(state)["status"] == "waiting"
    assert [r["id"] for r in state["state"]["runs"]] == ["p2-r1"]
    assert sorted(r["status"] for r in state["history"]["runs"]) == ["failed", "skipped"]
    assert not [e for e in fx if isinstance(e, AbortRunEffect)]


def test_skip_plan_goes_idle_when_nothing_left():
    state = paused_failed_state(n=2)
    state, _ = step(state, SkipPlan(at(500)))
    assert sched(state)["status"] == "idle"
    assert state["state"]["runs"] == []


def test_skip_plan_leaves_schedule_paused_when_pause_requested():
    state = paused_failed_state(n=2, pause_requested=True)
    plan, runs = make_plan("p2", n=1)
    state, _ = step(state, AddPlan(at(450), plan, runs))
    state, _ = step(state, SkipPlan(at(500)))
    assert sched(state)["status"] == "paused"
    assert [r["id"] for r in state["state"]["runs"]] == ["p2-r1"]


def test_retry_and_skip_without_failed_run_are_no_ops():
    state = mowing_state()
    for event in (RetryRun(at(900)), SkipPlan(at(900))):
        after, fx = step(state, event)
        assert after == state and fx == []
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
