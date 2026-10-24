"""Angles on Plans: layering, history window, re-roll, provisional angles
(spec §5.5; ticket #22). Pure."""

import copy
import random
from datetime import datetime, timedelta, timezone

from custom_components.groundskeeper.plans import build_plan, redraw_plan
from tests.fake_mower import FakeMower

LIMITS = FakeMower().limits
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
FULL = {"id": "z1", "name": "Front Lawn", "angle_range": {"angle_range": "full"}}
UNSET = {"id": "z1", "name": "Front Lawn", "angle_range": {"angle_range": "unset"}}


def build(zone=FULL, *, config=None, seed=1, start=100, end=58, **kw):
    ids = iter(f"id{i}" for i in range(100))
    return build_plan(
        zone=zone, config=config or {}, limits=LIMITS, start=start, end=end,
        origin="service", now=NOW, rng=random.Random(seed), new_id=lambda: next(ids), **kw,
    )


def done(angle, days_ago, *, index=1, of=1, zone_id="z1"):
    return {"zone_id": zone_id, "status": "done", "angle_deg": angle, "index": index,
            "of": of, "finished_at": (NOW - timedelta(days=days_ago)).isoformat()}


# -- stored on each Run ---------------------------------------------------------------


def test_each_run_carries_its_angle_and_the_plans_angle_draw_record() -> None:
    built = build()
    assert len(built.runs) > 2
    for run in built.runs:
        assert run["angle_draw"]["angles"] == [r["angle_deg"] for r in built.runs]
        assert run["angle_draw"]["provisional"] is False


# -- layering -------------------------------------------------------------------------


def test_finish_jitter_layers_mower_zone_plan() -> None:
    def final(config=None, zone=FULL, **kw):
        return build(zone, config=config, **kw).runs[-1]["angle_deg"]

    # base 20 fixed; jitter 0 means exactly the base angle
    assert final({"run_defaults": {"finish_jitter_deg": 0, "base_angle_deg": 20}}) == 20
    zone = {**FULL, "run_defaults": {"finish_jitter_deg": 0}}
    assert final({"run_defaults": {"base_angle_deg": 20, "finish_jitter_deg": 5}}, zone) == 20
    assert final({"run_defaults": {"base_angle_deg": 20}}, zone,
                 plan_settings={"finish_jitter_deg": 0}) == 20
    widened = {**FULL, "run_defaults": {"finish_jitter_deg": 0}}
    seen = {final({"run_defaults": {"base_angle_deg": 20}}, widened,
                  plan_settings={"finish_jitter_deg": 5}, seed=s) for s in range(30)}
    assert len(seen) > 1 and all(15 <= a <= 25 for a in seen)


def test_crossing_and_separation_layer_through_to_the_run_and_the_draw() -> None:
    built = build(FULL, config={"run_defaults": {"crossing_angle_deg": 30}},
                  plan_settings={"min_separation_deg": 20})
    assert built.runs[0]["angle_draw"]["crossing_angle"] == 30
    assert built.runs[0]["angle_draw"]["min_separation"] == 20
    assert built.runs[0]["settings"]["crossing_angle_deg"] == 30


# -- history avoidance ----------------------------------------------------------------


def test_history_uses_done_runs_on_this_zone_in_the_last_14_days_only() -> None:
    history = [
        done(30, 1), done(-40, 13),          # inside the window
        done(60, 15),                        # too old
        {**done(10, 1), "status": "failed"},  # not done
        done(-70, 1, zone_id="other"),       # other Zone
    ]
    built = build(history_runs=history)
    assert sorted(built.runs[0]["angle_draw"]["history_angles_avoided"]) == [-40, 30]


def test_finish_jitter_compares_against_the_last_three_final_angles_on_the_zone() -> None:
    finals = [done(a, d, index=2, of=2) for a, d in ((1, 6), (-1, 5), (2, 4), (3, 3), (-2, 2))]
    config = {"run_defaults": {"finish_jitter_deg": 5}}
    for seed in range(20):
        final = build(config=config, history_runs=finals, seed=seed).runs[-1]["angle_deg"]
        assert all(abs(final - f) >= 2 for f in (3, -2, 2))


# -- re-roll ---------------------------------------------------------------------------


def test_reroll_redraws_every_angle_keeping_everything_else() -> None:
    built = build(seed=1)
    runs = copy.deepcopy(built.runs)
    runs[0]["angle_edited"] = True
    new_plan, new_runs, _ = redraw_plan(
        built.plan, runs, zone=FULL, config={}, limits=LIMITS, now=NOW,
        rng=random.Random(99),
    )
    assert [r["id"] for r in new_runs] == [r["id"] for r in runs]
    assert [r["height_mm"] for r in new_runs] == [r["height_mm"] for r in runs]
    assert [r["angle_deg"] for r in new_runs] != [r["angle_deg"] for r in runs]
    assert not any(r.get("angle_edited") for r in new_runs)
    assert new_runs[0]["angle_draw"]["angles"] == [r["angle_deg"] for r in new_runs]
    assert new_plan["run_ids"] == built.plan["run_ids"]


# -- provisional angles ---------------------------------------------------------------


def test_plan_on_an_unset_range_is_drawn_full_and_marked_provisional() -> None:
    built = build(UNSET)
    assert all(r["angle_draw"]["provisional"] for r in built.runs)
    assert any("provisional" in w.lower() for w in built.warnings)


def test_range_arriving_redraws_unedited_provisional_runs_and_notes_the_plan() -> None:
    built = build(UNSET, seed=3)
    runs = copy.deepcopy(built.runs)
    runs[1]["angle_edited"] = True
    runs[1]["angle_deg"] = 80.0  # edited, will be outside the arriving range
    arrived = {**FULL, "angle_range": {"angle_range": {"min": -20.0, "max": 20.0}}}
    result = redraw_plan(
        built.plan, runs, zone=arrived, config={}, limits=LIMITS, now=NOW,
        rng=random.Random(5), provisional_only=True,
    )
    plan, new_runs, warnings = result
    assert plan["note"] == "angles_redrawn_for_range"
    assert new_runs[1]["angle_deg"] == 80.0  # edited keeps its angle
    assert any("outside" in w for w in warnings)
    for i in (0, 2, *range(3, len(new_runs))):
        assert -20 <= new_runs[i]["angle_deg"] <= 20
        assert new_runs[i]["angle_draw"]["provisional"] is False


def test_provisional_redraw_skips_a_plan_with_nothing_provisional() -> None:
    built = build(FULL)
    assert redraw_plan(
        built.plan, built.runs, zone=FULL, config={}, limits=LIMITS, now=NOW,
        provisional_only=True,
    ) is None
    # still provisional because the range is still unset
    prov = build(UNSET)
    assert redraw_plan(
        prov.plan, prov.runs, zone=UNSET, config={}, limits=LIMITS, now=NOW,
        provisional_only=True,
    ) is None
