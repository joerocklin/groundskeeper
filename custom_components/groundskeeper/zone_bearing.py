"""Zone bearing from a Zone polygon (spec §4.5).

Pure and HA-free. The bearing is the compass bearing of the long axis of
the polygon's minimum-area rectangle, folded to 0-180 degrees. Warnings are
advisory only; they never block anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

AMBIGUOUS_ASPECT = 1.2
IRREGULAR_FILL = 0.5

_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQ = 111320.0

Ring = Sequence[Sequence[float]]  # [lon, lat] pairs (GeoJSON order, WGS84)


@dataclass(frozen=True)
class ZoneBearing:
    """Derived bearing (degrees, 0 <= b < 180) with quality metrics.

    `bearing` is None when there is no usable polygon (warning `unset`).
    `aspect` is long/short side of the minimum-area rectangle; `fill` is
    polygon area / rectangle area.
    """

    bearing: float | None
    aspect: float | None
    fill: float | None
    warnings: tuple[str, ...]


_UNSET = ZoneBearing(None, None, None, ("unset",))


def zone_bearing(ring: Ring | None) -> ZoneBearing:
    """Return the Zone bearing and warnings for a WGS84 [lon, lat] ring."""
    if not ring or len(ring) < 3:
        return _UNSET

    lat0 = sum(p[1] for p in ring) / len(ring)
    lon0 = sum(p[0] for p in ring) / len(ring)
    kx = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
    pts = [((p[0] - lon0) * kx, (p[1] - lat0) * _M_PER_DEG_LAT) for p in ring]

    hull = _convex_hull(pts)
    area = abs(_shoelace(pts))
    if len(hull) < 3 or area <= 0.0:
        return _UNSET

    best = None  # (rect_area, long_dx, long_dy, long_len, short_len)
    for i in range(len(hull)):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % len(hull)]
        el = math.hypot(x2 - x1, y2 - y1)
        if el == 0.0:
            continue
        ux, uy = (x2 - x1) / el, (y2 - y1) / el
        us = [x * ux + y * uy for x, y in hull]
        ws = [-x * uy + y * ux for x, y in hull]
        a, b = max(us) - min(us), max(ws) - min(ws)
        if best is None or a * b < best[0]:
            best = (a * b, ux, uy, a, b)
    assert best is not None
    rect_area, ux, uy, a, b = best
    if a < b:  # long axis is the other side
        ux, uy, a, b = -uy, ux, b, a
    if b <= 0.0:
        return _UNSET

    # x = east, y = north: compass bearing = atan2(east, north), folded.
    bearing = math.degrees(math.atan2(ux, uy)) % 180.0
    aspect = a / b
    fill = min(area / rect_area, 1.0)

    warnings = []
    if aspect < AMBIGUOUS_ASPECT:
        warnings.append("ambiguous")
    if fill < IRREGULAR_FILL:
        warnings.append("irregular")
    return ZoneBearing(bearing, aspect, fill, tuple(warnings))


def _shoelace(pts: list[tuple[float, float]]) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _convex_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain, counter-clockwise, no repeated endpoint."""
    p = sorted(set(pts))
    if len(p) < 3:
        return p

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for q in p:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    upper: list[tuple[float, float]] = []
    for q in reversed(p):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return lower[:-1] + upper[:-1]
