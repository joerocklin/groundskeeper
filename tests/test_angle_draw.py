"""Angle draw (spec 5.5). Pure functions, no HA. Seam: `draw_angles`."""

import random

import pytest

from custom_components.mowing_director.angle_draw import (
    AngleDraw,
    angular_distance,
    draw_angles,
)

FULL = "full"


def dist(a, b):
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def inside(angle, rng):
    return rng["min"] - 1e-9 <= angle <= rng["max"] + 1e-9


class FirstChoice:
    """Deterministic fake RNG: always picks the first candidate."""

    def choice(self, seq):
        return seq[0]


def test_angular_distance_wraps_at_180():
    assert angular_distance(80.0, -80.0) == 20.0
    assert angular_distance(90.0, -90.0) == 0.0
    assert angular_distance(0.0, 45.0) == 45.0


def test_same_seed_same_result():
    a = draw_angles({"min": -40, "max": 40}, 3, rng=random.Random(5))
    b = draw_angles({"min": -40, "max": 40}, 3, rng=random.Random(5))
    assert a == b


def test_injected_fake_rng_is_used():
    d = draw_angles(FULL, 1, base_angle_mode="random", rng=FirstChoice())
    assert d.angles == [-89.5]


def test_earlier_runs_inside_range_and_normalised():
    rng = {"min": -30, "max": 20}
    for seed in range(40):
        d = draw_angles(rng, 4, base_angle_mode="random", rng=random.Random(seed))
        assert len(d.angles) == 4
        assert all(inside(a, rng) for a in d.angles)
        assert all(-90 < a <= 90 for a in d.angles)


def test_full_range_stays_normalised():
    for seed in range(40):
        d = draw_angles(FULL, 5, rng=random.Random(seed))
        assert all(-90 < a <= 90 for a in d.angles)


def test_fixed_final_uses_base_plus_jitter():
    for seed in range(40):
        d = draw_angles(
            {"min": -80, "max": 80}, 3, base_angle=10.0, rng=random.Random(seed)
        )
        assert 5.0 - 1e-9 <= d.angles[-1] <= 15.0 + 1e-9
        assert d.base_angle_mode == "fixed"
        assert d.finish_jitter == 5.0


def test_single_maintenance_run_is_fixed_base_with_jitter():
    d = draw_angles({"min": -10, "max": 10}, 1, base_angle=0.0, rng=random.Random(1))
    assert -5.0 <= d.angles[0] <= 5.0


def test_final_differs_at_least_2_from_last_finals():
    finals = [0.0, 1.0, -1.0]
    for seed in range(60):
        d = draw_angles(
            FULL, 2, last_finals=finals, base_angle=0.0, rng=random.Random(seed)
        )
        assert all(dist(d.angles[-1], f) >= 2.0 - 1e-9 for f in finals)
        assert not d.relaxations_of("finish_difference")


def test_finish_difference_relaxed_when_impossible():
    finals = [-1.0, 0.0, 1.0]
    d = draw_angles(
        FULL, 1, base_angle=0.0, finish_jitter=0.0, last_finals=finals,
        rng=random.Random(1),
    )
    assert d.relaxations_of("finish_difference")
    assert d.warnings


def test_fixed_base_outside_range_warns_but_draws():
    d = draw_angles({"min": -20, "max": 20}, 2, base_angle=60.0, rng=random.Random(1))
    assert 55.0 <= d.angles[-1] <= 65.0
    assert any("outside" in w for w in d.warnings)


def test_random_mode_final_draws_inside_range():
    rng = {"min": -20, "max": 20}
    for seed in range(40):
        d = draw_angles(
            rng, 3, base_angle_mode="random", base_angle=80.0, rng=random.Random(seed)
        )
        assert inside(d.angles[-1], rng)
        assert not any("outside" in w for w in d.warnings)


def test_consecutive_runs_cross_at_least_crossing_angle():
    for seed in range(60):
        d = draw_angles(FULL, 5, crossing_angle=45.0, rng=random.Random(seed))
        for a, b in zip(d.angles, d.angles[1:]):
            assert dist(a, b) >= 45.0 - 1e-9


def test_all_pairs_at_least_separation():
    for seed in range(60):
        d = draw_angles(
            FULL, 4, crossing_angle=20.0, min_separation=30.0, rng=random.Random(seed)
        )
        for i in range(4):
            for j in range(i + 1, 4):
                assert dist(d.angles[i], d.angles[j]) >= 30.0 - 1e-9


def test_defaults_are_45_and_15():
    d = draw_angles(FULL, 3, rng=random.Random(2))
    assert d.crossing_angle == 45.0
    assert d.min_separation == 15.0


def test_new_angles_avoid_history():
    hist = [10.0, 50.0, -60.0]
    for seed in range(40):
        d = draw_angles(
            FULL, 3, base_angle_mode="random", history_angles=hist,
            rng=random.Random(seed),
        )
        for a in d.angles:
            assert all(dist(a, h) >= 15.0 - 1e-9 for h in hist)
        assert d.history_angles_avoided == hist


def test_farthest_from_history_wins_among_valid():
    # Only range [0, 30] is allowed; history at 0 -> the best valid draw is 30.
    d = draw_angles(
        {"min": 0, "max": 30}, 1, base_angle_mode="random", history_angles=[0.0],
        rng=random.Random(3), draws=500,
    )
    assert d.angles == [30.0]


