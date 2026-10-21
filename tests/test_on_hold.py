"""On hold and rain stop at the reducer seam (spec §6.3, §7.4, §7.5).

Pure: inputs in, state and effects out. Expected values come from the spec
tables, not from the implementation.
"""

from dataclasses import replace

from homeassistant.components.lawn_mower import LawnMowerActivity

from custom_components.mowing_director.adapter import (
    Movement,
    SelfCheckReason,
)
from custom_components.mowing_director.core import SnapshotReceived, TimerFired
from tests.test_core_reducer import (
    MOWING,
    at,
    events,
    mowing_state,
    run_status,
    step,
)

PAUSED = replace(MOWING, activity=LawnMowerActivity.PAUSED)


def hold_prompts(state):
    return [p for p in state["state"]["prompts"] if p["kind"] == "run_on_hold"]


def on_hold_by_external_pause():
    state = mowing_state()  # mowing since t=70
    state, _ = step(state, SnapshotReceived(at(100), PAUSED))
    state, _ = step(state, TimerFired(at(161)))
    return state


# -- external pause --------------------------------------------------------


def test_external_pause_holds_run_only_after_stable_60s():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), PAUSED))
    assert run_status(state) == ["mowing"]
    state, _ = step(state, TimerFired(at(159)))
    assert run_status(state) == ["mowing"]
    state, fx = step(state, TimerFired(at(160)))
    assert run_status(state) == ["on_hold"]
    run = state["state"]["runs"][0]
    assert run["on_hold_reason"] == "external_pause"
    assert state["state"]["schedule"]["status"] == "running"
    assert [e.payload["to"] for e in events(fx, "run_status_changed")] == ["on_hold"]


def test_pause_flicker_does_not_hold():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), PAUSED))
    state, _ = step(state, SnapshotReceived(at(130), MOWING))
    state, _ = step(state, TimerFired(at(200)))
    assert run_status(state) == ["mowing"]


def test_pause_while_recharging_is_not_external_pause():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), replace(PAUSED, recharging=True)))
    state, _ = step(state, TimerFired(at(200)))
    assert run_status(state) == ["mowing"]


def test_pause_the_director_commanded_is_not_external():
    state = mowing_state()
    state["state"]["command_log"].append(
        {"command": "pause_run", "run_id": "p1-r1", "issued_at": at(90).isoformat(),
         "result": "accepted"}
    )
    state, _ = step(state, SnapshotReceived(at(100), PAUSED))
    state, _ = step(state, TimerFired(at(200)))
    assert run_status(state) == ["mowing"]


def test_external_pause_raises_on_hold_prompt_with_reminder():
    state = on_hold_by_external_pause()
    (prompt,) = hold_prompts(state)
    assert prompt["actions"] == ["resume", "end_run"]
    assert prompt["context"]["reason"] == "external_pause"
    assert prompt["context"]["run_id"] == "p1-r1"
    assert prompt["next_reminder_at"] == at(161 + 3600).isoformat()


def test_external_pause_clears_when_mowing_stable_60s_and_prompt_goes():
    state = on_hold_by_external_pause()
    state, _ = step(state, SnapshotReceived(at(300), MOWING))
    assert run_status(state) == ["on_hold"]
    state, _ = step(state, TimerFired(at(359)))
    assert run_status(state) == ["on_hold"]
    state, fx = step(state, TimerFired(at(360)))
    assert run_status(state) == ["mowing"]
    assert "on_hold_reason" not in state["state"]["runs"][0]
    assert hold_prompts(state) == []
    assert [e.type for e in events(fx, "prompt_answered")] == ["prompt_answered"]


def test_stall_clock_is_paused_while_on_hold_and_rearmed_on_return():
    state = on_hold_by_external_pause()
    assert "stall" not in state["state"]["deadlines"]
    state, _ = step(state, SnapshotReceived(at(300), MOWING))
    state, _ = step(state, TimerFired(at(360)))
    assert state["state"]["deadlines"]["stall"] == at(360 + 3600).isoformat()


def test_no_clock_fails_an_on_hold_run():
    state = on_hold_by_external_pause()
    state, _ = step(state, TimerFired(at(160 + 10 * 3600)))
    assert run_status(state) == ["on_hold"]
    assert state["state"]["schedule"]["status"] == "running"


