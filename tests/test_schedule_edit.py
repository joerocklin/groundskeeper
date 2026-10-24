"""Reducer table tests: Schedule placement, Run edit/delete/reorder, remove_plan
(spec §5.7, §5.8, §13.2). Pure."""

import pytest

from custom_components.groundskeeper.core import (
    AbortRunEffect,
    AddPlan,
    DeleteRun,
    EditRun,
    EmitEvent,
    MoveRun,
    RemovePlan,
    ReplacePlan,
    SnapshotReceived,
    StartSchedule,
    TimerFired,
    initial_core_state,
    move_blocker,
    remove_plan_blocker,
)
from tests.fake_mower import DEFAULT_SNAPSHOT
from tests.test_core_reducer import ENV, T0, at, make_plan, step


def ids(state):
    return [r["id"] for r in state["state"]["runs"]]


def with_plans(*specs, position="end"):
    """specs: (plan_id, runs). Each added through AddPlan with `position`."""
    state = initial_core_state()
    for plan_id, n in specs:
        plan, runs = make_plan(plan_id, n=n)
        state, _ = step(state, AddPlan(T0, plan, runs, position=position))
    return state


# -- placement (§5.8) ----------------------------------------------------------


def test_end_appends_and_front_goes_before_the_first_pending_run() -> None:
    state = with_plans(("a", 2))
    plan, runs = make_plan("b", n=2)
    s_end, _ = step(state, AddPlan(T0, plan, runs, position="end"))
    assert ids(s_end) == ["a-r1", "a-r2", "b-r1", "b-r2"]
    s_front, _ = step(state, AddPlan(T0, plan, runs, position="front"))
    assert ids(s_front) == ["b-r1", "b-r2", "a-r1", "a-r2"]


def test_front_keeps_the_active_run_in_place() -> None:
    state = with_plans(("a", 2))
    state, _ = step(state, StartSchedule(T0))
    state, _ = step(state, SnapshotReceived(T0, DEFAULT_SNAPSHOT))
    state, _ = step(state, TimerFired(at(61)))
    assert state["state"]["runs"][0]["status"] == "starting"
    plan, runs = make_plan("b", n=1)
    state, _ = step(state, AddPlan(at(62), plan, runs, position="front"))
    assert ids(state) == ["a-r1", "b-r1", "a-r2"]


def test_merge_orders_by_step_index_so_first_cuts_come_first() -> None:
    state = with_plans(("a", 3))
    plan, runs = make_plan("b", n=3)
    state, _ = step(state, AddPlan(T0, plan, runs, position="merge"))
    assert ids(state) == ["a-r1", "b-r1", "a-r2", "b-r2", "a-r3", "b-r3"]


def test_merge_of_a_short_plan_into_a_longer_one() -> None:
    state = with_plans(("a", 3))
    plan, runs = make_plan("b", n=1)
    state, _ = step(state, AddPlan(T0, plan, runs, position="merge"))
    assert ids(state) == ["a-r1", "b-r1", "a-r2", "a-r3"]


def test_merge_never_reorders_existing_runs_or_precedes_the_active_run() -> None:
    state = with_plans(("a", 3))
    state["state"]["runs"][0]["status"] = "mowing"  # active
    plan, runs = make_plan("b", n=2)
    state, _ = step(state, AddPlan(T0, plan, runs, position="merge"))
    assert ids(state)[0] == "a-r1"
    assert ids(state) == ["a-r1", "b-r1", "a-r2", "b-r2", "a-r3"]


# -- edit (§5.7) -----------------------------------------------------------------


def test_edit_changes_a_pending_run_only() -> None:
    state = with_plans(("a", 2))
    state, _ = step(
        state,
        EditRun(T0, "a-r2", {"height_mm": 65, "angle_deg": 40.0, "settings": {"spacing_cm": 27}}),
    )
    run = state["state"]["runs"][1]
    assert run["height_mm"] == 65 and run["settings"]["blade_height_mm"] == 65
    assert run["angle_deg"] == 40.0 and run["angle_edited"] is True
    assert run["settings"]["spacing_cm"] == 27
    assert run["settings"]["speed_m_s"] == 0.4  # patch, not replace


def test_edit_merges_extras_key_by_key() -> None:
    state = with_plans(("a", 1))
    state["state"]["runs"][0]["settings"]["extras"] = {"x": 1, "y": 2}
    state, _ = step(state, EditRun(T0, "a-r1", {"settings": {"extras": {"y": 3}}}))
    assert state["state"]["runs"][0]["settings"]["extras"] == {"x": 1, "y": 3}


@pytest.mark.parametrize("status", ["starting", "mowing", "on_hold", "failed", "done"])
def test_edit_delete_and_move_ignore_runs_that_are_not_pending(status) -> None:
    state = with_plans(("a", 2))
    state["state"]["runs"][0]["status"] = status
    for event in (
        EditRun(T0, "a-r1", {"height_mm": 60}),
        DeleteRun(T0, "a-r1"),
        MoveRun(T0, "a-r1", 1),
    ):
        after, effects = step(state, event)
        assert after == state and effects == []


def test_edit_works_in_any_schedule_status() -> None:
    state = with_plans(("a", 2))
    for status in ("idle", "waiting", "paused", "running"):
        state["state"]["schedule"]["status"] = status
        after, _ = step(state, EditRun(T0, "a-r2", {"height_mm": 60}))
        assert after["state"]["runs"][1]["height_mm"] == 60


