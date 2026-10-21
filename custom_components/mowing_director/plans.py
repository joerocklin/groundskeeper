"""Plan derivation for `add_plan` and re-derivation (spec §5.1-5.3, §5.5, §5.6).

Pure and HA-free. A Plan is derived from its inputs (start/end height, max cut,
base angle, Finishing flag, setting overrides): geometric step-down heights,
angles drawn once, and every Run's settings resolved in full (SI) and copied
onto the Run. The inputs are stored on the Plan so it can be re-derived from
current defaults until it starts.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import ulid as ulid_util

from .adapter import Limits
from .angle_draw import DEFAULT_FINISH_JITTER, draw_angles
from .core import iso, parse
from .heights import DEFAULT_MAX_CUT, HeightLimits, HeightPlan, derive_heights
from .run_settings import (
    finishing_set,
    mower_run_defaults,  # noqa: F401 - re-exported
    resolve_run_settings,
    validate_overrides,
)

DEFAULT_HISTORY_DAYS = 14  # spec §5.5


class AmbiguousZone(ValueError):
    """More than one Zone has that name."""


class UnknownZone(ValueError):
    """No Zone has that id or name."""


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


@dataclass(frozen=True)
class BuiltPlan:
    """A derived Plan, its Runs, the height derivation and any warnings."""

    plan: dict[str, Any]
    runs: list[dict[str, Any]]
    heights: HeightPlan
    warnings: list[str] = field(default_factory=list)


def _history_angles(
    history_runs: Sequence[Mapping[str, Any]], zone_id: str, now: datetime, days: float
) -> tuple[list[float], list[float]]:
    """Angles of done Runs on the Zone within the window, and its last 3 finals."""
    done = [
        r for r in history_runs
        if r.get("zone_id") == zone_id and r.get("status") == "done" and r.get("finished_at")
    ]
    done.sort(key=lambda r: r["finished_at"])
    cutoff = now - timedelta(days=days)
    recent = [r["angle_deg"] for r in done if parse(r["finished_at"]) >= cutoff]
    finals = [r["angle_deg"] for r in done if r["index"] == r["of"]][-3:]
    return recent, finals


def build_plan(
    *,
    zone: Mapping[str, Any],
    config: Mapping[str, Any],
    limits: Limits,
    start: float,
    end: float,
    origin: str,
    now: datetime,
    max_cut_pct: float | None = None,
    base_angle: float | None = None,
    finishing: bool | None = None,
    plan_settings: Mapping[str, Any] | None = None,
    history_runs: Sequence[Mapping[str, Any]] = (),
    rng: random.Random | None = None,
    new_id: Callable[[], str] = ulid_util.ulid,
    plan_id: str | None = None,
    created_at: str | None = None,
) -> BuiltPlan:
    """Derive a Plan and its Runs. Raises HeightError / SettingsError."""
    plan_settings = dict(plan_settings or {})
    validate_overrides(limits, plan_settings)
    if max_cut_pct is None:
        max_cut_pct = config.get("max_cut_pct", DEFAULT_MAX_CUT * 100)
    heights = derive_heights(start, end, height_limits(limits), max_cut_pct / 100)
    count = len(heights.heights)

    fin = finishing_set(config, zone)
    # Never on a single maintenance Run; the flag forces it on or off (§5.6).
    use_finishing = fin is not None and count >= 2 and finishing is not False

    def settings_for(i: int) -> dict[str, Any]:
        s = resolve_run_settings(
            limits, config, zone, plan_settings=plan_settings,
            finishing=fin if use_finishing and i == count - 1 else None,
        )
        return s

    base = settings_for(0)
    mode = base["base_angle_mode"]
    base_deg = base["base_angle_deg"] if base_angle is None else base_angle
    window = config.get("history_window_days", DEFAULT_HISTORY_DAYS)
    recent, finals = _history_angles(history_runs, zone["id"], now, window)
    draw = draw_angles(
        (zone.get("angle_range") or {}).get("angle_range", "unset"),
        count,
        base_angle_mode=mode,
        base_angle=base_deg,
        finish_jitter=config.get("finish_jitter", DEFAULT_FINISH_JITTER),
        last_finals=finals,
        crossing_angle=base["crossing_angle_deg"],
        min_separation=base["min_separation_deg"],
        history_angles=recent,
        rng=rng,
    )
    record = draw.to_dict()

    plan_id = plan_id or new_id()
    runs: list[dict[str, Any]] = []
    for index, height in enumerate(heights.heights, start=1):
        s = settings_for(index - 1)
        runs.append(
            {
                "id": new_id(),
                "plan_id": plan_id,
                "zone_id": zone["id"],
                "zone_name": zone["name"],
                "status": "pending",
                "reason": None,
                "index": index,
                "of": count,
                "height_mm": height,
                "angle_deg": draw.angles[index - 1],
                "angle_draw": record,
                "settings": {
                    "blade_height_mm": int(height),
                    **{k: s[k] for k in (
                        "spacing_cm", "speed_m_s", "perimeter_laps",
                        "crossing_angle_deg", "min_separation_deg",
                    )},
                    "extras": dict(s["extras"]),
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
        "base_angle": base_angle,
        "finishing": finishing,
        "settings": plan_settings,
        "created_at": created_at or iso(now),
        "run_ids": [r["id"] for r in runs],
    }
    return BuiltPlan(plan, runs, heights, list(draw.warnings))


def rederive_plan(
    plan: Mapping[str, Any],
    *,
    zone: Mapping[str, Any],
    config: Mapping[str, Any],
    limits: Limits,
    now: datetime,
    history_runs: Sequence[Mapping[str, Any]] = (),
    rng: random.Random | None = None,
    new_id: Callable[[], str] = ulid_util.ulid,
) -> BuiltPlan:
    """The same Plan (id, origin, created_at) rebuilt from its stored inputs and
    the current defaults (spec §5.1). Only meaningful before it starts."""
    return build_plan(
        zone=zone,
        config=config,
        limits=limits,
        start=plan["start_height_mm"],
        end=plan["end_height_mm"],
        origin=plan["origin"],
        now=now,
        max_cut_pct=round(plan["max_cut"] * 100, 6),
        base_angle=plan.get("base_angle"),
        finishing=plan.get("finishing"),
        plan_settings=plan.get("settings"),
        history_runs=history_runs,
        rng=rng,
        new_id=new_id,
        plan_id=plan["id"],
        created_at=plan["created_at"],
    )
