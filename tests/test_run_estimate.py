"""A Run's estimated time from its own Spacing and speed (#108). Pure, no HA."""

import copy

import pytest

from custom_components.groundskeeper.zone_angles import (
    DEFAULT_SETTINGS,
    refresh_zone_angles,
    run_estimate_s,
)
from tests.test_zone_bearing import rect

# A 40 x 8 m Zone whose range basis is the Mower-level default (25 cm, 0.4 m/s).
ZONE = refresh_zone_angles(
    {"id": "z1", "polygon": {"type": "Polygon", "coordinates": [rect(40, 8, 30)]}},
    DEFAULT_SETTINGS,
)
BASIS_SPACING_CM = ZONE["angle_range"]["basis"]["spacing"] * 100
BASIS_SPEED = ZONE["angle_range"]["basis"]["speed"]


def test_no_override_is_the_estimate_from_the_zones_stored_basis() -> None:
    plain = run_estimate_s(ZONE, 0.0)
    assert plain is not None and plain > 0
    # Giving the basis' own Spacing and speed back changes nothing.
    assert run_estimate_s(ZONE, 0.0, spacing_cm=BASIS_SPACING_CM, speed_m_s=BASIS_SPEED) == plain


def test_a_slower_speed_takes_longer() -> None:
    assert run_estimate_s(ZONE, 0.0, speed_m_s=BASIS_SPEED / 2) > run_estimate_s(ZONE, 0.0)


def test_a_closer_spacing_takes_longer() -> None:
    assert run_estimate_s(ZONE, 0.0, spacing_cm=BASIS_SPACING_CM - 5) > run_estimate_s(ZONE, 0.0)


def test_a_faster_speed_and_a_wider_spacing_take_less_time() -> None:
    base = run_estimate_s(ZONE, 0.0)
    assert run_estimate_s(ZONE, 0.0, speed_m_s=BASIS_SPEED * 1.5) < base
    assert run_estimate_s(ZONE, 0.0, spacing_cm=BASIS_SPACING_CM + 5) < base


def test_either_value_alone_leaves_the_other_at_the_basis() -> None:
    both = run_estimate_s(ZONE, 0.0, spacing_cm=BASIS_SPACING_CM, speed_m_s=BASIS_SPEED / 2)
    assert run_estimate_s(ZONE, 0.0, speed_m_s=BASIS_SPEED / 2) == both


def test_unknown_while_the_bearing_or_outline_is_unknown_even_with_overrides() -> None:
    assert run_estimate_s({"id": "z", "polygon": None}, 0.0, speed_m_s=0.3) is None


def test_estimating_with_overrides_leaves_the_zones_range_record_alone() -> None:
    before = copy.deepcopy(ZONE)
    run_estimate_s(ZONE, 12.0, spacing_cm=20, speed_m_s=0.2)
    assert ZONE == before


# -- hand-counted: a 20 x 10 m Zone, no laps, stripes along the long side ----------


def test_the_estimate_follows_the_run_s_own_values_by_hand() -> None:
    plain = refresh_zone_angles(
        {"id": "z2", "polygon": {"type": "Polygon", "coordinates": [rect(20, 10, 0)]}},
        {**DEFAULT_SETTINGS, "spacing": 1.0, "speed": 0.4, "perimeter_laps": 0, "nogo_laps": 0},
    )
    # 10 stripes of 20 m at 1 m spacing, 8 s per turn (as tests/test_angle_range.py).
    assert run_estimate_s(plain, 0.0) == pytest.approx(200 / 0.4 + 10 * 8)  # 580
    assert run_estimate_s(plain, 0.0, speed_m_s=0.8) == pytest.approx(200 / 0.8 + 10 * 8)  # 330
    # 2 m spacing: 5 stripes of 20 m.
    assert run_estimate_s(plain, 0.0, spacing_cm=200) == pytest.approx(100 / 0.4 + 5 * 8)  # 290