# -- recharge stall --------------------------------------------------------

RECHARGING = replace(MOWING, activity=LawnMowerActivity.DOCKED, recharging=True, battery=20)


def recharging_state():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), RECHARGING))
    return state


def test_recharge_stall_holds_when_battery_not_risen_for_60_min():
    state = recharging_state()
    state, _ = step(state, SnapshotReceived(at(100 + 1800), RECHARGING))
    state, _ = step(state, TimerFired(at(100 + 3599)))
    assert run_status(state) == ["mowing"]
    state, _ = step(state, TimerFired(at(100 + 3600)))
    assert run_status(state) == ["on_hold"]
    assert state["state"]["runs"][0]["on_hold_reason"] == "recharge_stall"
    (prompt,) = hold_prompts(state)
    assert prompt["context"]["reason"] == "recharge_stall"


def test_battery_rise_restarts_the_recharge_stall_clock():
    state = recharging_state()
    state, _ = step(state, SnapshotReceived(at(2000), replace(RECHARGING, battery=21)))
    state, _ = step(state, TimerFired(at(100 + 3600)))
    assert run_status(state) == ["mowing"]
    state, _ = step(state, TimerFired(at(2000 + 3600)))
    assert run_status(state) == ["on_hold"]


def test_leaving_recharge_cancels_the_recharge_stall_clock():
    state = recharging_state()
    moved = replace(MOWING, movement=Movement(progress_pct=5))
    state, _ = step(state, SnapshotReceived(at(200), moved))
    state, _ = step(state, TimerFired(at(100 + 3600)))
    assert run_status(state) == ["mowing"]


def test_recharge_stall_hold_clears_when_battery_rises_and_stall_is_rearmed():
    state = recharging_state()
    state, _ = step(state, TimerFired(at(100 + 3600)))
    assert run_status(state) == ["on_hold"]
    state, _ = step(state, SnapshotReceived(at(4000), RECHARGING))
    assert run_status(state) == ["on_hold"]
    state, _ = step(state, SnapshotReceived(at(4100), replace(RECHARGING, battery=21)))
    assert run_status(state) == ["mowing"]
    assert hold_prompts(state) == []
    assert state["state"]["deadlines"]["stall"] == at(4100 + 3600).isoformat()
    # the recharge-stall clock starts again from the new level
    assert state["state"]["deadlines"]["recharge_stall"] == at(4100 + 3600).isoformat()


# -- contact lost ------------------------------------------------------------

UNAVAILABLE = replace(MOWING, available=False)
NO_ACTIVITY = replace(MOWING, activity=None)


def test_contact_lost_holds_after_10_min_of_unavailable():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), UNAVAILABLE))
    state, _ = step(state, TimerFired(at(100 + 599)))
    assert run_status(state) == ["mowing"]
    state, _ = step(state, TimerFired(at(100 + 600)))
    assert run_status(state) == ["on_hold"]
    assert state["state"]["runs"][0]["on_hold_reason"] == "contact_lost"
    assert hold_prompts(state)[0]["context"]["reason"] == "contact_lost"


def test_activity_none_counts_as_contact_lost():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), NO_ACTIVITY))
    state, _ = step(state, TimerFired(at(700)))
    assert run_status(state) == ["on_hold"]


def test_contact_back_within_grace_cancels_the_clock():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), UNAVAILABLE))
    state, _ = step(state, SnapshotReceived(at(300), MOWING))
    state, _ = step(state, TimerFired(at(800)))
    assert run_status(state) == ["mowing"]


def test_lost_contact_does_not_fail_run_as_stalled_while_in_grace():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(3500), UNAVAILABLE))
    state, _ = step(state, TimerFired(at(70 + 3600)))  # stall deadline due, contact lost
    assert run_status(state) == ["mowing"]


def test_contact_lost_hold_clears_when_contact_returns_and_rules_reevaluate():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), UNAVAILABLE))
    state, _ = step(state, TimerFired(at(700)))
    assert run_status(state) == ["on_hold"]
    state, _ = step(state, SnapshotReceived(at(900), MOWING))
    assert run_status(state) == ["mowing"]
    assert hold_prompts(state) == []
    assert state["state"]["deadlines"]["stall"] == at(900 + 3600).isoformat()


