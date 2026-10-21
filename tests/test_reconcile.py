"""Reducer tests for restart reconciliation: the spec §8.1 decision table (#20).

Pure: a persisted core state goes in with a `Reconcile` input and the new
state and effects are asserted. The reducer never re-sends `start_run`.
"""

import copy
from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity

from custom_components.mowing_director.adapter import (
    JobStarted,
    Movement,
    NewError,
    Position,
)
from custom_components.mowing_director.core import (
    AbortRunEffect,
    Reconcile,
    SnapshotReceived,
    StartRunEffect,
    TimerFired,
)
from tests.test_core_reducer import (
    DOCKED,
    ENV,
    MOWING,
    at,
    events,
    mowing_state,
    run_status,
    starting_state,
    step,
    waiting_state,
)

LOAD = at(10_000)  # HA was down; the load happens long after the persisted state
FRONT = "11111111"
LIVE = replace(MOWING, zone=FRONT)


def reconcile(state, snapshot, *, signals=(), now=LOAD, loaded_at=None, env=ENV):
    return step(state, Reconcile(now, loaded_at or now, snapshot, tuple(signals)), env)


def commands(fx):
    return [e for e in fx if isinstance(e, (StartRunEffect, AbortRunEffect))]


def reconciled(fx):
    [event] = events(fx, "reconciled")
    return event.payload


def fresh_stall(state):
    """The persisted stall deadline has not expired (so only evidence decides)."""
    state["state"]["deadlines"]["stall"] = (LOAD + timedelta(minutes=30)).isoformat()
    return state


def run_of(state):
    return state["state"]["runs"][0]


def last_run_event(fx):
    return [e.payload for e in events(fx, "run_status_changed")][-1]


# -- starting ---------------------------------------------------------------


def test_starting_no_job_waits_out_remaining_window_and_never_resends():
    state = starting_state()  # window ends at at(61 + 300)
    state, fx = reconcile(state, DOCKED, now=at(100), loaded_at=at(100))
    assert run_status(state) == ["starting"]
    assert commands(fx) == []
    assert state["state"]["deadlines"]["start_window"] == at(361).isoformat()


def test_starting_no_job_fails_at_once_when_window_expired_with_abort():
    state = starting_state()
    state, fx = reconcile(state, DOCKED)
    assert run_status(state) == ["failed"]
    assert run_of(state)["reason"] == "did_not_start"
    assert [type(c) for c in commands(fx)] == [AbortRunEffect]


def test_starting_mowing_matching_zone_becomes_mowing_and_records_job_ref():
    state = starting_state()
    state["state"]["runs"][0]["native_ref"] = FRONT
    state, fx = reconcile(state, replace(LIVE, job_ref="j9"))
    assert run_status(state) == ["mowing"]
    assert run_of(state)["job_ref"] == "j9"
    assert commands(fx) == []


def test_starting_mowing_zone_mismatch_is_superseded_without_command():
    state = starting_state()
    state, fx = reconcile(state, replace(LIVE, zone="99999999"))
    assert run_status(state) == ["failed"]
    assert run_of(state)["reason"] == "superseded"
    assert commands(fx) == []


def test_starting_job_started_before_the_intent_is_superseded():
    state = starting_state()
    early = JobStarted("j0", FRONT, at(5))  # intent was issued at at(61)
    state, fx = reconcile(state, LIVE, signals=[early])
    assert run_of(state)["reason"] == "superseded"
    assert commands(fx) == []


# -- mowing: missed signals -----------------------------------------------------


def test_mowing_newer_error_fails_without_command():
    state = mowing_state()
    state["state"]["last_error_time"] = at(50).isoformat()
    error = NewError("1004", "stuck", at(5000))
    state, fx = reconcile(state, LIVE, signals=[error])
    assert run_of(state)["status"] == "failed"
    assert run_of(state)["error"]["code"] == "1004"
    assert commands(fx) == []


def test_mowing_old_error_is_ignored():
    state = mowing_state()
    state["state"]["last_error_time"] = at(5000).isoformat()
    fresh_stall(state)
    state, _ = reconcile(state, LIVE, signals=[NewError("1004", "x", at(5000))])
    assert run_status(state) == ["mowing"]


def test_mowing_different_job_ref_is_superseded_without_command():
    state = mowing_state()  # recorded job_ref j1
    state["state"]["runs"][0]["native_ref"] = FRONT
    state, fx = reconcile(state, replace(LIVE, job_ref="other"))
    assert run_of(state)["reason"] == "superseded"
    assert commands(fx) == []
    assert reconciled(fx)["job_check"] == "job_ref"


