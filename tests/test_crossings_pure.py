"""Learned recharge thresholds and warning prediction (spec §10), pure."""

from datetime import datetime, timedelta, timezone

from custom_components.mowing_director.crossings import (
    battery_slope,
    effective_level,
    should_warn,
)

T0 = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)


def pts(*pairs):
    """(minutes, battery) -> history points."""
    return [(T0 + timedelta(minutes=m), b) for m, b in pairs]


def test_median_of_the_samples_once_there_are_three() -> None:
    assert effective_level("return", [30, 20, 26], configured=None) == (26, "learned")


def test_fewer_than_three_samples_fall_back_to_the_configured_level() -> None:
    assert effective_level("return", [30, 20], configured=22) == (22, "configured")


def test_smart_mower_falls_back_to_25_and_95() -> None:
    assert effective_level("return", [], configured=None) == (25, "default")
    assert effective_level("resume", [], configured=None) == (95, "default")


def test_slope_over_the_window_in_points_per_minute() -> None:
    assert battery_slope(pts((0, 50), (5, 45), (10, 40)), T0 + timedelta(minutes=10)) == -1.0


def test_slope_ignores_points_older_than_ten_minutes() -> None:
    history = pts((0, 90), (12, 50), (22, 40))
    assert battery_slope(history, T0 + timedelta(minutes=22)) == -1.0


def test_no_slope_from_one_point_or_a_tiny_span() -> None:
    assert battery_slope(pts((0, 50)), T0) is None
    assert battery_slope(pts((0, 50), (0.5, 49)), T0 + timedelta(minutes=1)) is None


LEAD = timedelta(minutes=10)


def test_return_warns_when_the_level_is_ten_minutes_away() -> None:
    history = pts((0, 36), (10, 28))  # -0.8 %/min, 3 above 25 -> under 4 min
    assert should_warn("return", 28, 25, history, T0 + timedelta(minutes=10), LEAD)
    far = pts((0, 60), (10, 52))  # 27 above 25 -> 34 min away
    assert not should_warn("return", 52, 25, far, T0 + timedelta(minutes=10), LEAD)


def test_return_slope_warns_ahead_of_the_five_point_margin() -> None:
    history = pts((0, 50), (10, 30))  # -2 %/min, 5 above -> 2.5 min; 30 is at the margin
    assert should_warn("return", 30, 25, history, T0 + timedelta(minutes=10), LEAD)
    steep = pts((0, 60), (10, 40))  # -2 %/min, 15 above -> 7.5 min
    assert should_warn("return", 40, 25, steep, T0 + timedelta(minutes=10), LEAD)


def test_without_a_usable_slope_the_margin_is_five_points() -> None:
    now = T0 + timedelta(minutes=10)
    assert should_warn("return", 30, 25, [], now, LEAD)
    assert not should_warn("return", 31, 25, [], now, LEAD)
    flat = pts((0, 31), (10, 31))
    assert not should_warn("return", 31, 25, flat, now, LEAD)


def test_resume_warns_on_a_rising_battery_and_the_margin() -> None:
    now = T0 + timedelta(minutes=10)
    rising = pts((0, 80), (10, 90))  # +1/min, 5 below 95 -> 5 min
    assert should_warn("resume", 90, 95, rising, now, LEAD)
    assert not should_warn("resume", 60, 95, pts((0, 55), (10, 60)), now, LEAD)
    assert should_warn("resume", 90, 95, [], now, LEAD)
    assert not should_warn("resume", 89, 95, [], now, LEAD)
