"""Gate open Check, Gate Prompt and crossings at the reducer seam (spec §9.2, §10)."""

from dataclasses import replace

from custom_components.groundskeeper.core import (
    AddPlan,
    AnswerPrompt,
    GateActionEffect,
    SetGateFlag,
    SnapshotReceived,
    StartRunEffect,
    TimerFired,
    initial_core_state,
)
from custom_components.groundskeeper.gates import normalise_gate
from tests.test_core_reducer import DOCKED, ENV, MOWING, at, events, make_plan, step

OPEN_ACTION = [{"action": "cover.open_cover", "target": {"entity_id": "cover.fence"}}]
CLOSE_ACTION = [{"action": "cover.close_cover", "target": {"entity_id": "cover.fence"}}]


def gate(**kw):
    return normalise_gate({"id": "g1", "name": "Fence gate", "channels": ["path1"], **kw})


def env_for(*gates, entity_states=None, check=None):
    env = replace(
        ENV,
        gates=tuple(gates),
        gated_zones={"z1": tuple(g["id"] for g in gates)},
        gate_entities=entity_states or {},
    )
    if check:
        env = replace(env, checks=tuple(
            {**r, **check} if r["type"] == "gate_open" else r for r in env.checks))
    return env


def begin(env, zone="z1"):
    state, _ = step(initial_core_state(), AddPlan(at(0), *make_plan(zone=zone), start=True), env)
    state, fx = step(state, SnapshotReceived(at(0), DOCKED), env)
    return state, fx


def prompts(state, kind="gate"):
    return [p for p in state["state"]["prompts"] if p["kind"] == kind]


def failing(state):
    return [(w["check"], w["reason"]) for w in state["state"]["waiting_on"]
            if w["check"] == "gate_open"]


def answer(state, env, action, t=5):
    [prompt] = prompts(state)
    return step(state, AnswerPrompt(at(t), prompt["prompt_id"], action), env)


def test_closed_gate_fails_the_check_and_prompts_with_three_actions() -> None:
    env = env_for(gate())
    state, fx = begin(env)
    assert failing(state) == [("gate_open", "gate_closed:Fence gate")]
    [prompt] = prompts(state)
    assert prompt["subject"] == "g1"
    assert prompt["actions"] == ["opened", "closed", "skip_plan"]
    assert prompt["context"]["gate_name"] == "Fence gate"
    assert [e.payload["gate_id"] for e in events(fx, "gate_crossing_expected")] == ["g1"]
    assert state["state"]["gate_crossings"]["g1"]["run_id"] == "p1-r1"


def test_a_run_on_an_ungated_zone_is_not_held() -> None:
    env = env_for(gate())
    state, fx = begin(env, zone="z2")
    assert failing(state) == [] and prompts(state) == []
    assert not events(fx, "gate_crossing_expected")


def test_unanswered_prompt_waits_and_never_starts() -> None:
    env = env_for(gate())
    state, _ = begin(env)
    for t in (61, 200, 4000):
        state, fx = step(state, TimerFired(at(t)), env)
        assert not [e for e in fx if isinstance(e, StartRunEffect)]
    assert state["state"]["schedule"]["status"] == "waiting"
    assert len(prompts(state)) == 1  # not re-sent every tick


def test_i_opened_it_sets_the_flag_and_lets_the_run_start() -> None:
    env = env_for(gate())
    state, _ = begin(env)
    state, fx = answer(state, env, "opened")
    assert state["state"]["gate_flags"] == {"g1": True}
    assert prompts(state) == [] and failing(state) == []
    assert events(fx, "prompt_answered")
    state, _ = step(state, TimerFired(at(10)), env)  # stability window opens
    state, fx = step(state, TimerFired(at(80)), env)
    assert [e for e in fx if isinstance(e, StartRunEffect)]


def test_closed_acknowledges_but_the_check_keeps_failing() -> None:
    env = env_for(gate())
    state, _ = begin(env)
    state, _ = answer(state, env, "closed")
    assert prompts(state) == []
    assert failing(state) == [("gate_open", "gate_closed:Fence gate")]
    assert state["state"]["gate_flags"].get("g1", False) is False


