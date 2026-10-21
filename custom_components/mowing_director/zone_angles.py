"""Zone bearing and Allowed angle range records (spec §4.5, §5.4, §14.3).

Pure and HA-free. `refresh_zone_angles` recomputes the derived parts of a
Zone's `bearing` and `angle_range` records from its polygon and the Mower's
range settings, leaving manual values and the per-Zone tolerance override
alone.

    bearing:     value, source (derived|manual), derived_value, ambiguous,
                 irregular, status (pending|unset|ready)
    angle_range: angle_range ({min,max}|"full"|"unset"), source, derived,
                 basis, tolerance_override
"""

from __future__ import annotations

from typing import Any

from dataclasses import asdict

from .angle_range import DEFAULT_TOLERANCE, Basis, estimate_angle_range, estimate_at
from .zone_bearing import zone_bearing

DEFAULT_SETTINGS: dict[str, Any] = {
    "range_tolerance": DEFAULT_TOLERANCE,
    "spacing": Basis.spacing,  # m
    "speed": Basis.speed,  # m/s
    "perimeter_laps": Basis.perimeter_laps,
    "nogo_laps": Basis.nogo_laps,
}


def _ring(polygon: dict | None) -> list | None:
    if not polygon:
        return None
    coords = polygon.get("coordinates")
    if polygon.get("type") == "MultiPolygon" and coords:
        coords = coords[0]
    return coords[0] if coords else None


def refresh_zone_angles(zone: dict, settings: dict) -> dict:
    """Return a copy of `zone` with fresh `bearing` and `angle_range`."""
    zone = dict(zone)
    ring = _ring(zone.get("polygon"))
    zb = zone_bearing(ring)

    old_b = zone.get("bearing") or {}
    manual_b = old_b.get("source") == "manual"
    value = old_b.get("value") if manual_b else zb.bearing
    if value is not None:
        status = "ready"
    else:
        status = "pending" if zone.get("polygon") is None else "unset"
    zone["bearing"] = {
        "value": value,
        "source": "manual" if manual_b else "derived",
        "derived_value": zb.bearing,
        "ambiguous": "ambiguous" in zb.warnings,
        "irregular": "irregular" in zb.warnings,
        "status": status,
    }

    old_r = zone.get("angle_range") or {}
    override = old_r.get("tolerance_override")
    merged = {**DEFAULT_SETTINGS, **settings}
    basis = Basis(
        tolerance=merged["range_tolerance"] if override is None else override,
        spacing=merged["spacing"],
        speed=merged["speed"],
        perimeter_laps=merged["perimeter_laps"],
        nogo_laps=merged["nogo_laps"],
    )
    derived = estimate_angle_range(ring, value, basis=basis).angle_range
    manual_r = old_r.get("source") == "manual"
    zone["angle_range"] = {
        "angle_range": old_r["angle_range"] if manual_r else derived,
        "source": "manual" if manual_r else "derived",
        "derived": derived,
        "basis": {
            "tolerance": basis.tolerance,
            "spacing": basis.spacing,
            "speed": basis.speed,
            "perimeter_laps": basis.perimeter_laps,
            "nogo_laps": basis.nogo_laps,
        },
        "tolerance_override": override,
    }
    return zone


def angle_estimates(zone: dict) -> dict | None:
    """Estimated mow time at the base angle and at each edge of the Zone's
    effective Allowed angle range (spec 5.4), for the panel.

    Each entry is `{time_s, stripe_count, stripe_length}`; the edges are None
    when the range is `full`. None overall while the bearing or range is
    unset. Uses the basis stored on the record, so it matches the range."""
    rec = zone.get("angle_range") or {}
    rng = rec.get("angle_range")
    bearing = (zone.get("bearing") or {}).get("value")
    ring = _ring(zone.get("polygon"))
    if bearing is None or ring is None or rng in (None, "unset"):
        return None
    basis = Basis(**rec["basis"])

    def at(rel: float) -> dict:
        return asdict(estimate_at(ring, bearing, rel, basis=basis))

    if rng == "full":
        return {"base": at(0.0), "min_edge": None, "max_edge": None}
    return {
        "base": at(0.0),
        "min_edge": at(rng["min"]),
        "max_edge": at(rng["max"]),
    }


def run_estimate_s(zone: dict, rel_angle: float) -> float | None:
    """Estimated mow time of one Run on `zone` at a path angle relative to the
    bearing (spec 5.4/5.5), or None while the bearing or outline is unknown.
    Uses the basis stored on the range record."""
    rec = zone.get("angle_range") or {}
    bearing = (zone.get("bearing") or {}).get("value")
    ring = _ring(zone.get("polygon"))
    if bearing is None or ring is None or "basis" not in rec:
        return None
    return estimate_at(ring, bearing, rel_angle, basis=Basis(**rec["basis"])).time_s
