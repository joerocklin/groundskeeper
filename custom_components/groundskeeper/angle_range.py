"""Allowed angle range estimate (spec 5.4).

Pure and HA-free. Estimates mow time per relative path angle for a Zone and
derives the Allowed angle range: the contiguous span of relative angles,
containing 0 (the Zone bearing), whose estimated time is within a tolerance
of the best angle.

Relative angles are degrees clockwise from the Zone bearing (the same sense as
a compass bearing), normalised to (-90, 90]. A stripe direction is a line, so
angles 180 degrees apart are the same.

Estimate: shrink the polygon by perimeter laps x spacing, subtract obstacles
grown by no-go laps x spacing, stripe the interior at the spacing;
time = stripe length / speed + stripe count x TURN_COST_S. Lap time does not
depend on angle and is ignored.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Optional, Sequence, Union

TURN_COST_S = 8.0
DEFAULT_TOLERANCE = 0.25
ANGLE_STEP = 1.0  # degrees; resolution of the range search

_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQ = 111320.0
_MIN_STRIPE_M = 1e-3

Ring = Sequence[Sequence[float]]  # [lon, lat] pairs (GeoJSON order, WGS84)
_Pt = tuple[float, float]


@dataclass(frozen=True)
class Basis:
    """Inputs the estimate depends on; stored with the result (spec 5.4)."""

    tolerance: float = DEFAULT_TOLERANCE  # fraction, 0.25 = 25 %
    spacing: float = 0.25  # m
    speed: float = 0.4  # m/s
    perimeter_laps: int = 1
    nogo_laps: int = 1


@dataclass(frozen=True)
class AngleEstimate:
    """Estimated mow of the interior at one angle."""

    time_s: float
    stripe_count: int
    stripe_length: float  # m


@dataclass(frozen=True)
class AngleRangeResult:
    """Derived range. `angle_range` and `derived` are `{min, max}`, "full"
    or "unset". The estimates are None when the range is "unset"."""

    angle_range: Union[dict, str]
    basis: Basis
    base: Optional[AngleEstimate] = None  # at relative angle 0
    min_edge: Optional[AngleEstimate] = None
    max_edge: Optional[AngleEstimate] = None

    @property
    def derived(self) -> Union[dict, str]:
        return self.angle_range

    def to_dict(self) -> dict:
        """Storable form: `angle_range`, `derived` and `basis` (spec 5.4)."""
        return {
            "angle_range": self.angle_range,
            "derived": self.angle_range,
            "basis": asdict(self.basis),
        }


# Name used by the ticket text.
AngleRange = AngleRangeResult


def normalise_angle(rel: float) -> float:
    """Normalise a relative angle to (-90, 90]."""
    r = (rel + 90.0) % 180.0 - 90.0  # [-90, 90)
    return 90.0 if r == -90.0 else r


def estimate_at(
    ring: Ring,
    bearing: float,
    rel: float,
    *,
    obstacles: Sequence[Ring] = (),
    basis: Basis = Basis(),
) -> AngleEstimate:
    """Estimated time and stripe count for stripes at `bearing + rel`."""
    poly, obs = _project(ring, obstacles)
    return _estimate(poly, obs, bearing + rel, basis)


def double_grid_estimate(
    ring: Ring,
    bearing: float,
    rel1: float,
    rel2: float,
    *,
    obstacles: Sequence[Ring] = (),
    basis: Basis = Basis(),
) -> AngleEstimate:
    """Double grid: laps once (ignored, as elsewhere) plus both stripe passes."""
    poly, obs = _project(ring, obstacles)
    a = _estimate(poly, obs, bearing + rel1, basis)
    b = _estimate(poly, obs, bearing + rel2, basis)
    return AngleEstimate(
        a.time_s + b.time_s,
        a.stripe_count + b.stripe_count,
        a.stripe_length + b.stripe_length,
    )


def estimate_angle_range(
    ring: Optional[Ring],
    bearing: Optional[float],
    *,
    obstacles: Sequence[Ring] = (),
    basis: Basis = Basis(),
) -> AngleRangeResult:
    """Derive the Allowed angle range for a Zone polygon and bearing."""
    if bearing is None or not ring or len(ring) < 3:
        return AngleRangeResult("unset", basis)

    poly, obs = _project(ring, obstacles)
    n = int(round(180.0 / ANGLE_STEP))
    # Grid over (-90, 90]: index i is relative angle (i - n/2 + ... ) below.
    angles = [normalise_angle(-90.0 + (i + 1) * ANGLE_STEP) for i in range(n)]
    est = {a: _estimate(poly, obs, bearing + a, basis) for a in angles}
    best = min(e.time_s for e in est.values())
    limit = best * (1.0 + basis.tolerance)

    def ok(a: float) -> bool:
        return est[a].time_s <= limit + 1e-9

    steps = int(round(90.0 / ANGLE_STEP))
    up = 0  # furthest passing step above 0
    while up < steps and ok(up * ANGLE_STEP + ANGLE_STEP):
        up += 1
    down = 0
    while down < steps - 1 and ok(-(down * ANGLE_STEP + ANGLE_STEP)):
        down += 1

    base = est[0.0]
    if up == steps and down == steps - 1:
        return AngleRangeResult("full", basis, base)
    lo, hi = -down * ANGLE_STEP, up * ANGLE_STEP
    return AngleRangeResult(
        {"min": lo, "max": hi}, basis, base, est[lo], est[hi]
    )


# --- geometry ---------------------------------------------------------


def _project(ring: Ring, obstacles: Sequence[Ring]) -> tuple[list[_Pt], list[list[_Pt]]]:
    lat0 = sum(p[1] for p in ring) / len(ring)
    lon0 = sum(p[0] for p in ring) / len(ring)
    kx = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))

    def proj(r: Ring) -> list[_Pt]:
        pts = [((p[0] - lon0) * kx, (p[1] - lat0) * _M_PER_DEG_LAT) for p in r]
        if pts and pts[0] == pts[-1]:
            pts = pts[:-1]
        return pts

    return proj(ring), [proj(o) for o in obstacles if o and len(o) >= 3]


def _estimate(
    poly: list[_Pt], obstacles: list[list[_Pt]], compass: float, basis: Basis
) -> AngleEstimate:
    th = math.radians(compass)
    ux, uy = math.sin(th), math.cos(th)  # along stripes (east, north)
    nx, ny = uy, -ux  # across stripes (clockwise normal)

    def rot(pts: list[_Pt]) -> list[_Pt]:
        return [(x * ux + y * uy, x * nx + y * ny) for x, y in pts]

    P = rot(poly)
    O = [rot(o) for o in obstacles]
    d_in = basis.perimeter_laps * basis.spacing
    d_no = basis.nogo_laps * basis.spacing
    vs = [p[1] for p in P]
    lo, hi = min(vs), max(vs)
    omin = [(min(p[1] for p in o), max(p[1] for p in o)) for o in O]

    count = 0
    length = 0.0
    v = lo + basis.spacing / 2.0
    while v < hi:
        ivs = _crossings(P, v)
        if d_in > 0:
            ivs = _subtract(ivs, _near(P, v, d_in))
        for o, (olo, ohi) in zip(O, omin):
            if olo - d_no <= v <= ohi + d_no:
                cut = _crossings(o, v)
                if d_no > 0:
                    cut = _union(cut + _near(o, v, d_no))
                ivs = _subtract(ivs, cut)
        for a, b in ivs:
            if b - a > _MIN_STRIPE_M:
                count += 1
                length += b - a
        v += basis.spacing
    return AngleEstimate(length / basis.speed + count * TURN_COST_S, count, length)


Interval = tuple[float, float]


def _crossings(P: list[_Pt], v: float) -> list[Interval]:
    """Inside intervals (even-odd) of polygon P along the line y = v."""
    xs = []
    n = len(P)
    for i in range(n):
        x1, y1 = P[i]
        x2, y2 = P[(i + 1) % n]
        if (y1 <= v) != (y2 <= v):
            xs.append(x1 + (v - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    return [(xs[j], xs[j + 1]) for j in range(0, len(xs) - 1, 2)]


def _near(P: list[_Pt], v: float, d: float) -> list[Interval]:
    """Intervals of y = v within distance d of the polygon boundary."""
    out: list[Interval] = []
    n = len(P)
    for i in range(n):
        ax, ay = P[i]
        bx, by = P[(i + 1) % n]
        if v < min(ay, by) - d or v > max(ay, by) + d:
            continue
        for cx, cy in ((ax, ay), (bx, by)):  # end caps
            h = d * d - (cy - v) ** 2
            if h >= 0:
                r = math.sqrt(h)
                out.append((cx - r, cx + r))
        out.extend(_slab(ax, ay, bx, by, v, d))
    return _union(out)


def _slab(ax: float, ay: float, bx: float, by: float, v: float, d: float) -> list[Interval]:
    """Part of y = v within d of segment AB's interior projection (the body
    of the stadium): 0 <= s <= L and |p| <= d, both linear in x."""
    dx, dy = bx - ax, by - ay
    seg = math.hypot(dx, dy)
    if seg == 0.0:
        return []
    # s(x) = ((x-ax)*dx + (v-ay)*dy)/seg ; p(x) = (-(x-ax)*dy + (v-ay)*dx)/seg
    lo, hi = -math.inf, math.inf
    for k, b0, lim_lo, lim_hi in (
        (dx / seg, ((v - ay) * dy - ax * dx) / seg, 0.0, seg),
        (-dy / seg, ((v - ay) * dx + ax * dy) / seg, -d, d),
    ):
        if abs(k) < 1e-12:
            if not (lim_lo <= b0 <= lim_hi):
                return []
            continue
        t1, t2 = (lim_lo - b0) / k, (lim_hi - b0) / k
        if t1 > t2:
            t1, t2 = t2, t1
        lo, hi = max(lo, t1), min(hi, t2)
    return [(lo, hi)] if lo < hi else []


def _union(ivs: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _subtract(ivs: list[Interval], cut: list[Interval]) -> list[Interval]:
    if not cut:
        return ivs
    cut = _union(cut)
    out: list[Interval] = []
    for a, b in ivs:
        cur = a
        for c, e in cut:
            if e <= cur or c >= b:
                continue
            if c > cur:
                out.append((cur, c))
            cur = max(cur, e)
        if cur < b:
            out.append((cur, b))
    return out
