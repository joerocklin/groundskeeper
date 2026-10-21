"""Gate crossings during a Run at the reducer seam (spec §10)."""

from dataclasses import replace

from homeassistant.components.lawn_mower import LawnMowerActivity as A

from custom_components.mowing_director.core import (
    AnswerPrompt,
    GateActionEffect,
    SnapshotReceived,
    TimerFired,
)
from tests.test_core_reducer import DOCKED, MOWING, at, events, step
from tests.test_gates_reducer import (
    CLOSE_ACTION,
    OPEN_ACTION,
    answer,
    begin,
    env_for,
    gate,
    prompts,
    start_through,
)


def snap(base=MOWING, **kw):
    return replace(base, **kw)


def mowing_through(env, battery=80, start_env=None):
    """A gated Run that has been let through and is mowing; the Gate is closed
    behind it again. `start_env` is what the Gate looked like at Run start."""
    state, _ = begin(start_env or env)
    if prompts(state):
        state, _ = answer(state, start_env or env, "opened")
    state = start_through(start_env or env, state)
    state, _ = step(state, SnapshotReceived(at(100), snap(battery=battery)), env)
    assert state["state"]["gate_crossings"] == {}
    return state


def feed(state, env, *points):
    """Snapshots as (seconds, snapshot) pairs; returns state and all effects."""
    effects = []
    for t, snapshot in points:
        state, fx = step(state, SnapshotReceived(at(t), snapshot), env)
        effects += fx
    return state, effects


def expected(fx, kind=None):
    found = events(fx, "gate_crossing_expected")
    return [e.payload for e in found if kind is None or e.payload["kind"] == kind]


def levels(state):
    return state["learned"]["battery_levels"]


# -- learning the thresholds ----------------------------------------------------


def test_return_level_is_the_battery_when_returning_leads_to_recharging() -> None:
    env = env_for()  # no Gates at all: samples come from every Run
    state, _ = step_run(env)
    state, _ = feed(
        state, env,
        (200, snap(battery=31)),
        (260, snap(A.RETURNING if False else MOWING, activity=A.RETURNING, battery=30)),
        (320, snap(activity=A.RETURNING, battery=28)),
        (400, snap(DOCKED, job_active=True, recharging=True, battery=27)),
    )
    assert levels(state)["return"] == [30]


def step_run(env):
    from tests.test_core_reducer import AddPlan, make_plan
    from custom_components.mowing_director.core import initial_core_state

    state, _ = step(initial_core_state(), AddPlan(at(0), *make_plan(), start=True), env)
    state, _ = step(state, SnapshotReceived(at(0), DOCKED), env)
    state, _ = step(state, TimerFired(at(10)), env)
    state, _ = step(state, TimerFired(at(80)), env)
    state, _ = step(state, SnapshotReceived(at(100), snap(battery=40)), env)
    return state, None


def test_a_breakpoint_return_without_recharging_learns_nothing() -> None:
    env = env_for()
    state, _ = step_run(env)
    state, _ = feed(
        state, env,
        (200, snap(activity=A.RETURNING, battery=35)),
        (300, snap(battery=35)),
    )
    assert levels(state)["return"] == []


def test_resume_level_is_the_battery_when_the_mower_leaves_the_dock() -> None:
    env = env_for()
    state, _ = step_run(env)
    state, _ = feed(
        state, env,
        (200, snap(activity=A.RETURNING, battery=25)),
        (300, snap(DOCKED, job_active=True, recharging=True, battery=26)),
        (900, snap(DOCKED, job_active=True, recharging=True, battery=96)),
        (960, snap(battery=96)),
    )
    assert levels(state)["resume"] == [96]
    assert levels(state)["return"] == [25]


def test_samples_keep_only_the_last_n() -> None:
    env = replace(env_for(), battery_samples_max=2)
    state, _ = step_run(env)
    for i, level in enumerate((30, 28, 26)):
        t = 200 + i * 1000
        state, _ = feed(
            state, env,
            (t, snap(battery=level + 1)),
            (t + 60, snap(activity=A.RETURNING, battery=level)),
            (t + 120, snap(DOCKED, job_active=True, recharging=True, battery=level)),
            (t + 600, snap(battery=level + 40)),
        )
    assert levels(state)["return"] == [28, 26]


# -- warning ahead of a recharge return -------------------------------------------


def test_return_warns_ten_minutes_ahead_from_the_battery_slope() -> None:
    env = env_for(gate())
    state = mowing_through(env, battery=45)
    state, fx = feed(state, env, (400, snap(battery=40)))
    assert expected(fx) == []  # 15 points to go on a thin trace
    state, fx = feed(state, env, (700, snap(battery=35)))  # -1 %/min: 10 min to 25
    [warning] = expected(fx)
    assert (warning["kind"], warning["direction"], warning["gate_id"]) == (
        "return", "back", "g1")
    [prompt] = prompts(state)
    assert prompt["actions"] == ["opened", "closed"]  # no Skip Plan mid-Run
    assert prompt["context"]["crossing"] == "back" and not prompt["context"]["urgent"]
    # warned once per recharge, not on every snapshot
    state, fx = feed(state, env, (760, snap(battery=34)))
    assert expected(fx) == [] and len(prompts(state)) == 1


