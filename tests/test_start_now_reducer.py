"""Reducer table tests for Start now (issue #99, ADR 0004).

Start now skips what is left of the 60 s stability window. It starts the head
Run through the normal start path, and it never bypasses a failing Check.
"""

from dataclasses import replace

from custom_components.groundskeeper.core import (
    AddPlan,
    PauseSchedule,
    StartNow,
    StartRunEffect,
    TimerFired,
    initial_core_state,
    stability_window_ends,
)
from tests.test_checks_reducer import custom, env_with, failing_ids, start
from tests.test_core_reducer import (
    DOCKED,
    at,
    make_plan,
    run_status,
    starting_state,
    step,
    waiting_state,
)


def start_commands(effects):
    return [e for e in effects if isinstance(e, StartRunEffect)]


def test_start_now_inside_the_window_starts_the_run_at_once_with_one_command():
    state = waiting_state()
    assert state["state"]["deadlines"]["stability"] == at(60).isoformat()
    state, fx = step(state, StartNow(at(10)))
    s = state["state"]
    assert s["schedule"]["status"] == "running"
    assert run_status(state) == ["starting"]
    assert "stability" not in s["deadlines"]
    assert len(start_commands(fx)) == 1
    # The normal start path: write-ahead intent and the start window.
    assert s["intent"] == {
        "command": "start_run", "run_id": "p1-r1", "issued_at": at(10).isoformat()}
    assert s["command_log"][-1]["run_id"] == "p1-r1"
    assert s["deadlines"]["start_window"] == at(10 + 300).isoformat()


def test_start_now_sends_the_same_command_the_timer_would_have_sent():
    by_timer, timer_fx = step(waiting_state(), TimerFired(at(61)))
    by_hand, hand_fx = step(waiting_state(), StartNow(at(61)))
    assert start_commands(hand_fx) == start_commands(timer_fx)
    assert by_hand["state"]["runs"] == by_timer["state"]["runs"]


def test_the_command_is_never_sent_again_after_start_now():
    state, _ = step(waiting_state(), StartNow(at(10)))
    state, fx = step(state, StartNow(at(11)))
    assert not start_commands(fx)
    state, fx = step(state, TimerFired(at(61)))
    assert not start_commands(fx)
    assert run_status(state) == ["starting"]


def test_start_now_with_a_failing_check_does_nothing():
    state = waiting_state(snapshot=replace(DOCKED, battery=14))
    assert state["state"]["waiting_on"]
    after, fx = step(state, StartNow(at(10)))
    assert not start_commands(fx)
    assert after == state
    assert after["state"]["schedule"]["status"] == "waiting"


def test_start_now_reads_the_checks_at_its_own_time_not_the_last_tick():
    # The Checks passed when the window opened; the Mower has since gone busy
    # and the reducer has not seen the change yet. Start now still reads them.
    state = waiting_state()
    state["state"]["snapshot"]["job_active"] = True
    after, fx = step(state, StartNow(at(10)))
    assert not start_commands(fx)
    assert after["state"]["schedule"]["status"] == "waiting"
    assert "stability" not in after["state"]["deadlines"]
    assert {"check": "mower_ready", "reason": "job_active"} in after["state"]["waiting_on"]


def test_start_now_does_not_bypass_work_hours_that_closed_since_the_window_opened():
    env = env_with(work_hours={"wed": ["12:00", "12:30"]})  # T0 is Wed 12:00
    state, _ = start(env)
    assert state["state"]["deadlines"]["stability"] == at(60).isoformat()
    after, fx = step(state, StartNow(at(31 * 60)), env)  # 12:31, nobody ticked yet
    assert not start_commands(fx)
    assert after["state"]["schedule"]["status"] == "waiting"
    assert {"check": "work_hours", "reason": "outside_work_hours"} in after["state"]["waiting_on"]


def test_start_now_does_not_bypass_a_custom_check_that_started_failing():
    env = env_with(custom("c1"), results={"c1": None})
    state, _ = start(env)
    assert state["state"]["deadlines"]["stability"] == at(60).isoformat()
    failing = replace(env, custom_results={"c1": "false"})
    after, fx = step(state, StartNow(at(10)), failing)
    assert not start_commands(fx)
    assert failing_ids(after) == {"c1": "false"}


def test_start_now_while_running_does_nothing():
    state = starting_state()
    after, fx = step(state, StartNow(at(70)))
    assert after == state and not fx


def test_start_now_while_paused_does_nothing():
    paused, _ = step(waiting_state(), PauseSchedule(at(2)))
    assert paused["state"]["schedule"]["status"] == "paused"
    after, fx = step(paused, StartNow(at(3)))
    assert after == paused and not fx


def test_start_now_before_the_window_opens_does_nothing():
    # No snapshot yet, so no Check has been evaluated and no window is armed.
    state, _ = step(initial_core_state(), AddPlan(at(0), *make_plan(), start=True))
    assert state["state"]["schedule"]["status"] == "waiting"
    assert "stability" not in state["state"]["deadlines"]
    after, fx = step(state, StartNow(at(5)))
    assert after == state and not fx


def test_stability_window_ends_reports_the_open_window_only():
    assert stability_window_ends(waiting_state()["state"]) == at(60)
    failing = waiting_state(snapshot=replace(DOCKED, battery=14))
    assert stability_window_ends(failing["state"]) is None
    assert stability_window_ends(starting_state()["state"]) is None
