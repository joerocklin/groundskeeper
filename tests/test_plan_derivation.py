"""Plan derivation: step-down heights, layered settings, Finishing Run, stored inputs
(spec §5.1-5.3, §5.5, §5.6). Pure."""

import random
from datetime import datetime, timezone

import pytest

from custom_components.groundskeeper.heights import HeightError
from custom_components.groundskeeper.plans import build_plan, rederive_plan
from custom_components.groundskeeper.run_settings import SettingsError
from tests.fake_mower import FakeMower

LIMITS = FakeMower().limits
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
ZONE = {"id": "z1", "name": "Front Lawn", "angle_range": {"angle_range": "full"}}


def build(start=79, end=61, *, config=None, zone=None, seed=1, **kw):
    ids = iter(f"id{i}" for i in range(100))
    return build_plan(
        zone=zone or ZONE, config=config or {}, limits=LIMITS, start=start, end=end,
        origin="service", now=NOW, rng=random.Random(seed), new_id=lambda: next(ids), **kw,
    )


def test_step_down_79_to_61_is_three_runs_with_n_of_m() -> None:
    built = build()
    runs = built.runs
    assert [r["height_mm"] for r in runs] == [79, 69, 61]
    assert [(r["index"], r["of"]) for r in runs] == [(1, 3), (2, 3), (3, 3)]
    assert [r["settings"]["blade_height_mm"] for r in runs] == [79, 69, 61]
    assert built.plan["run_ids"] == [r["id"] for r in runs]


def test_plan_stores_its_inputs() -> None:
    built = build(max_cut_pct=20, base_angle=10, finishing=False, plan_settings={"spacing_cm": 27})
    plan = built.plan
    assert plan["start_height_mm"] == 79 and plan["end_height_mm"] == 61
    assert plan["max_cut"] == pytest.approx(0.20)
    assert plan["base_angle"] == 10
    assert plan["finishing"] is False
    assert plan["settings"] == {"spacing_cm": 27}


def test_max_cut_defaults_to_the_mower_option_then_plan_override() -> None:
    assert build(100, 58).plan["max_cut"] == pytest.approx(0.13)
    assert build(100, 58, config={"max_cut_pct": 33}).plan["max_cut"] == pytest.approx(0.33)
    assert len(build(100, 58, config={"max_cut_pct": 33}).runs) < len(build(100, 58).runs)
    assert build(100, 58, config={"max_cut_pct": 33}, max_cut_pct=13).plan["max_cut"] == pytest.approx(0.13)


def test_start_below_end_and_out_of_limits_are_rejected() -> None:
    with pytest.raises(HeightError):
        build(50, 70)
    with pytest.raises(HeightError, match="limits"):
        build(500, 61)


def test_rounded_values_are_reported() -> None:
    built = build(start=79.4, end=61)
    assert built.heights.start == 79 and built.heights.rounded


def test_equal_heights_make_one_maintenance_run() -> None:
    assert len(build(70, 70).runs) == 1


def test_settings_are_copied_in_full_onto_each_run() -> None:
    built = build(config={"run_defaults": {"spacing_cm": 26}}, zone={**ZONE, "run_defaults": {"speed_m_s": 0.5}},
                  plan_settings={"perimeter_laps": 2})
    for run in built.runs:
        s = run["settings"]
        assert (s["spacing_cm"], s["speed_m_s"], s["perimeter_laps"]) == (26, 0.5, 2)
        assert "ultra_wave" in s["extras"]


def test_later_default_edits_do_not_change_a_built_plan() -> None:
    config = {"run_defaults": {"spacing_cm": 26}}
    built = build(config=config)
    config["run_defaults"]["spacing_cm"] = 30
    assert built.runs[0]["settings"]["spacing_cm"] == 26


def test_invalid_plan_settings_are_rejected() -> None:
    with pytest.raises(SettingsError):
        build(plan_settings={"spacing_cm": 9999})


def test_finishing_set_applies_to_last_run_of_a_multi_run_plan_only() -> None:
    config = {"finishing": {"speed_m_s": 0.3}}
    speeds = [r["settings"]["speed_m_s"] for r in build(config=config).runs]
    assert speeds[:-1] == [speeds[0]] * 2 and speeds[-1] == 0.3 and speeds[0] != 0.3


def test_finishing_never_applies_to_a_single_maintenance_run() -> None:
    config = {"finishing": {"speed_m_s": 0.3}}
    assert build(70, 70, config=config).runs[0]["settings"]["speed_m_s"] != 0.3
    assert build(70, 70, config=config, finishing=True).runs[0]["settings"]["speed_m_s"] != 0.3


def test_zone_finishing_wins_over_mower_finishing() -> None:
    config = {"finishing": {"speed_m_s": 0.3}}
    zone = {**ZONE, "finishing": {"speed_m_s": 0.25}}
    assert build(config=config, zone=zone).runs[-1]["settings"]["speed_m_s"] == 0.25


def test_finishing_flag_forces_on_or_off() -> None:
    off = build(config={"finishing": {"speed_m_s": 0.3}}, finishing=False)
    assert off.runs[-1]["settings"]["speed_m_s"] != 0.3
    # Forced on but no set defined: nothing to apply, no error.
    assert len(build(finishing=True).runs) == 3


def test_angles_are_drawn_and_recorded_on_every_run() -> None:
    built = build(base_angle=0)
    assert all(-90 < r["angle_deg"] <= 90 for r in built.runs)
    assert abs(built.runs[-1]["angle_deg"]) <= 5  # fixed base angle with finish jitter
    assert built.runs[0]["angle_draw"]["crossing_angle"] == 45.0
    assert built.runs[0]["angle_draw"]["provisional"] is False


def test_unset_zone_range_draws_provisional_angles() -> None:
    built = build(zone={**ZONE, "angle_range": {"angle_range": "unset"}})
    assert all(r["angle_draw"]["provisional"] for r in built.runs)


def test_rederive_keeps_plan_id_and_uses_current_defaults() -> None:
    built = build(config={"run_defaults": {"spacing_cm": 26}})
    again = rederive_plan(
        built.plan, zone=ZONE, config={"run_defaults": {"spacing_cm": 31}}, limits=LIMITS,
        now=NOW, rng=random.Random(2),
    )
    assert again.plan["id"] == built.plan["id"]
    assert again.plan["created_at"] == built.plan["created_at"]
    assert {r["settings"]["spacing_cm"] for r in again.runs} == {31}
    assert [r["height_mm"] for r in again.runs] == [79, 69, 61]