def test_without_a_usable_slope_the_margin_is_five_points() -> None:
    env = env_for(gate())
    state = mowing_through(env, battery=31)  # one point only
    state, fx = feed(state, env, (101, snap(battery=31)))
    assert expected(fx) == []
    state, fx = feed(state, env, (102, snap(battery=30)))
    assert [w["kind"] for w in expected(fx)] == ["return"]


def test_learned_return_level_replaces_the_default() -> None:
    env = env_for(gate())
    state = mowing_through(env, battery=45)
    state["learned"]["battery_levels"]["return"] = [40, 41, 39]
    state, fx = feed(state, env, (101, snap(battery=45)))  # margin 5 over the 40
    assert [w["kind"] for w in expected(fx)] == ["return"]


def test_two_samples_are_not_enough_so_the_configured_level_is_used() -> None:
    env = replace(env_for(gate()), recharge_level=50)
    state = mowing_through(env, battery=60)
    state["learned"]["battery_levels"]["return"] = [30, 30]
    state, fx = feed(state, env, (101, snap(battery=60)))
    assert expected(fx) == []
    state, fx = feed(state, env, (102, snap(battery=55)))
    assert [w["kind"] for w in expected(fx)] == ["return"]


def test_no_battery_samples_kept_disables_recharge_warnings() -> None:
    env = replace(env_for(gate()), battery_samples_max=0)
    state = mowing_through(env, battery=20)
    state, fx = feed(state, env, (101, snap(battery=20)))
    assert expected(fx) == [] and prompts(state) == []


def test_a_run_on_an_ungated_zone_is_never_warned() -> None:
    env = replace(env_for(gate()), gated_zones={})
    state, _ = step_run(env)
    state, fx = feed(state, env, (101, snap(battery=20)))
    assert expected(fx) == [] and prompts(state) == []


def test_a_gate_already_open_needs_no_prompt() -> None:
    env = env_for(gate(state_entity="cover.fence"), entity_states={"g1": "open"})
    state, _ = step_run(env)
    state, fx = feed(state, env, (101, snap(battery=20)))
    assert [w["kind"] for w in expected(fx)] == ["return"]
    assert prompts(state) == []


def test_open_action_runs_at_the_expected_return_crossing() -> None:
    env = env_for(gate(open_action=OPEN_ACTION, close_action=CLOSE_ACTION))
    state = mowing_through(env, battery=45)
    state, fx = feed(state, env, (101, snap(battery=20)))
    [act] = [e for e in fx if isinstance(e, GateActionEffect)]
    assert (act.gate_id, act.which) == ("g1", "open")
    assert prompts(state) == []


def test_open_action_not_confirmed_falls_back_to_a_prompt() -> None:
    env = env_for(gate(open_action=OPEN_ACTION, state_entity="cover.fence"),
                  entity_states={"g1": "closed"})
    opened = env_for(gate(open_action=OPEN_ACTION, state_entity="cover.fence"),
                     entity_states={"g1": "open"})
    state = mowing_through(env, battery=45, start_env=opened)
    state, fx = feed(state, env, (101, snap(battery=20)))
    assert prompts(state) == []
    state, fx = step(state, TimerFired(at(150)), env)
    assert not events(fx, "gate_open_action_failed")
    state, fx = step(state, TimerFired(at(225)), env)
    assert events(fx, "gate_open_action_failed")
    [prompt] = prompts(state)
    assert prompt["actions"] == ["opened", "closed"]


# -- the crossing ends ------------------------------------------------------------


def test_docking_ends_the_back_crossing_and_asks_to_close() -> None:
    env = env_for(gate())
    state = mowing_through(env, battery=45)
    state, _ = feed(state, env, (101, snap(battery=20)))
    state, _ = answer(state, env, "opened", t=105)
    assert state["state"]["gate_flags"] == {"g1": True}
    state, fx = feed(
        state, env,
        (200, snap(activity=A.RETURNING, battery=19)),
        (300, snap(DOCKED, job_active=True, recharging=True, battery=19)),
    )
    [ended] = events(fx, "gate_crossing_ended")
    assert ended.payload["gate_id"] == "g1"
    assert state["state"]["gate_crossings"] == {}
    assert state["state"]["gate_flags"] == {"g1": False}
    [close] = prompts(state, "gate_close")
    assert close["actions"] == ["closed"]


def test_close_action_runs_after_the_back_crossing() -> None:
    env = env_for(gate(open_action=OPEN_ACTION, close_action=CLOSE_ACTION))
    state = mowing_through(env, battery=45)
    state, _ = feed(state, env, (101, snap(battery=20)))
    state, fx = feed(state, env, (300, snap(DOCKED, job_active=True, recharging=True,
                                            battery=19)))
    assert [e.which for e in fx if isinstance(e, GateActionEffect)] == ["close"]


# -- resume ----------------------------------------------------------------------