def test_mowing_without_job_ref_falls_back_to_zone_and_flags_weaker_check():
    state = mowing_state()
    run = state["state"]["runs"][0]
    run["native_ref"], run["job_ref"] = FRONT, None
    state, fx = reconcile(state, replace(LIVE, job_ref=None, zone="99999999"))
    assert run_of(state)["reason"] == "superseded"
    assert reconciled(fx)["job_check"] == "zone"


def test_mowing_matching_job_ref_stays_mowing():
    state = mowing_state()
    state["state"]["runs"][0]["native_ref"] = FRONT
    fresh_stall(state)
    state, _ = reconcile(state, LIVE)
    assert run_status(state) == ["mowing"]


# -- mowing: finished while away --------------------------------------------------


def test_returning_no_job_needs_a_fresh_60s_window_then_done_and_schedule_advances():
    state = mowing_state()
    state["state"]["deadlines"]["done_stable"] = at(80).isoformat()  # long expired
    returning = replace(DOCKED, activity=LawnMowerActivity.RETURNING)
    state, fx = reconcile(state, returning)
    assert run_status(state) == ["mowing"]  # persisted deadline does not count
    assert state["state"]["deadlines"]["done_stable"] == (LOAD + timedelta(seconds=60)).isoformat()
    state, fx = step(state, TimerFired(LOAD + timedelta(seconds=61)))
    assert state["state"]["runs"] == []
    assert state["state"]["schedule"]["status"] == "idle"
    assert commands(fx) == []


def test_done_after_downtime_advances_through_checks_when_runs_remain():
    state = mowing_state(n=2)
    state, fx = reconcile(state, replace(DOCKED, activity=LawnMowerActivity.DOCKED))
    state, fx = step(state, SnapshotReceived(LOAD + timedelta(seconds=61), DOCKED))
    assert state["state"]["schedule"]["status"] == "waiting"
    assert run_status(state) == ["pending"]


def test_idle_no_job_fails_stopped_off_dock_after_fresh_window_without_command():
    state = mowing_state()
    idle = replace(DOCKED, activity=LawnMowerActivity.IDLE)
    state, _ = reconcile(state, idle)
    assert run_status(state) == ["mowing"]
    state, fx = step(state, TimerFired(LOAD + timedelta(seconds=61)))
    assert run_of(state)["reason"] == "stopped_off_dock"
    assert commands(fx) == []


# -- mowing: stall -----------------------------------------------------------------


def moved(snapshot):
    return replace(snapshot, movement=Movement(progress_pct=40, position=Position(1.0, 2.0)))


def test_still_mowing_with_changed_movement_resets_stall_clock_from_load():
    state = mowing_state()
    state["state"]["runs"][0]["native_ref"] = FRONT
    state["state"]["deadlines"]["stall"] = at(100).isoformat()  # expired
    state, fx = reconcile(state, moved(LIVE))
    assert run_status(state) == ["mowing"]
    assert commands(fx) == []
    assert state["state"]["deadlines"]["stall"] == (LOAD + ENV.stall_window).isoformat()


def test_still_mowing_unchanged_movement_and_expired_stall_aborts_and_fails():
    state = mowing_state()
    state["state"]["runs"][0]["native_ref"] = FRONT
    state["state"]["deadlines"]["stall"] = at(100).isoformat()
    unchanged = replace(LIVE, movement=Movement(**{
        "progress_pct": state["state"]["stall_movement"]["progress_pct"],
        "area_m2": state["state"]["stall_movement"]["area_m2"],
    }))
    state, fx = reconcile(state, unchanged)
    assert run_of(state)["reason"] == "stalled"
    assert [type(c) for c in commands(fx)] == [AbortRunEffect]


# -- height verify -------------------------------------------------------------------


def test_open_height_verify_is_tested_against_live_readback_and_rearmed():
    env = replace(ENV, height_readback_resolution_mm=5)
    state = starting_state()
    state["state"]["runs"][0]["native_ref"] = FRONT
    state, _ = step(state, SnapshotReceived(at(70), LIVE), env)
    assert state["state"]["height_verify"] is not None
    state["state"]["deadlines"]["height_verify"] = at(200).isoformat()  # expired
    state["state"]["deadlines"]["height_stable"] = at(250).isoformat()
    fresh_stall(state)
    state, fx = reconcile(state, LIVE, env=env)  # blade_height_mm 70 = target 70
    assert run_status(state) == ["mowing"]
    assert commands(fx) == []
    deadlines = state["state"]["deadlines"]
    assert deadlines["height_verify"] == (LOAD + env.height_verify_window).isoformat()
    assert deadlines["height_stable"] == (LOAD + timedelta(seconds=60)).isoformat()


