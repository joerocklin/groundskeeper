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

from .angle_range import DEFAULT_TOLERANCE, Basis, estimate_angle_range
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