# -- delete ----------------------------------------------------------------------


def test_delete_removes_a_pending_run_and_keeps_the_others_numbering() -> None:
    state = with_plans(("a", 3))
    state, _ = step(state, DeleteRun(T0, "a-r2"))
    assert ids(state) == ["a-r1", "a-r3"]
    assert [r["index"] for r in state["state"]["runs"]] == [1, 3]


def test_deleting_the_last_run_drops_the_plan_and_idles_a_waiting_schedule() -> None:
    state = with_plans(("a", 1))
    state, _ = step(state, StartSchedule(T0))
    assert state["state"]["schedule"]["status"] == "waiting"
    state, _ = step(state, DeleteRun(T0, "a-r1"))
    assert state["state"]["runs"] == [] and "a" not in state["state"]["plans"]
    assert state["state"]["schedule"]["status"] == "idle"


# -- reorder (§5.7) --------------------------------------------------------------


def test_move_reorders_runs_of_different_plans() -> None:
    state = with_plans(("a", 1), ("b", 1), ("c", 1))
    state, _ = step(state, MoveRun(T0, "c-r1", 0))
    assert ids(state) == ["c-r1", "a-r1", "b-r1"]


def test_move_never_puts_a_run_ahead_of_an_earlier_run_of_its_plan() -> None:
    state = with_plans(("a", 2), ("b", 1))
    assert move_blocker(state["state"], "a-r2", 0) == "before_earlier_run"
    after, _ = step(state, MoveRun(T0, "a-r2", 0))
    assert ids(after) == ids(state)
    # and the earlier Run cannot be pushed behind its later sibling either
    assert move_blocker(state["state"], "a-r1", 1) == "before_earlier_run"
    assert move_blocker(state["state"], "a-r1", 2) == "before_earlier_run"
    assert move_blocker(state["state"], "a-r1", 0) is None


def test_move_position_is_among_pending_runs_and_the_active_run_stays_put() -> None:
    state = with_plans(("a", 1), ("b", 1), ("c", 1))
    state["state"]["runs"][0]["status"] = "mowing"
    state, _ = step(state, MoveRun(T0, "c-r1", 0))
    assert ids(state) == ["a-r1", "c-r1", "b-r1"]


def test_move_rejects_a_position_out_of_range() -> None:
    state = with_plans(("a", 1), ("b", 1))
    assert move_blocker(state["state"], "a-r1", 5) == "bad_position"
    assert move_blocker(state["state"], "nope", 0) == "unknown_run"


# -- remove_plan (§13.2) ---------------------------------------------------------


def test_remove_plan_cancels_pending_runs_fires_event_and_never_commands() -> None:
    state = with_plans(("a", 2), ("b", 1))
    state, effects = step(state, RemovePlan(T0, "a"))
    assert ids(state) == ["b-r1"]
    assert [r["status"] for r in state["history"]["runs"]] == ["cancelled", "cancelled"]
    assert "a" in state["history"]["plans"]
    assert not any(isinstance(e, AbortRunEffect) for e in effects)
    removed = [e for e in effects if isinstance(e, EmitEvent) and e.type == "plan_removed"]
    assert len(removed) == 1 and removed[0].payload["plan_id"] == "a"


@pytest.mark.parametrize("status", ["starting", "mowing", "on_hold"])
def test_remove_plan_is_refused_while_a_run_of_it_is_active(status) -> None:
    state = with_plans(("a", 2))
    state["state"]["runs"][0]["status"] = status
    assert remove_plan_blocker(state["state"], "a") == "run_active"
    after, effects = step(state, RemovePlan(T0, "a"))
    assert after == state and effects == []


def test_remove_unknown_plan_is_refused() -> None:
    assert remove_plan_blocker(initial_core_state()["state"], "zz") == "unknown_plan"


def test_remove_plan_leaves_another_plans_active_run_alone() -> None:
    state = with_plans(("a", 1), ("b", 1))
    state["state"]["runs"][0]["status"] = "mowing"
    state["state"]["schedule"]["status"] = "running"
    state, effects = step(state, RemovePlan(T0, "b"))
    assert ids(state) == ["a-r1"] and state["state"]["schedule"]["status"] == "running"
    assert not any(isinstance(e, AbortRunEffect) for e in effects)


def test_remove_last_plan_idles_a_waiting_schedule() -> None:
    state = with_plans(("a", 1))
    state, _ = step(state, StartSchedule(T0))
    state, _ = step(state, RemovePlan(T0, "a"))
    assert state["state"]["schedule"]["status"] == "idle"


# -- replace (re-derive) ----------------------------------------------------------


def test_replace_swaps_a_not_started_plans_runs_in_place() -> None:
    state = with_plans(("a", 2), ("b", 1))
    plan, runs = make_plan("a", n=3)
    state, _ = step(state, ReplacePlan(T0, plan, runs))
    assert ids(state) == ["a-r1", "a-r2", "a-r3", "b-r1"]
    assert state["state"]["plans"]["a"]["id"] == "a"


def test_replace_is_ignored_once_a_run_of_the_plan_has_started() -> None:
    state = with_plans(("a", 2))
    state["state"]["runs"][0]["status"] = "mowing"
    plan, runs = make_plan("a", n=3)
    after, _ = step(state, ReplacePlan(T0, plan, runs))
    assert after == state