def test_contact_returning_with_a_finished_job_goes_on_to_done():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), UNAVAILABLE))
    state, _ = step(state, TimerFired(at(700)))
    finished = replace(MOWING, activity=LawnMowerActivity.DOCKED, job_active=False)
    state, _ = step(state, SnapshotReceived(at(900), finished))
    state, _ = step(state, TimerFired(at(960)))
    assert state["state"]["runs"] == []  # archived as done
    assert state["history"]["runs"][-1]["status"] == "done"


# -- device work hours --------------------------------------------------------

NON_WORK = replace(MOWING, self_check=SelfCheckReason.DEVICE_NON_WORK_HOURS)


def test_device_non_work_hours_holds_at_once_without_a_prompt():
    state = mowing_state()
    state, fx = step(state, SnapshotReceived(at(100), NON_WORK))
    assert run_status(state) == ["on_hold"]
    assert state["state"]["runs"][0]["on_hold_reason"] == "device_work_hours"
    assert hold_prompts(state) == []
    assert events(fx, "prompt_sent") == []


def test_device_work_hours_hold_clears_when_mower_resumes_by_itself():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), NON_WORK))
    state, _ = step(state, SnapshotReceived(at(5000), MOWING))
    state, _ = step(state, TimerFired(at(5060)))
    assert run_status(state) == ["mowing"]
    assert state["state"]["deadlines"]["stall"] == at(5060 + 3600).isoformat()


def test_device_work_hours_hold_stays_while_still_non_work_hours():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), NON_WORK))
    state, _ = step(state, TimerFired(at(100 + 86400)))
    assert run_status(state) == ["on_hold"]


# -- user actions ---------------------------------------------------------------

from custom_components.mowing_director.adapter import RainStop  # noqa: E402
from custom_components.mowing_director.core import (  # noqa: E402
    AbortRunEffect,
    AnswerPrompt,
    CancelSchedule,
    EndRun,
    ResumeRun,
    ResumeRunEffect,
    SignalReceived,
)


def test_resume_sends_resume_run_logs_it_and_run_stays_on_hold():
    state = on_hold_by_external_pause()
    state, fx = step(state, ResumeRun(at(200)))
    assert [e.run_id for e in fx if isinstance(e, ResumeRunEffect)] == ["p1-r1"]
    assert run_status(state) == ["on_hold"]
    assert state["state"]["command_log"][-1]["command"] == "resume_run"
    assert state["state"]["command_log"][-1]["run_id"] == "p1-r1"
    assert hold_prompts(state) == []  # answered
    assert [e.payload["action"] for e in events(fx, "prompt_answered")] == ["resume"]


def test_after_resume_the_hold_clears_only_when_the_cause_clears():
    state = on_hold_by_external_pause()
    state, _ = step(state, ResumeRun(at(200)))
    state, _ = step(state, SnapshotReceived(at(210), PAUSED))
    state, _ = step(state, TimerFired(at(300)))
    assert run_status(state) == ["on_hold"]
    state, _ = step(state, SnapshotReceived(at(310), MOWING))
    state, _ = step(state, TimerFired(at(370)))
    assert run_status(state) == ["mowing"]


def test_resume_without_an_on_hold_run_does_nothing():
    state = mowing_state()
    state, fx = step(state, ResumeRun(at(200)))
    assert not [e for e in fx if isinstance(e, ResumeRunEffect)]
    assert run_status(state) == ["mowing"]


def test_end_run_aborts_and_fails_run_ended_by_user():
    state = on_hold_by_external_pause()
    state, fx = step(state, EndRun(at(200)))
    assert [e.reason for e in fx if isinstance(e, AbortRunEffect)] == ["ended_by_user"]
    run = state["state"]["runs"][0]
    assert (run["status"], run["reason"]) == ("failed", "ended_by_user")
    assert state["state"]["schedule"]["status"] == "paused"
    assert state["state"]["active_run_id"] is None
    assert hold_prompts(state) == []
    kinds = [p["kind"] for p in state["state"]["prompts"]]
    assert kinds == ["run_failed"]
    assert state["state"]["deadlines"].keys().isdisjoint({"hold_clear_stable", "pause_stable"})