# -- on hold ---------------------------------------------------------------------------


def held_state(cause="contact_lost"):
    state = mowing_state()
    run = state["state"]["runs"][0]
    run["native_ref"], run["status"], run["on_hold_reason"] = FRONT, "on_hold", cause
    state["state"]["deadlines"].clear()
    state["state"]["prompts"] = [{
        "prompt_id": "pr1", "kind": "run_on_hold", "subject": None, "tag": "t",
        "actions": ["resume", "end_run"], "context": {"run_id": run["id"], "plan_id": "p1"},
        "sent_at": at(100).isoformat(), "next_reminder_at": at(3700).isoformat(),
        "status": "pending",
    }]
    return state


def test_on_hold_cause_cleared_returns_to_mowing_and_clears_the_prompt():
    state, fx = reconcile(held_state("contact_lost"), LIVE)
    assert run_status(state) == ["mowing"]
    assert state["state"]["prompts"] == []
    assert commands(fx) == []


def test_on_hold_cause_persists_stays_and_expired_reminder_fires_once():
    state = held_state("recharge_stall")
    state, fx = reconcile(state, replace(DOCKED, recharging=True))
    assert run_status(state) == ["on_hold"]
    assert [e.payload["prompt_id"] for e in events(fx, "prompt_reminder")] == ["pr1"]
    state, fx = reconcile(state, replace(DOCKED, recharging=True), now=LOAD + timedelta(hours=1))
    assert events(fx, "prompt_reminder") == []


def test_on_hold_with_a_different_live_job_is_superseded():
    state, _ = reconcile(held_state("contact_lost"), replace(LIVE, job_ref="other"))
    assert run_of(state)["reason"] == "superseded"


# -- schedule-level rows -------------------------------------------------------------------


def test_waiting_schedule_rechecks_and_arms_a_fresh_stability_window():
    state = waiting_state()
    state["state"]["deadlines"]["stability"] = at(5).isoformat()  # expired
    state, fx = reconcile(state, DOCKED)
    assert state["state"]["schedule"]["status"] == "waiting"
    assert commands(fx) == []  # evidence first: the expired window does not start a Run
    assert state["state"]["deadlines"]["stability"] == (LOAD + timedelta(seconds=60)).isoformat()


@pytest.mark.parametrize("status", ["idle", "paused"])
def test_idle_and_paused_schedules_are_unchanged(status):
    state = waiting_state()
    state["state"]["schedule"]["status"] = status
    before = copy.deepcopy(state["state"]["schedule"])
    state, fx = reconcile(state, DOCKED)
    assert state["state"]["schedule"] == before
    assert commands(fx) == []
    # (the docked battery has been flat for 2 h by LOAD: that Anomaly is a separate rule)
    assert not [e for e in events(fx, "prompt_sent") if e.payload["kind"] != "anomaly"]


# -- no usable snapshot ---------------------------------------------------------------------


def test_no_usable_snapshot_counts_contact_grace_from_load_then_holds():
    state = mowing_state()
    loaded = at(10_000)
    state, fx = reconcile(state, None, now=loaded + timedelta(minutes=5), loaded_at=loaded)
    assert run_status(state) == ["mowing"]  # HA downtime is not lost contact
    assert state["state"]["deadlines"]["contact_lost"] == (loaded + timedelta(minutes=10)).isoformat()
    assert reconciled(fx)["snapshot"] == "none"
    state, fx = step(state, TimerFired(loaded + timedelta(minutes=10, seconds=1)))
    assert run_of(state)["status"] == "on_hold"
    assert run_of(state)["on_hold_reason"] == "contact_lost"


# -- the reconciled event --------------------------------------------------------------------


def test_every_reconcile_fires_exactly_one_reconciled_event_with_before_and_after():
    state, fx = reconcile(starting_state(), replace(LIVE, zone="99999999"))
    payload = reconciled(fx)
    assert payload["before"] == {"schedule": "running", "run": "starting"}
    assert payload["after"] == {"schedule": "paused", "run": "failed"}
    assert "superseded" in payload["reasons"]
    assert len(events(fx, "reconciled")) == 1


def test_reconcile_never_mutates_its_input():
    state = mowing_state()
    frozen = copy.deepcopy(state)
    reconcile(state, DOCKED)
    assert state == frozen


def test_reconcile_with_nothing_to_do_still_reports():
    state, fx = reconcile(waiting_state(), DOCKED)
    assert len(events(fx, "reconciled")) == 1