def test_history_relaxed_first_without_touching_separation():
    # Range [0,20]; history at 10 blocks it all with sep 15. Relax history only.
    d = draw_angles(
        {"min": 0, "max": 20}, 1, base_angle_mode="random", history_angles=[10.0],
        rng=random.Random(1),
    )
    kinds = [r.kind for r in d.relaxations]
    assert kinds == ["history"]
    assert inside(d.angles[0], {"min": 0, "max": 20})
    assert d.warnings


def test_separation_relaxed_before_crossing_and_records_value():
    # Range 0..40 only, 2 runs, crossing 30 (needs 0 and >=30), sep 35 impossible.
    d = draw_angles(
        {"min": 0, "max": 40}, 2, base_angle_mode="random",
        crossing_angle=30.0, min_separation=35.0, rng=random.Random(1), draws=500,
    )
    kinds = [r.kind for r in d.relaxations]
    assert "separation" in kinds and "crossing" not in kinds
    rel = d.relaxations_of("separation")[0]
    assert rel.applied == 40.0 or rel.applied == pytest.approx(rel.applied)
    assert rel.message == f"separation relaxed to {rel.applied:g}°"
    assert dist(d.angles[0], d.angles[1]) >= rel.applied - 1e-9


def test_crossing_relaxed_to_widest_that_fits_after_separation():
    # Range 0..20: widest possible gap is 20, below crossing 45.
    d = draw_angles(
        {"min": 0, "max": 20}, 2, base_angle_mode="random",
        rng=random.Random(1), draws=500,
    )
    assert [r.kind for r in d.relaxations] == ["crossing"]
    cross = d.relaxations_of("crossing")[0]
    assert cross.applied == 20.0
    assert dist(d.angles[0], d.angles[1]) == pytest.approx(20.0)
    assert cross.message == "crossing relaxed to 20°"


def test_both_relaxed_in_order_when_range_tiny():
    d = draw_angles(
        {"min": 0, "max": 8}, 2, base_angle_mode="random",
        rng=random.Random(1), draws=500,
    )
    kinds = [r.kind for r in d.relaxations]
    assert kinds.index("separation") < kinds.index("crossing")
    assert d.relaxations_of("separation")[0].message == "separation relaxed to 8°"
    assert dist(d.angles[0], d.angles[1]) == pytest.approx(8.0)


def test_never_refuses_zero_width_range():
    d = draw_angles({"min": 0, "max": 0}, 3, base_angle_mode="random", rng=random.Random(1))
    assert d.angles == [0.0, 0.0, 0.0]
    assert d.warnings


def test_no_relaxation_when_constraints_fit():
    d = draw_angles(FULL, 3, rng=random.Random(4))
    assert d.relaxations == []
    assert d.warnings == []


def test_double_grid_second_angle_counts_toward_crossing_and_separation():
    # Run 0 is double grid with second angle 0: run 1 must keep 45 from it.
    for seed in range(40):
        d = draw_angles(
            FULL, 2, base_angle_mode="random", double_grid=[0.0, None],
            rng=random.Random(seed),
        )
        assert d.second_angles == [0.0, None]
        assert dist(d.angles[1], 0.0) >= 45.0 - 1e-9
        assert dist(d.angles[1], d.angles[0]) >= 45.0 - 1e-9


def test_double_grid_second_angle_counts_for_non_adjacent_separation():
    for seed in range(40):
        d = draw_angles(
            FULL, 3, base_angle_mode="random", double_grid=[None, None, 0.0],
            crossing_angle=10.0, rng=random.Random(seed),
        )
        assert dist(d.angles[0], 0.0) >= 15.0 - 1e-9


def test_double_grid_second_angle_outside_range_warns_only():
    d = draw_angles(
        {"min": -20, "max": 20}, 2, double_grid=[None, 70.0], rng=random.Random(1)
    )
    assert d.second_angles[1] == 70.0
    assert any("double-grid" in w and "outside" in w for w in d.warnings)


def test_double_grid_second_angle_normalised():
    d = draw_angles(FULL, 1, double_grid=[100.0], rng=random.Random(1))
    assert d.second_angles == [-80.0]


def test_unset_range_draws_as_full_and_is_provisional():
    d = draw_angles("unset", 3, rng=random.Random(1))
    assert d.provisional is True
    assert len(d.angles) == 3
    assert draw_angles(FULL, 3, rng=random.Random(1)).provisional is False


def test_record_dict_is_storable():
    d = draw_angles(
        FULL, 2, history_angles=[10.0], last_finals=[0.0], rng=random.Random(1)
    )
    rec = d.to_dict()
    assert rec["angles"] == d.angles
    assert rec["base_angle_mode"] == "fixed"
    assert rec["finish_jitter"] == 5.0
    assert rec["history_angles_avoided"] == [10.0]
    assert rec["relaxations"] == []
    import json

    json.dumps(rec)


def test_zero_runs_returns_empty():
    assert draw_angles(FULL, 0, rng=random.Random(1)).angles == []


def test_bad_inputs_rejected():
    with pytest.raises(ValueError):
        draw_angles(FULL, 2, base_angle_mode="sideways")
    with pytest.raises(ValueError):
        draw_angles(FULL, 2, double_grid=[None])
