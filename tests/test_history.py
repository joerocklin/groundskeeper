"""Pure retention rules (spec §14.6): ranges, clamping, warning thresholds."""

import pytest

from custom_components.mowing_director.history import (
    capability_warnings,
    retention_limits,
    validate_limits,
)

OK = {"history_runs_max": 500, "duration_samples_max": 20, "battery_samples_max": 5}


def codes(**over):
    return [w["code"] for w in capability_warnings({**OK, **over}, 10, 14)]


def test_defaults_and_clamping() -> None:
    assert retention_limits(None) == OK
    assert retention_limits({"history_runs_max": 9999, "battery_samples_max": -1}) == {
        **OK, "history_runs_max": 2000, "battery_samples_max": 0}


@pytest.mark.parametrize("bad", [{"history_runs_max": 2001}, {"duration_samples_max": 101},
                                 {"battery_samples_max": 21}, {"history_runs_max": -1},
                                 {"nope": 1}, {"history_runs_max": True}])
def test_validate_rejects_out_of_range(bad) -> None:
    with pytest.raises(ValueError):
        validate_limits(bad)


def test_range_edges_are_valid() -> None:
    validate_limits({"history_runs_max": 0, "duration_samples_max": 100, "battery_samples_max": 20})


def test_warning_thresholds() -> None:
    assert codes() == []
    assert codes(history_runs_max=0) == ["history_empty"]
    assert codes(history_runs_max=9) == ["angle_history_short"]
    assert codes(duration_samples_max=2) == ["run_long_never"]
    assert codes(duration_samples_max=3) == ["run_long_noisy"]
    assert codes(duration_samples_max=9) == ["run_long_noisy"]
    assert codes(duration_samples_max=10) == []
    assert codes(battery_samples_max=0) == ["recharge_warnings_off"]
    assert codes(battery_samples_max=2) == ["recharge_timing_less_reliable"]
    assert codes(battery_samples_max=3) == []