def test_end_run_without_on_hold_run_does_nothing():
    state = mowing_state()
    state, fx = step(state, EndRun(at(200)))
    assert run_status(state) == ["mowing"]
    assert not [e for e in fx if isinstance(e, AbortRunEffect)]


def test_on_hold_prompt_actions_route_to_resume_and_end_run():
    state = on_hold_by_external_pause()
    pid = hold_prompts(state)[0]["prompt_id"]
    after, fx = step(state, AnswerPrompt(at(200), pid, "resume"))
    assert [e for e in fx if isinstance(e, ResumeRunEffect)]
    pid = hold_prompts(state)[0]["prompt_id"]
    after, fx = step(state, AnswerPrompt(at(200), pid, "end_run"))
    assert run_status(after) == ["failed"]


def test_stale_on_hold_answer_is_ignored():
    state = on_hold_by_external_pause()
    pid = hold_prompts(state)[0]["prompt_id"]
    state, _ = step(state, SnapshotReceived(at(300), MOWING))
    state, _ = step(state, TimerFired(at(360)))
    after, fx = step(state, AnswerPrompt(at(400), pid, "end_run"))
    assert after == state and fx == []


def test_cancel_while_on_hold_aborts_and_clears_the_prompt():
    state = on_hold_by_external_pause()
    state, fx = step(state, CancelSchedule(at(200)))
    assert [e for e in fx if isinstance(e, AbortRunEffect)]
    assert hold_prompts(state) == []
    assert state["state"]["runs"] == []


# -- rain stop --------------------------------------------------------------------


def test_rain_stop_while_mowing_returns_run_pending_without_command_or_failure():
    state = mowing_state()
    state, fx = step(state, SignalReceived(at(200), RainStop()))
    run = state["state"]["runs"][0]
    assert run["status"] == "pending"
    assert state["state"]["schedule"]["status"] == "waiting"
    assert not [e for e in fx if isinstance(e, AbortRunEffect)]
    assert [p["kind"] for p in state["state"]["prompts"]] == []
    assert state["state"]["active_run_id"] is None
    assert state["state"]["deadlines"].keys().isdisjoint({"stall", "done_stable"})
    assert state["history"]["runs"] == []


def test_rain_stop_while_on_hold_clears_the_hold_and_its_prompt():
    state = on_hold_by_external_pause()
    state, fx = step(state, SignalReceived(at(200), RainStop()))
    run = state["state"]["runs"][0]
    assert run["status"] == "pending"
    assert "on_hold_reason" not in run
    assert hold_prompts(state) == []
    assert state["state"]["schedule"]["status"] == "waiting"
    assert not [e for e in fx if isinstance(e, AbortRunEffect)]


def test_rain_stop_with_no_active_run_changes_nothing():
    from tests.test_core_reducer import waiting_state

    state = waiting_state()
    after, _ = step(state, SignalReceived(at(5), RainStop()))
    assert run_status(after) == ["pending"]
    assert after["state"]["schedule"]["status"] == "waiting"


def test_resume_command_result_is_recorded_on_its_own_log_entry():
    from custom_components.mowing_director.adapter import Accepted
    from custom_components.mowing_director.core import CommandOutcome

    state = mowing_state()
    state["state"]["command_log"].append(  # an unanswered start_run entry
        {"command": "start_run", "run_id": "p1-r1", "issued_at": at(1).isoformat(), "result": None}
    )
    state = on_hold_by_external_pause_from(state)
    state, _ = step(state, ResumeRun(at(200)))
    state, _ = step(state, CommandOutcome(at(201), "p1-r1", "resume_run", Accepted()))
    log = state["state"]["command_log"]
    assert [(c["command"], c["result"]) for c in log[-2:]] == [
        ("start_run", None),
        ("resume_run", "accepted"),
    ]


def on_hold_by_external_pause_from(state):
    state, _ = step(state, SnapshotReceived(at(100), PAUSED))
    state, _ = step(state, TimerFired(at(161)))
    return state
