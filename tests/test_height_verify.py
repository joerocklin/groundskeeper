"""Blade height verification (spec §7.6), reducer level.

Readback series are built from the ExampleSite evidence: the 10-18 Run (readback
stays at 80 mm = 3.1496 in), the reconnect blip (unknown, then a false
100 mm) and the 10-19 2748 lift fault (80 <-> 100 swing while mowing).
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity

from custom_components.groundskeeper.core import Env, SnapshotReceived, TimerFired
from tests.test_core_reducer import (
    DOCKED,
    ENV,
    MOWING,
    at,
    events,
    make_plan,
    mowing_state,
    run_status,
    starting_state,
    step,
)

HENV = Env(
    zones=ENV.zones,
    min_start_battery_pct=15,
    height_readback_resolution_mm=5,
    height_verify_window=timedelta(minutes=10),
)
START = 70  # commanded mm in make_plan


def reading(mm, available=True):
    return replace(MOWING, blade_height_mm=mm, available=available)


def enter_mowing(mm=80):
    """Run enters `mowing` at t=70 s, first readback `mm`."""
    state = starting_state()
    state, fx = step(state, SnapshotReceived(at(70), reading(mm)), HENV)
    return state, fx


def run_of(state):
    return state["state"]["runs"][0]


def feed(state, series, env=HENV):
    """series: (seconds, mm|None, available). Returns state, effects list."""
    out = []
    for t, mm, avail in series:
        state, fx = step(state, SnapshotReceived(at(t), reading(mm, avail)), env)
        state, fx2 = step(state, TimerFired(at(t)), env)
        out += fx + fx2
    return state, out


def test_entering_mowing_arms_ten_minute_window():
    state, _ = enter_mowing()
    assert state["state"]["deadlines"]["height_verify"] == at(70 + 600).isoformat()
    assert run_of(state).get("height_check") is None


def test_in_tolerance_for_60s_verifies_and_records_readback():
    state, _ = enter_mowing(80)
    state, _ = feed(state, [(100, 70, True), (130, 70, True), (161, 70, True)])
    run = run_of(state)
    assert run["height_check"] == "verified"
    assert run["height_readback_mm"] == 70
    assert "height_verify" not in state["state"]["deadlines"]
    assert run_status(state) == ["mowing"]


def test_one_resolution_step_off_is_still_in_tolerance():
    state, _ = enter_mowing(80)
    state, _ = feed(state, [(100, 75, True), (161, 65, True)])
    assert run_of(state)["height_check"] == "verified"


def test_not_held_60s_is_not_verified_and_leaving_tolerance_resets():
    state, _ = enter_mowing(80)
    state, _ = feed(state, [(100, 70, True), (150, 80, True), (180, 70, True), (230, 70, True)])
    assert run_of(state).get("height_check") is None
    state, _ = feed(state, [(241, 70, True)])
    assert run_of(state)["height_check"] == "verified"


def test_transit_height_then_lowering_verifies_after_the_window_starts():
    # ~90 mm in transit, lowering to cut height 10-90 s after starting.
    state, _ = enter_mowing(90)
    state, _ = feed(state, [(100, 90, True), (130, 70, True), (190, 70, True)])
    assert run_of(state)["height_check"] == "verified"


def test_reconnect_blip_is_ignored_while_unavailable_and_60s_after():
    state, _ = enter_mowing(80)
    # in tolerance, then the entity goes unavailable and flips to a false 100
    # that happens to be... off target; then returns to 70 mm.
    state, _ = feed(state, [(100, 70, True), (110, None, False), (115, 100, True),
                           (140, 70, True)])
    assert run_of(state).get("height_check") is None
    # 60 s after becoming available at t=115 -> ignored until 175; stability
    # then needs 60 s from the first usable in-tolerance reading.
    state, _ = feed(state, [(176, 70, True)])
    assert run_of(state).get("height_check") is None
    state, _ = feed(state, [(237, 70, True)])
    assert run_of(state)["height_check"] == "verified"


def test_false_blip_in_tolerance_does_not_verify_for_a_100mm_command():
    # commanded 100 mm: the blip's false 100 must not count as a reading.
    state = starting_state()
    plan_run = state["state"]["runs"][0]
    plan_run["settings"]["blade_height_mm"] = 100
    state, _ = step(state, SnapshotReceived(at(70), reading(80)), HENV)
    state, _ = feed(state, [(100, None, False), (101, 100, True), (130, 100, True),
                            (150, 100, True)])
    assert run_of(state).get("height_check") is None  # all ignored (<60 s after)


def test_window_expiry_with_readings_never_stable_fails_height_not_reached():
    state, _ = enter_mowing(80)
    state, fx = feed(state, [(100, 80, True), (400, 80, True), (671, 80, True)])
    run = run_of(state)
    assert run["status"] == "failed" and run["reason"] == "height_not_reached"
    assert run["height_check"] == "not_reached"
    assert any(type(e).__name__ == "AbortRunEffect" for e in fx)
    assert state["state"]["schedule"]["status"] == "paused"
    assert "height_verify" not in state["state"]["deadlines"]


def test_2748_swing_80_100_fails_height_not_reached():
    state, _ = enter_mowing(80)
    series = []
    for i, t in enumerate(range(100, 700, 20)):
        series.append((t, 80 if i % 2 == 0 else 100, True))
    series.append((671, 80, True))
    state, fx = feed(state, sorted(series))
    run = run_of(state)
    assert run["reason"] == "height_not_reached"
    assert run["height_check"] == "not_reached"
    assert any(type(e).__name__ == "AbortRunEffect" for e in fx)


def test_10_18_readback_stuck_at_80_for_a_79_command_is_within_one_step():
    # 79 mm commanded on 10-18; readback sat at 80 mm (3.1496 in).
    state = starting_state()
    state["state"]["runs"][0]["settings"]["blade_height_mm"] = 79
    state, _ = step(state, SnapshotReceived(at(70), reading(80)), HENV)
    state, _ = feed(state, [(100, 80, True), (161, 80, True)])
    assert run_of(state)["height_check"] == "verified"


def test_window_expiry_with_no_usable_reading_proceeds_unverified():
    state, _ = enter_mowing(None)
    state, fx = feed(state, [(100, None, True), (400, None, False), (671, None, True)])
    run = run_of(state)
    assert run["status"] == "mowing"
    assert run["height_check"] == "unverified"
    assert len(events(fx, "height_unverified")) == 1
    assert "height_verify" not in state["state"]["deadlines"]


def test_no_readback_declared_is_unverified_at_once_with_event():
    state = starting_state()
    state, fx = step(state, SnapshotReceived(at(70), MOWING))  # default Env: none
    run = run_of(state)
    assert run["height_check"] == "unverified"
    assert len(events(fx, "height_unverified")) == 1
    assert "height_verify" not in state["state"]["deadlines"]
    assert run_status(state) == ["mowing"]


def test_verified_run_is_not_monitored_afterwards():
    state, _ = enter_mowing(80)
    state, _ = feed(state, [(100, 70, True), (161, 70, True)])
    state, fx = feed(state, [(300, 100, True), (900, 100, True)])
    assert run_status(state) == ["mowing"]
    assert run_of(state)["height_check"] == "verified"


def test_reverify_when_run_reenters_mowing_resets_check_and_window():
    state, _ = enter_mowing(80)
    state, _ = feed(state, [(100, 70, True), (161, 70, True)])
    assert run_of(state)["height_check"] == "verified"
    # hold -> mowing re-entry is not reachable yet through the reducer inputs
    # of this slice; exercise the segment entry point directly.
    from custom_components.groundskeeper import core

    machine = core._Machine(state, HENV, at(2000))
    machine._begin_height_verify(machine.s["runs"][0], reading(80))
    assert machine.s["runs"][0]["height_check"] is None
    assert machine.s["deadlines"]["height_verify"] == at(2600).isoformat()
    # last stable readback of the previous segment is kept
    assert machine.s["runs"][0]["height_readback_mm"] == 70


def test_window_is_per_mower_tunable():
    env = replace(HENV, height_verify_window=timedelta(minutes=3))
    state = starting_state()
    state, _ = step(state, SnapshotReceived(at(70), reading(80)), env)
    assert state["state"]["deadlines"]["height_verify"] == at(250).isoformat()
    state, _ = feed(state, [(100, 80, True), (251, 80, True)], env)
    assert run_of(state)["reason"] == "height_not_reached"


def test_failure_prompt_explains_height_not_reached():
    state, _ = enter_mowing(80)
    state, _ = feed(state, [(100, 80, True), (671, 80, True)])
    prompts = state["state"]["prompts"]
    assert prompts and prompts[-1]["context"]["reason"] == "height_not_reached"


def test_stop_off_run_clears_height_deadlines():
    state, _ = enter_mowing(80)
    state, _ = feed(state, [(100, 70, True)])
    assert "height_stable" in state["state"]["deadlines"]
    state, _ = step(state, SnapshotReceived(at(1000), replace(MOWING, activity=LawnMowerActivity.IDLE, job_active=False)), HENV)
    state, _ = step(state, TimerFired(at(1061)), HENV)
    assert run_of(state)["reason"] == "stopped_off_dock"
    assert "height_verify" not in state["state"]["deadlines"]
    assert "height_stable" not in state["state"]["deadlines"]