def recharging(env, battery=80):
    state = mowing_through(env, battery=45)
    state, _ = feed(state, env, (101, snap(battery=20)))
    state, _ = answer(state, env, "opened", t=105)
    state, _ = feed(
        state, env,
        (200, snap(activity=A.RETURNING, battery=19)),
        (300, snap(DOCKED, job_active=True, recharging=True, battery=battery)),
    )
    state, _ = step(state, AnswerPrompt(at(310), prompts(state, "gate_close")[0]["prompt_id"],
                                        "closed"), env)
    return state


def test_resume_warns_ahead_and_the_crossing_ends_in_the_zone() -> None:
    env = env_for(gate())
    state = recharging(env, battery=80)
    state, fx = feed(state, env, (1000, snap(DOCKED, job_active=True, recharging=True,
                                             battery=85)))
    assert expected(fx) == []
    state, fx = feed(state, env, (1600, snap(DOCKED, job_active=True, recharging=True,
                                             battery=91)))
    [warning] = expected(fx)
    assert (warning["kind"], warning["direction"]) == ("resume", "out")
    state, _ = answer(state, env, "opened", t=1610)
    state, fx = feed(state, env, (1700, snap(battery=96)))
    assert events(fx, "gate_crossing_ended")
    assert prompts(state, "gate_close")
    assert state["state"]["gate_flags"] == {"g1": False}


# -- Run end ---------------------------------------------------------------------


def test_run_end_heads_up_at_95_percent_progress() -> None:
    env = env_for(gate())
    state = mowing_through(env, battery=80)
    state, fx = feed(state, env, (200, snap(battery=78, progress=94)))
    assert expected(fx) == []
    state, fx = feed(state, env, (260, snap(battery=77, progress=95)))
    [warning] = expected(fx)
    assert (warning["kind"], warning["direction"]) == ("end", "back")


def test_finishing_the_run_ends_the_back_crossing() -> None:
    env = env_for(gate())
    state = mowing_through(env, battery=80)
    state, _ = feed(state, env, (260, snap(battery=77, progress=96)))
    state, _ = answer(state, env, "opened", t=270)
    state, fx = feed(
        state, env,
        (300, snap(DOCKED, battery=76)),
        (361, snap(DOCKED, battery=76)),
    )
    state, fx2 = step(state, TimerFired(at(362)), env)
    assert state["state"]["gate_crossings"] == {}
    assert prompts(state, "gate_close")


# -- `returning` is definitive -----------------------------------------------------


def test_an_unpredicted_return_is_warned_then_and_urgently() -> None:
    env = env_for(gate())
    state = mowing_through(env, battery=80)
    state, fx = feed(state, env, (200, snap(activity=A.RETURNING, battery=79)))
    [warning] = expected(fx)
    assert (warning["kind"], warning["direction"]) == ("unpredicted", "back")
    [prompt] = prompts(state)
    assert prompt["context"]["urgent"] is True


def test_a_predicted_return_resends_its_prompt_once_when_returning_starts() -> None:
    env = env_for(gate())
    state = mowing_through(env, battery=45)
    state, _ = feed(state, env, (101, snap(battery=20)))
    [first] = prompts(state)
    assert not first["context"]["urgent"]
    state, fx = feed(state, env, (200, snap(activity=A.RETURNING, battery=19)))
    [second] = prompts(state)
    assert second["prompt_id"] != first["prompt_id"] and second["tag"] == first["tag"]
    assert second["context"]["urgent"] is True
    assert expected(fx) == []
    state, fx = feed(state, env, (260, snap(activity=A.RETURNING, battery=18)))
    assert prompts(state)[0]["prompt_id"] == second["prompt_id"]  # only once


def test_no_resend_when_the_gate_got_opened() -> None:
    env = env_for(gate())
    state = mowing_through(env, battery=45)
    state, _ = feed(state, env, (101, snap(battery=20)))
    state, _ = answer(state, env, "opened", t=150)
    state, _ = feed(state, env, (200, snap(activity=A.RETURNING, battery=19)))
    assert prompts(state) == []


def test_leaving_the_dock_with_the_gate_closed_resends_once() -> None:
    env = env_for(gate())
    state = recharging(env, battery=80)
    state, _ = feed(state, env, (1600, snap(DOCKED, job_active=True, recharging=True,
                                            battery=91)))
    [first] = prompts(state)
    leaving = snap(activity=A.MOWING, battery=93, zone="elsewhere")
    state, _ = feed(state, env, (1700, leaving))
    [second] = prompts(state)
    assert second["prompt_id"] != first["prompt_id"] and second["context"]["urgent"]
    state, _ = feed(state, env, (1760, leaving))
    assert prompts(state)[0]["prompt_id"] == second["prompt_id"]


def test_the_director_never_commands_the_mower_for_a_gate() -> None:
    from custom_components.mowing_director.core import AbortRunEffect, ResumeRunEffect

    env = env_for(gate())
    state = mowing_through(env, battery=45)
    _, fx = feed(
        state, env,
        (101, snap(battery=20)),
        (200, snap(activity=A.RETURNING, battery=19)),
        (400, snap(activity=A.RETURNING, battery=19)),
    )
    assert not [e for e in fx if isinstance(e, (AbortRunEffect, ResumeRunEffect))]
