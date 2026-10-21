"""Run history and retention limits at the reducer seam (spec §14.3, §14.6).

Pure: inputs in, state out. Expected numbers come from the spec (oldest
pruned first, Plan leaves history with its last Run, limits never touch
`state`), not from the implementation.
"""

from dataclasses import replace

from custom_components.mowing_director.adapter import ZoneTaskComplete
from custom_components.mowing_director.core import (
    AnswerPrompt,
    SignalReceived,
    SkipPlan,
)
from tests.test_core_reducer import (
    ENV,
    at,
    mowing_state,
    paused_failed_state,
    step,
)


def old_run(plan_id, finished):
    return {
        "id": f"{plan_id}-r1", "plan_id": plan_id, "zone_id": "z1",
        "zone_name": "Front Lawn", "status": "done", "index": 1, "of": 1,
        "angle_deg": 0.0, "finished_at": finished, "started_at": finished,
    }


def seeded(max_runs):
    state = mowing_state()
    for i, plan_id in enumerate(["old1", "old2"]):
        state["history"]["runs"].append(old_run(plan_id, f"2026-01-0{i + 1}T00:00:00+00:00"))
        state["history"]["plans"][plan_id] = {"id": plan_id, "zone_id": "z1"}
    return state, replace(ENV, history_runs_max=max_runs)


def test_finishing_a_run_prunes_the_oldest_beyond_the_limit() -> None:
    state, env = seeded(2)
    state, _ = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")), env)
    assert [r["plan_id"] for r in state["history"]["runs"]] == ["old2", "p1"]


def test_a_plan_leaves_history_when_its_last_run_is_pruned() -> None:
    state, env = seeded(2)
    state, _ = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")), env)
    assert sorted(state["history"]["plans"]) == ["old2", "p1"]


def test_limit_zero_keeps_no_finished_runs_but_learns_durations() -> None:
    state, env = seeded(0)
    state, _ = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")), env)
    assert state["history"]["runs"] == [] and state["history"]["plans"] == {}
    assert len(state["learned"]["durations"]["z1"]) == 1  # separate limit


def test_default_limit_is_500() -> None:
    state = mowing_state()
    state["history"]["runs"] = [old_run(f"o{i}", f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00")
                                for i in range(500)]
    state["history"]["plans"] = {f"o{i}": {"id": f"o{i}"} for i in range(500)}
    state, _ = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")))
    assert len(state["history"]["runs"]) == 500
    assert state["history"]["runs"][0]["plan_id"] == "o1"


def test_duration_samples_per_zone_follow_the_limit() -> None:
    state = mowing_state()
    state["learned"]["durations"]["z1"] = [
        {"value": float(i), "native_ref": "11111111"} for i in range(5)
    ]
    env = replace(ENV, duration_samples_max=3)
    state, _ = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")), env)
    samples = state["learned"]["durations"]["z1"]
    assert len(samples) == 3 and samples[0]["value"] == 3.0


def test_duration_samples_limit_zero_keeps_none() -> None:
    state = mowing_state()
    env = replace(ENV, duration_samples_max=0)
    state, _ = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")), env)
    assert state["learned"]["durations"].get("z1", []) == []


def test_pruning_never_touches_state() -> None:
    state, env = seeded(0)
    before = state["state"]["command_log"]
    state, _ = step(state, SignalReceived(at(900), ZoneTaskComplete("11111111")), env)
    assert state["state"]["command_log"] == before[-20:]
    assert state["state"]["schedule"]["status"] == "idle"


def test_prompt_outcomes_are_kept_on_the_archived_run() -> None:
    state = paused_failed_state(n=1)
    (prompt,) = state["state"]["prompts"]
    state, _ = step(state, AnswerPrompt(at(500), prompt["prompt_id"], "skip_plan"))
    (run,) = state["history"]["runs"]
    (outcome,) = run["prompt_outcomes"]
    assert outcome["prompt_id"] == prompt["prompt_id"]
    assert outcome["kind"] == "run_failed" and outcome["action"] == "skip_plan"
    assert outcome["at"] == at(500).isoformat()


def test_prompt_outcome_lands_on_a_run_still_in_state() -> None:
    state = paused_failed_state(n=1)
    (prompt,) = state["state"]["prompts"]
    state, _ = step(state, AnswerPrompt(at(500), prompt["prompt_id"], "retry"))
    (run,) = [r for r in state["state"]["runs"] if r["id"] == prompt["context"]["run_id"]]
    assert [o["action"] for o in run["prompt_outcomes"]] == ["retry"]