def test_skip_plan_answers_the_prompt_and_skips_the_plan() -> None:
    env = env_for(gate())
    state, _ = begin(env)
    state, fx = answer(state, env, "skip_plan")
    assert state["state"]["schedule"]["status"] == "idle"
    assert [r["status"] for r in state["history"]["runs"]] == ["skipped"]
    assert prompts(state) == []
    assert state["state"]["gate_crossings"] == {}


def test_skip_plan_goes_on_to_the_next_run_when_one_is_left() -> None:
    env = replace(env_for(gate()), gated_zones={"z1": ("g1",)})
    state, _ = begin(env)
    state, _ = step(state, AddPlan(at(1), *make_plan("p2", zone="z2")), env)
    state, _ = answer(state, env, "skip_plan", t=2)
    assert state["state"]["schedule"]["status"] == "waiting"
    assert [r["plan_id"] for r in state["state"]["runs"]] == ["p2"]


def start_through(env, state):
    state, _ = step(state, TimerFired(at(10)), env)
    state, fx = step(state, TimerFired(at(80)), env)
    assert [e for e in fx if isinstance(e, StartRunEffect)]
    return state


def test_crossing_ends_when_mowing_closes_the_flag_and_prompts_to_close() -> None:
    env = env_for(gate())
    state, _ = begin(env)
    state, _ = answer(state, env, "opened")
    state = start_through(env, state)
    state, fx = step(state, SnapshotReceived(at(100), MOWING), env)
    [ended] = events(fx, "gate_crossing_ended")
    assert ended.payload["gate_id"] == "g1" and ended.payload["run_id"] == "p1-r1"
    assert state["state"]["gate_flags"] == {"g1": False}
    assert state["state"]["gate_crossings"] == {}
    [close] = prompts(state, "gate_close")
    assert close["actions"] == ["closed"] and close["subject"] == "g1"
    state, _ = step(state, AnswerPrompt(at(110), close["prompt_id"], "closed"), env)
    assert prompts(state, "gate_close") == []


def test_close_action_runs_instead_of_the_through_prompt() -> None:
    env = env_for(gate(close_action=CLOSE_ACTION))
    state, _ = begin(env)
    state, _ = answer(state, env, "opened")
    state = start_through(env, state)
    state, fx = step(state, SnapshotReceived(at(100), MOWING), env)
    [act] = [e for e in fx if isinstance(e, GateActionEffect)]
    assert (act.gate_id, act.which) == ("g1", "close")
    assert prompts(state, "gate_close") == []


def test_crossing_dropped_quietly_when_the_run_never_gets_through() -> None:
    env = env_for(gate())
    state, _ = begin(env)
    state, fx = answer(state, env, "skip_plan")
    assert not events(fx, "gate_crossing_ended")


# -- automation -----------------------------------------------------------------


def test_open_action_runs_instead_of_notifying_and_sets_the_flag() -> None:
    env = env_for(gate(open_action=OPEN_ACTION))
    state, fx = begin(env)
    [act] = [e for e in fx if isinstance(e, GateActionEffect)]
    assert (act.gate_id, act.which) == ("g1", "open")
    assert prompts(state) == []
    assert state["state"]["gate_flags"] == {"g1": True}
    assert failing(state) == []


def test_open_action_with_a_state_entity_verifies_by_state() -> None:
    env = env_for(gate(open_action=OPEN_ACTION, state_entity="cover.fence"),
                  entity_states={"g1": "closed"})
    state, fx = begin(env)
    assert [e for e in fx if isinstance(e, GateActionEffect)]
    assert prompts(state) == []
    assert failing(state) == [("gate_open", "gate_closed:Fence gate")]
    # The entity reads open in time: no failure, no prompt.
    env_open = env_for(gate(open_action=OPEN_ACTION, state_entity="cover.fence"),
                       entity_states={"g1": "open"})
    state, fx = step(state, TimerFired(at(60)), env_open)
    assert failing(state) == []
    state, fx = step(state, TimerFired(at(130)), env_open)
    assert not events(fx, "gate_open_action_failed") and prompts(state) == []


