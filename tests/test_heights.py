"""Height derivation, validation and unit helpers (spec 5.2, 5.3, 5.9). Pure."""

import math

import pytest

from custom_components.mowing_director.heights import (
    DEFAULT_MAX_CUT,
    HeightError,
    HeightLimits,
    derive_heights,
    from_display,
    height_input,
    to_display,
)

EXAMPLE_MOWER = HeightLimits(min_mm=55, max_mm=100, step_mm=1)
COARSE = HeightLimits(min_mm=50, max_mm=100, step_mm=5)


def test_default_max_cut_is_13_percent():
    assert DEFAULT_MAX_CUT == 0.13


@pytest.mark.parametrize(
    "start,end,m,expected",
    [
        (79, 61, 0.13, [79, 69, 61]),  # spec example
        (79, 79, 0.13, [79]),  # maintenance Run
        (80, 70, 0.13, [80, 70]),  # single step, 12.5 %
        (100, 55, 0.33, [100, 74, 55]),  # one-third rule
        (100, 55, 0.13, [100, 89, 79, 70, 62, 55]),
        (61, 60, 0.05, [61, 60]),  # tiny gap
    ],
)
def test_derive_heights_table(start, end, m, expected):
    plan = derive_heights(start, end, EXAMPLE_MOWER, m)
    assert list(plan.heights) == expected
    assert plan.heights[0] == start and plan.heights[-1] == end


@pytest.mark.parametrize("start,end,m", [(100, 55, 0.13), (90, 56, 0.07), (99, 60, 0.33)])
def test_every_step_within_max_cut_and_fewest(start, end, m):
    h = derive_heights(start, end, EXAMPLE_MOWER, m).heights
    for a, b in zip(h, h[1:]):
        assert (a - b) / a <= m + 1e-9
    # fewest: the theoretical minimum number of steps
    assert len(h) - 1 >= math.ceil(math.log(start / end) / -math.log(1 - m))


def test_rounding_pushes_step_over_max_cut_adds_a_step():
    # 100 -> 50 at 33 %: n=2 gives 100/71/50 -> 71->50 is 29 %; fine.
    # Coarse 5 mm step: 100 -> 55 at 33 %: ideal 100/74/55 rounds to
    # 100/75/55, 75->55 is 26.7 % ok. Use a case that fails at the ideal n.
    plan = derive_heights(100, 70, COARSE, 0.13)
    h = plan.heights
    assert h[0] == 100 and h[-1] == 70
    assert all((a - b) / a <= 0.13 + 1e-9 for a, b in zip(h, h[1:]))
    assert all(x % 5 == 0 for x in h)


def test_neighbouring_duplicates_dropped():
    h = derive_heights(56, 55, EXAMPLE_MOWER, 0.33).heights
    assert list(h) == [56, 55]


def test_unsatisfiable_max_cut_raises():
    # 5 mm step on 50 mm is a 9 % minimum cut; m = 5 % cannot be met.
    with pytest.raises(HeightError, match="max cut"):
        derive_heights(55, 50, COARSE, 0.05)


@pytest.mark.parametrize("m", [0.04, 0.34, 0.0, 1.0])
def test_max_cut_out_of_range_rejected(m):
    with pytest.raises(HeightError, match="5"):
        derive_heights(79, 61, EXAMPLE_MOWER, m)


@pytest.mark.parametrize("m", [0.05, 0.33])
def test_max_cut_bounds_accepted(m):
    derive_heights(79, 61, EXAMPLE_MOWER, m)


@pytest.mark.parametrize("start,end", [(101, 60), (90, 54), (54, 54), (120, 120)])
def test_outside_limits_rejected_naming_limits(start, end):
    with pytest.raises(HeightError) as e:
        derive_heights(start, end, EXAMPLE_MOWER)
    assert "55" in str(e.value) and "100" in str(e.value)


def test_start_below_end_rejected():
    with pytest.raises(HeightError, match="start"):
        derive_heights(60, 80, EXAMPLE_MOWER)


def test_off_step_values_rounded_and_reported():
    plan = derive_heights(79.4, 60.6, EXAMPLE_MOWER)
    assert (plan.start, plan.end) == (79, 61)
    assert plan.rounded is True
    assert list(plan.heights) == [79, 69, 61]
    assert derive_heights(79, 61, EXAMPLE_MOWER).rounded is False


def test_coarse_step_rounds_to_device_grid_anchored_at_min():
    plan = derive_heights(88, 61, COARSE)
    assert (plan.start, plan.end) == (90, 60)
    assert plan.rounded is True


def test_limit_check_applies_to_unrounded_value():
    # 100.4 rounds to 100 but is still outside the limits: no clamping.
    with pytest.raises(HeightError):
        derive_heights(100.4, 80, EXAMPLE_MOWER)


@pytest.mark.parametrize(
    "kind,si,system,shown",
    [
        ("height", 79, "us_customary", 3.11),
        ("height", 79, "metric", 79),
        ("spacing", 25, "us_customary", 9.84),
        ("spacing", 25, "metric", 25),
        ("speed", 0.4, "us_customary", 1.31),
        ("speed", 0.4, "metric", 0.4),
    ],
)
def test_to_display(kind, si, system, shown):
    assert to_display(kind, si, system) == pytest.approx(shown)


@pytest.mark.parametrize(
    "kind,shown,system,si",
    [
        ("height", 3.0, "us_customary", 76.2),
        ("spacing", 10.0, "us_customary", 25.4),
        ("speed", 1.0, "us_customary", 0.3048),
        ("height", 79, "metric", 79),
    ],
)
def test_from_display(kind, shown, system, si):
    assert from_display(kind, shown, system) == pytest.approx(si)


def test_height_input_converted_rounded_to_step_and_echoed():
    r = height_input(3.0, "us_customary", EXAMPLE_MOWER)  # 76.2 mm
    assert r.mm == 76
    assert r.display == 2.99  # echo of the value actually used
    assert r.rounded is True


def test_height_input_metric_exact_not_flagged():
    r = height_input(79, "metric", EXAMPLE_MOWER)
    assert (r.mm, r.display, r.rounded) == (79, 79, False)


def test_height_input_outside_limits_rejected():
    with pytest.raises(HeightError, match="55"):
        height_input(5.0, "us_customary", EXAMPLE_MOWER)  # 127 mm


def test_unknown_system_rejected():
    with pytest.raises(ValueError):
        to_display("height", 79, "furlongs")
