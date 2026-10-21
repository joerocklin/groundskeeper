"""Run settings layers and the Finishing Run override (spec §5.1, §5.6).

Pure and HA-free. Settings resolve Mower default -> Zone default ->
(Finishing set, last Run only) -> Plan -> Run, the most specific winning, for
every portable setting and adapter extra. Everything is SI. Plan creation
calls `resolve_run_settings` once per Run and stores the full result on the
Run, so later edits to a default never reach a Plan already built.

The Finishing set sits above the Zone default (it is a deliberate, more
specific choice for the last Run) and below the Plan, so an explicit `settings`
override on `add_plan` still wins.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .adapter import Limits
from .angle_draw import DEFAULT_CROSSING, DEFAULT_SEPARATION
from .angle_range import Basis

BASE_ANGLE_MODES = ("fixed", "random")
# Keys any layer may set besides `extras`.
SCALAR_KEYS = (
    "spacing_cm",
    "speed_m_s",
    "perimeter_laps",
    "crossing_angle_deg",
    "min_separation_deg",
    "base_angle_mode",
    "base_angle_deg",
)
# Stored on each Run's `settings` (the angle-mode keys live on the Plan).
RUN_KEYS = (
    "spacing_cm",
    "speed_m_s",
    "perimeter_laps",
    "crossing_angle_deg",
    "min_separation_deg",
)


class SettingsError(ValueError):
    """A settings override was rejected."""


def mower_run_defaults(limits: Limits, config: Mapping[str, Any]) -> dict[str, Any]:
    """Mower default Run settings, built-ins overlaid by `config["run_defaults"]`.

    Spacing and speed are clamped to the device limits (the Mower default is
    the user's own choice; an out-of-range one must not break Plan creation).
    """
    o = config.get("run_defaults") or {}
    spacing = o.get("spacing_cm", Basis.spacing * 100)
    speed = o.get("speed_m_s", Basis.speed)
    return {
        "spacing_cm": min(max(spacing, limits.spacing_min_cm), limits.spacing_max_cm),
        "speed_m_s": min(max(speed, limits.speed_min_m_s), limits.speed_max_m_s),
        "perimeter_laps": o.get("perimeter_laps", Basis.perimeter_laps),
        "crossing_angle_deg": o.get("crossing_angle_deg", DEFAULT_CROSSING),
        "min_separation_deg": o.get("min_separation_deg", DEFAULT_SEPARATION),
        "base_angle_mode": o.get("base_angle_mode", "fixed"),
        "base_angle_deg": o.get("base_angle_deg", 0.0),
        "extras": {f.key: f.default for f in limits.extras_schema}
        | dict(o.get("extras") or {}),
    }


def finishing_set(
    config: Mapping[str, Any], zone: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """The Finishing Run override: the Zone's, else the Mower's, else None."""
    return zone.get("finishing") or config.get("finishing") or None


def _overlay(base: dict[str, Any], layer: Mapping[str, Any] | None) -> dict[str, Any]:
    if not layer:
        return base
    out = {**base, **{k: v for k, v in layer.items() if k != "extras"}}
    out["extras"] = {**base["extras"], **dict(layer.get("extras") or {})}
    return out


def resolve_run_settings(
    limits: Limits,
    config: Mapping[str, Any],
    zone: Mapping[str, Any],
    *,
    plan_settings: Mapping[str, Any] | None = None,
    run_settings: Mapping[str, Any] | None = None,
    finishing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Every setting for one Run, fully resolved (SI). `finishing` is the set
    to apply, or None; the caller decides whether this is the last Run."""
    out = mower_run_defaults(limits, config)
    for layer in (zone.get("run_defaults"), finishing, plan_settings, run_settings):
        out = _overlay(out, layer)
    return out


def validate_overrides(limits: Limits, overrides: Mapping[str, Any]) -> None:
    """Reject (never clamp) an override outside the device limits."""
    schema = {f.key for f in limits.extras_schema}
    for key, value in overrides.items():
        if key == "extras":
            unknown = sorted(set(value) - schema)
            if unknown:
                raise SettingsError(f"unknown extra setting(s): {', '.join(unknown)}")
        elif key == "spacing_cm":
            if not limits.spacing_min_cm <= value <= limits.spacing_max_cm:
                raise SettingsError(
                    f"spacing {value:g} cm is outside the device limits "
                    f"({limits.spacing_min_cm:g}-{limits.spacing_max_cm:g} cm)"
                )
        elif key == "speed_m_s":
            if not limits.speed_min_m_s <= value <= limits.speed_max_m_s:
                raise SettingsError(
                    f"speed {value:g} m/s is outside the device limits "
                    f"({limits.speed_min_m_s:g}-{limits.speed_max_m_s:g} m/s)"
                )
        elif key == "perimeter_laps":
            if int(value) != value or value < 0:
                raise SettingsError("perimeter_laps must be a whole number, 0 or more")
        elif key in ("crossing_angle_deg", "min_separation_deg"):
            if not 0 <= value <= 90:
                raise SettingsError(f"{key} must be between 0 and 90")
        elif key == "base_angle_mode":
            if value not in BASE_ANGLE_MODES:
                raise SettingsError("base_angle_mode must be fixed or random")
        elif key == "base_angle_deg":
            if not -90 <= value <= 90:
                raise SettingsError("base_angle_deg must be between -90 and 90")
        else:
            raise SettingsError(f"unknown setting {key!r}")