def test_still_closed_after_two_minutes_falls_back_to_notifying() -> None:
    env = env_for(gate(open_action=OPEN_ACTION, state_entity="cover.fence"),
                  entity_states={"g1": "closed"})
    state, _ = begin(env)
    state, fx = step(state, TimerFired(at(119)), env)
    assert prompts(state) == [] and not events(fx, "gate_open_action_failed")
    state, fx = step(state, TimerFired(at(121)), env)
    [failed] = events(fx, "gate_open_action_failed")
    assert failed.payload["gate_id"] == "g1"
    [prompt] = prompts(state)
    assert prompt["actions"] == ["opened", "closed", "skip_plan"]
    # The open action is not run again while this crossing waits.
    state, fx = step(state, TimerFired(at(300)), env)
    assert not [e for e in fx if isinstance(e, GateActionEffect)]


def test_no_prompt_when_notify_is_off_but_the_run_still_waits() -> None:
    env = env_for(gate(notify=False))
    state, _ = begin(env)
    assert prompts(state) == []
    assert failing(state)


# -- believed state ------------------------------------------------------------------


def test_linked_entity_wins_over_the_flag() -> None:
    env = env_for(gate(state_entity="cover.fence"), entity_states={"g1": "open"})
    state, _ = begin(env)
    assert failing(state) == [] and prompts(state) == []
    closed = env_for(gate(state_entity="cover.fence"), entity_states={"g1": "closed"})
    state, _ = step(initial_core_state(), AddPlan(at(0), *make_plan(), start=True), closed)
    state, _ = step(state, SetGateFlag(at(1), "g1", True), closed)
    state, _ = step(state, SnapshotReceived(at(2), DOCKED), closed)
    assert failing(state)  # the flag is not consulted when an entity is linked


def test_the_prompt_settles_when_the_entity_reports_open() -> None:
    closed = env_for(gate(state_entity="cover.fence"), entity_states={"g1": "closed"})
    state, _ = begin(closed)
    assert len(prompts(state)) == 1
    opened = env_for(gate(state_entity="cover.fence"), entity_states={"g1": "open"})
    state, fx = step(state, TimerFired(at(20)), opened)
    assert prompts(state) == [] and events(fx, "prompt_answered")


def test_set_gate_flag_input_toggles_the_flag_and_reevaluates() -> None:
    env = env_for(gate())
    state, _ = begin(env)
    state, _ = step(state, SetGateFlag(at(3), "g1", True), env)
    assert failing(state) == [] and prompts(state) == []


# -- Check settings --------------------------------------------------------------------


def test_default_gate_check_has_no_timeout_but_one_can_be_set() -> None:
    from custom_components.groundskeeper.checks import default_check_records

    rec = {r["type"]: r for r in default_check_records()}["gate_open"]
    assert (rec["enabled"], rec["timeout_s"], rec["policy"]) == (True, None, "notify")
    env = env_for(gate(), check={"timeout_s": 600, "policy": "pause"})
    state, _ = begin(env)
    state, fx = step(state, TimerFired(at(601)), env)
    assert events(fx, "check_timeout")
    assert state["state"]["schedule"]["status"] == "paused"


def test_a_disabled_gate_check_holds_nothing() -> None:
    env = env_for(gate(), check={"enabled": False})
    state, _ = begin(env)
    assert failing(state) == [] and prompts(state) == []


def test_the_gate_is_only_asked_about_once_nothing_else_holds_the_run() -> None:
    env = env_for(gate())
    state, _ = step(initial_core_state(), AddPlan(at(0), *make_plan(), start=True), env)
    state, fx = step(state, SnapshotReceived(at(0), replace(DOCKED, battery=5)), env)
    assert prompts(state) == [] and state["state"]["gate_crossings"] == {}
    assert not events(fx, "gate_crossing_expected")
    state, fx = step(state, SnapshotReceived(at(10), DOCKED), env)
    assert len(prompts(state)) == 1 and events(fx, "gate_crossing_expected")
