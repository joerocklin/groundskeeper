"""Plan building for `add_plan` (spec §5.1, §13.3). Pure and HA-free.

Tracer-bullet scope: a Plan is exactly one Run (start height = end height),
at path angle 0 relative to the Zone bearing, with settings resolved from
Mower defaults. Step-down heights and angles arrive in later tickets.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from homeassistant.util import ulid as ulid_util

from .adapter import Limits
from .angle_range import Basis
from .core import iso
from .heights import HeightLimits, HeightPlan, derive_heights


class AmbiguousZone(ValueError):
    """More than one Zone has that name."""


class UnknownZone(ValueError):
    """No Zone has that id or name."""


class NeedsStepDown(ValueError):
    """The heights need more than one Run (a later ticket)."""


def height_limits(limits: Limits) -> HeightLimits:
    """Height part of the adapter limits descriptor."""
    return HeightLimits(
        float(limits.height_min_mm), float(limits.height_max_mm), float(limits.height_step_mm)
    )


def find_zone(zones: list[dict[str, Any]], ref: str) -> dict[str, Any]:
    """Resolve a Zone by ULID, else by exact case-insensitive name."""
    for zone in zones:
        if zone["id"] == ref:
            return zone
    matches = [z for z in zones if z["name"].casefold() == ref.casefold()]
    if not matches:
        raise UnknownZone(ref)
    if len(matches) > 1:
        raise AmbiguousZone(ref)
    return matches[0]


def mower_run_defaults(limits: Limits, config: Mapping[str, Any]) -> dict[str, Any]:
    """Mower default Run settings (SI), clamped to the device limits.

    `config["run_defaults"]` overrides the built-ins; the panel edits it in a
    later ticket.
    """
    overrides = config.get("run_defaults") or {}
    spacing = overrides.get("spacing_cm", Basis.spacing * 100)
    speed = overrides.get("speed_m_s", Basis.speed)
    return {
        "spacing_cm": min(max(spacing, limits.spacing_min_cm), limits.spacing_max_cm),
        "speed_m_s": min(max(speed, limits.speed_min_m_s), limits.speed_max_m_s),
        "perimeter_laps": overrides.get("perimeter_laps", Basis.perimeter_laps),
        "extras": {f.key: f.default for f in limits.extras_schema}
        | dict(overrides.get("extras") or {}),
    }


def derive_single_run_heights(start: float, end: float, limits: Limits) -> HeightPlan:
    """Validated heights for a one-Run Plan; raises HeightError / NeedsStepDown."""
    plan = derive_heights(start, end, height_limits(limits))
    if len(plan.heights) != 1:
        raise NeedsStepDown
    return plan


def build_plan(
    *,
    zone: Mapping[str, Any],
    heights: HeightPlan,
    defaults: Mapping[str, Any],
    origin: str,
    now: datetime,
    new_id: Callable[[], str] = ulid_util.ulid,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A Plan record and its Runs, settings copied in full at creation (§5.1)."""
    plan_id = new_id()
    runs: list[dict[str, Any]] = []
    of = len(heights.heights)
    for index, height in enumerate(heights.heights, start=1):
        runs.append(
            {
                "id": new_id(),
                "plan_id": plan_id,
                "zone_id": zone["id"],
                "zone_name": zone["name"],
                "status": "pending",
                "reason": None,
                "index": index,
                "of": of,
                "height_mm": height,
                "angle_deg": 0.0,
                "settings": {
                    "blade_height_mm": int(height),
                    "spacing_cm": defaults["spacing_cm"],
                    "speed_m_s": defaults["speed_m_s"],
                    "perimeter_laps": defaults["perimeter_laps"],
                    "extras": dict(defaults["extras"]),
                },
                "created_at": iso(now),
            }
        )
    plan = {
        "id": plan_id,
        "zone_id": zone["id"],
        "zone_name": zone["name"],
        "origin": origin,
        "start_height_mm": heights.start,
        "end_height_mm": heights.end,
        "max_cut": heights.max_cut,
        "base_angle": None,
        "created_at": iso(now),
        "run_ids": [r["id"] for r in runs],
    }
    return plan, runs
