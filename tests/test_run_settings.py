"""Settings layering and the Finishing Run override (spec §5.1, §5.6). Pure."""

import pytest

from custom_components.mowing_director.run_settings import (
    SettingsError,
    finishing_set,
    resolve_run_settings,
    validate_overrides,
)
from tests.fake_mower import FakeMower

LIMITS = FakeMower().limits


def resolve(config=None, zone=None, plan=None, run=None, finishing=None):
    return resolve_run_settings(
        LIMITS, config or {}, zone or {}, plan_settings=plan, run_settings=run,
        finishing=finishing,
    )


def test_builtin_defaults_when_nothing_is_set() -> None:
    s = resolve()
    assert s["crossing_angle_deg"] == 45.0
    assert s["min_separation_deg"] == 15.0
    assert s["perimeter_laps"] == 1


def test_most_specific_layer_wins_for_a_portable_setting() -> None:
    config = {"run_defaults": {"spacing_cm": 26}}
    zone = {"run_defaults": {"spacing_cm": 27}}
    assert resolve(config)["spacing_cm"] == 26
    assert resolve(config, zone)["spacing_cm"] == 27
    assert resolve(config, zone, plan={"spacing_cm": 28})["spacing_cm"] == 28
    assert resolve(config, zone, plan={"spacing_cm": 28}, run={"spacing_cm": 29})[
        "spacing_cm"
    ] == 29


def test_adapter_extras_layer_key_by_key() -> None:
    key = LIMITS.extras_schema[0].key
    default = LIMITS.extras_schema[0].default
    other = LIMITS.extras_schema[0].selector.config["options"][-1]["value"]
    config = {"run_defaults": {"extras": {key: other}}}
    assert resolve()["extras"][key] == default
    assert resolve(config)["extras"][key] == other
    assert resolve(config, {"run_defaults": {"extras": {key: default}}})["extras"][key] == default


def test_mower_defaults_clamp_to_device_limits() -> None:
    s = resolve({"run_defaults": {"spacing_cm": 9999}})
    assert s["spacing_cm"] == LIMITS.spacing_max_cm


def test_finishing_set_prefers_zone_then_falls_back_to_mower() -> None:
    config = {"finishing": {"speed_m_s": 0.3}}
    assert finishing_set(config, {}) == {"speed_m_s": 0.3}
    assert finishing_set(config, {"finishing": {"speed_m_s": 0.2}}) == {"speed_m_s": 0.2}
    assert finishing_set({}, {}) is None


def test_finishing_set_sits_between_zone_default_and_plan_override() -> None:
    zone = {"run_defaults": {"speed_m_s": 0.5, "spacing_cm": 27}}
    fin = {"speed_m_s": 0.3}
    s = resolve(zone=zone, finishing=fin)
    assert s["speed_m_s"] == 0.3 and s["spacing_cm"] == 27
    assert resolve(zone=zone, finishing=fin, plan={"speed_m_s": 0.4})["speed_m_s"] == 0.4


@pytest.mark.parametrize(
    "bad",
    [
        {"spacing_cm": 1000},
        {"speed_m_s": 99},
        {"perimeter_laps": -1},
        {"extras": {"no_such_extra": 1}},
        {"nonsense": 1},
    ],
)
def test_invalid_overrides_are_rejected_not_clamped(bad) -> None:
    with pytest.raises(SettingsError):
        validate_overrides(LIMITS, bad)


def test_valid_overrides_pass() -> None:
    validate_overrides(LIMITS, {"spacing_cm": LIMITS.spacing_min_cm, "perimeter_laps": 2})
