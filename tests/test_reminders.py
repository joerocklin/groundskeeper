"""Reducer tests for Prompt reminders and the Schedule summary (spec §12.4, §12.9, §12.11)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from custom_components.groundskeeper.core import (
    AnswerPrompt,
    ArmTimer,
    CancelTimer,
    Reconcile,
    TimerFired,
    initial_core_state,
    reduce,
)
from tests.test_core_reducer import ENV, events

DAY = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)


def clock(hour, minute=0, day=0):
    return DAY + timedelta(days=day, hours=hour, minutes=minute)


def env():
    ids = iter(f"P{i}" for i in range(1, 50))
    return replace(ENV, entry_id="e1", new_id=lambda: next(ids))


def step(state, event):
    red = reduce(state, event, env())
    return red.state, list(red.effects)


def failing_state(raised_at):
    """A state with one failure Prompt raised at `raised_at` (with reminder)."""
    from custom_components.groundskeeper.core import RaisePrompt

    state, _ = step(
        initial_core_state(),
        RaisePrompt(raised_at, "run_failed", None, ["retry", "skip_plan", "abort"], {}),
    )
    prompt = state["state"]["prompts"][0]
    prompt["next_reminder_at"] = (raised_at + timedelta(hours=1)).isoformat()
    return state


def reminder_deadline(state):
    return state["state"]["deadlines"].get("prompt_reminder")


def test_reminder_deadline_is_armed_for_the_earliest_reminder():
    state = failing_state(clock(12))
    state, fx = step(state, TimerFired(clock(12, 1)))
    assert reminder_deadline(state) == clock(13).isoformat()
    assert [e for e in fx if isinstance(e, ArmTimer)] == [
        ArmTimer("prompt_reminder", clock(13))
    ]


def test_first_reminder_at_one_hour_then_next_morning():
    state = failing_state(clock(12))
    state, _ = step(state, TimerFired(clock(12, 1)))
    state, fx = step(state, TimerFired(clock(13)))
    [rem] = events(fx, "prompt_reminder")
    assert rem.payload["prompt_id"] == "P1"
    assert state["state"]["prompts"][0]["next_reminder_at"] == clock(8, day=1).isoformat()
    assert reminder_deadline(state) == clock(8, day=1).isoformat()
    state, fx = step(state, TimerFired(clock(8, day=1)))
    assert len(events(fx, "prompt_reminder")) == 1
    assert state["state"]["prompts"][0]["next_reminder_at"] == clock(8, day=2).isoformat()


def test_a_reminder_falling_overnight_waits_for_the_morning():
    state = failing_state(clock(23, 30))
    state, _ = step(state, TimerFired(clock(23, 31)))
    state, fx = step(state, TimerFired(clock(0, 30, day=1)))
    assert events(fx, "prompt_reminder") == []
    assert state["state"]["prompts"][0]["next_reminder_at"] == clock(8, day=1).isoformat()
    state, fx = step(state, TimerFired(clock(8, day=1)))
    assert len(events(fx, "prompt_reminder")) == 1


def test_answering_cancels_the_reminder_deadline():
    state = failing_state(clock(12))
    state, _ = step(state, TimerFired(clock(12, 1)))
    state, fx = step(state, AnswerPrompt(clock(12, 5), "P1", "retry"))
    assert reminder_deadline(state) is None
    assert CancelTimer("prompt_reminder") in fx


def test_catch_up_after_downtime_sends_one_reminder_in_the_window():
    state = failing_state(clock(10))
    state, _ = step(state, TimerFired(clock(10, 1)))
    state, fx = step(state, Reconcile(clock(20), clock(20), None))
    assert len(events(fx, "prompt_reminder")) == 1
    assert state["state"]["prompts"][0]["next_reminder_at"] == clock(8, day=1).isoformat()


def test_catch_up_overnight_waits_for_the_morning():
    state = failing_state(clock(10))
    state, fx = step(state, Reconcile(clock(2, day=1), clock(2, day=1), None))
    assert events(fx, "prompt_reminder") == []
    assert state["state"]["prompts"][0]["next_reminder_at"] == clock(8, day=1).isoformat()


# -- Schedule completed summary (spec §12.9) ---------------------------------------


def test_schedule_completed_carries_a_summary_of_the_schedule():
    from custom_components.groundskeeper.adapter import ZoneTaskComplete
    from custom_components.groundskeeper.core import SignalReceived
    from tests.test_core_reducer import mowing_state, at

    state = mowing_state(n=1)  # started at T0, Run mowing since at(70)
    state, fx = step(state, SignalReceived(at(970), ZoneTaskComplete("11111111")))
    [done] = events(fx, "schedule_completed")
    assert done.payload["summary"] == {
        "plans": 1,
        "runs_done": 1,
        "runs_skipped": 0,
        "mowing_s": 900,
        "anomalies": 0,
    }
