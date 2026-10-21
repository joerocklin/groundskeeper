"""Angle draw (spec 5.5).

Pure and HA-free. Draws a Plan's relative path angles from a Zone's Allowed
angle range under the Crossing angle, minimum separation, history avoidance
and Base angle rules, and records what it did. Never refuses: when the rules
cannot all hold it relaxes them in the order history, separation, crossing
and says so.

Angles are degrees relative to the Zone bearing, normalised to (-90, 90]. A
stripe direction is a line, so distances wrap at 180 degrees. Randomness is
injected as `rng` (anything with `choice(seq)`, e.g. `random.Random(seed)`).
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence, Union

DEFAULT_CROSSING = 45.0
DEFAULT_SEPARATION = 15.0
DEFAULT_FINISH_JITTER = 5.0
FINISH_MIN_DIFF = 2.0  # from each of the last 3 final angles
GRID_STEP = 0.5  # degrees; candidate resolution
DEFAULT_DRAWS = 8  # random valid draws compared for "farthest from history"

_EPS = 1e-9


def normalise_angle(rel: float) -> float:
    """Normalise a relative angle to (-90, 90]."""
    r = (rel + 90.0) % 180.0 - 90.0  # [-90, 90)
    return 90.0 if r == -90.0 else r


def angular_distance(a: float, b: float) -> float:
    """Distance between two stripe directions, 0..90."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


@dataclass(frozen=True)
class Relaxation:
    """One rule that had to be loosened. `kind` is history, separation,
    crossing or finish_difference; `run_index` is 0-based."""

    run_index: int
    kind: str
    requested: Optional[float]
    applied: Optional[float]
    message: str


@dataclass(frozen=True)
class AngleDraw:
    """Drawn angles and the angle-draw record stored on the Runs."""

    angles: list
    second_angles: list  # per Run: the double-grid angle or None
    base_angle_mode: str
    base_angle: float
    finish_jitter: float
    crossing_angle: float
    min_separation: float
    history_angles_avoided: list
    relaxations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    provisional: bool = False

    def relaxations_of(self, kind: str) -> list:
        return [r for r in self.relaxations if r.kind == kind]

    def to_dict(self) -> dict:
        return {
            "angles": list(self.angles),
            "second_angles": list(self.second_angles),
            "base_angle_mode": self.base_angle_mode,
            "base_angle": self.base_angle,
            "finish_jitter": self.finish_jitter,
            "crossing_angle": self.crossing_angle,
            "min_separation": self.min_separation,
            "history_angles_avoided": list(self.history_angles_avoided),
            "relaxations": [asdict(r) for r in self.relaxations],
            "warnings": list(self.warnings),
            "provisional": self.provisional,
        }


def _grid(lo: float, hi: float) -> list:
    n = int(math.floor((hi - lo) / GRID_STEP + _EPS))
    pts = [lo + i * GRID_STEP for i in range(n + 1)]
    if hi - pts[-1] > _EPS:
        pts.append(hi)
    seen: list = []
    for p in pts:
        q = normalise_angle(p)
        if q not in seen:
            seen.append(q)
    return seen


def _min_dist(a: float, others: Sequence[float]) -> float:
    return min((angular_distance(a, o) for o in others), default=math.inf)


def _fmt(x: float) -> str:
    return f"{x:g}°"


def draw_angles(
    angle_range: Union[dict, str],
    run_count: int,
    *,
    base_angle_mode: str = "fixed",
    base_angle: float = 0.0,
    finish_jitter: float = DEFAULT_FINISH_JITTER,
    last_finals: Sequence[float] = (),
    crossing_angle: float = DEFAULT_CROSSING,
    min_separation: float = DEFAULT_SEPARATION,
    history_angles: Sequence[float] = (),
    double_grid: Optional[Sequence[Optional[float]]] = None,
    rng=None,
    draws: int = DEFAULT_DRAWS,
) -> AngleDraw:
    """Draw `run_count` relative angles for one Plan.

    `angle_range` is `{min, max}`, "full" or "unset" (drawn as full and
    marked provisional). `history_angles` are angles of done Runs on the Zone
    within the history window; `last_finals` the last 3 final angles on the
    Zone. `double_grid` gives, per Run, the second (double-grid) angle or
    None; it is not drawn, but counts toward every rule.
    """
    if base_angle_mode not in ("fixed", "random"):
        raise ValueError(f"base_angle_mode must be fixed or random: {base_angle_mode!r}")
    if double_grid is None:
        double_grid = [None] * run_count
    if len(double_grid) != run_count:
        raise ValueError("double_grid needs one entry per Run")
    rng = rng if rng is not None else random.Random()

    provisional = angle_range == "unset"
    if angle_range in ("full", "unset"):
        lo, hi = -90.0, 90.0
        full = True
    else:
        lo, hi = float(angle_range["min"]), float(angle_range["max"])
        full = False
    range_grid = _grid(-89.5, 90.0) if full else _grid(lo, hi)

    def in_range(a: float) -> bool:
        return full or lo - _EPS <= a <= hi + _EPS

    seconds = [None if s is None else normalise_angle(s) for s in double_grid]
    hist = [normalise_angle(h) for h in history_angles]
    finals = [normalise_angle(f) for f in last_finals]
    base = normalise_angle(base_angle)

    warnings: list = []
    relaxations: list = []
    angles: list = [None] * run_count

    def relax(i: int, kind: str, req, app, message: str) -> None:
        relaxations.append(Relaxation(i, kind, req, app, message))
        warnings.append(f"Run {i + 1}: {message}")

    for i, s in enumerate(seconds):
        if s is not None and not in_range(s):
            warnings.append(
                f"Run {i + 1}: double-grid angle {_fmt(s)} is outside the allowed range"
            )

    def placed(i: int) -> list:
        out = []
        if angles[i] is not None:
            out.append(angles[i])
        if seconds[i] is not None:
            out.append(seconds[i])
        return out

    # The final Run is anchored first when fixed, so earlier Runs adapt to it.
    fixed_final = base_angle_mode == "fixed" and run_count > 0
    if fixed_final:
        i = run_count - 1
        if not in_range(base):
            warnings.append(f"Base angle {_fmt(base)} is outside the allowed range")
        cands = _grid(base - finish_jitter, base + finish_jitter)
        ok = [c for c in cands if _min_dist(c, finals) >= FINISH_MIN_DIFF - _EPS]
        if ok:
            angles[i] = rng.choice(ok)
        else:
            best = max(_min_dist(c, finals) for c in cands)
            angles[i] = rng.choice(
                [c for c in cands if _min_dist(c, finals) >= best - _EPS]
            )
            relax(
                i, "finish_difference", FINISH_MIN_DIFF, best,
                f"finish angle differs only {_fmt(best)} from recent finals",
            )

    order = list(range(run_count - 1 if fixed_final else run_count))
    for i in order:
        others = [a for j in range(run_count) if j != i for a in placed(j)]
        adj = [a for j in (i - 1, i + 1) if 0 <= j < run_count for a in placed(j)]
        sep, cross = min_separation, crossing_angle

        def sep_of(c): return _min_dist(c, others)
        def cross_of(c): return _min_dist(c, adj)
        def hist_of(c): return _min_dist(c, hist)

        pts = [
            c for c in range_grid
            if sep_of(c) >= sep - _EPS and cross_of(c) >= cross - _EPS
            and hist_of(c) >= min_separation - _EPS
        ]
        if not pts:
            pts = [
                c for c in range_grid
                if sep_of(c) >= sep - _EPS and cross_of(c) >= cross - _EPS
            ]
            if pts and hist:
                relax(i, "history", min_separation, None, "history avoidance relaxed")
        if not pts:
            ok_cross = [c for c in range_grid if cross_of(c) >= cross - _EPS]
            if ok_cross:
                sep = max(sep_of(c) for c in ok_cross)
                pts = [c for c in ok_cross if sep_of(c) >= sep - _EPS]
            else:
                cross = max(cross_of(c) for c in range_grid)
                ok_cross = [c for c in range_grid if cross_of(c) >= cross - _EPS]
                sep = min(min_separation, max(sep_of(c) for c in ok_cross))
                pts = [c for c in ok_cross if sep_of(c) >= sep - _EPS]
            if hist:
                relax(i, "history", min_separation, None, "history avoidance relaxed")
            if sep < min_separation - _EPS:
                relax(i, "separation", min_separation, sep,
                      f"separation relaxed to {_fmt(sep)}")
            if cross < crossing_angle - _EPS:
                relax(i, "crossing", crossing_angle, cross,
                      f"crossing relaxed to {_fmt(cross)}")

        sample = [rng.choice(pts) for _ in range(max(1, draws))]
        # Farthest from recent history wins; ties go to the earliest draw.
        angles[i] = max(sample, key=hist_of)

    return AngleDraw(
        angles=angles,
        second_angles=seconds,
        base_angle_mode=base_angle_mode,
        base_angle=base,
        finish_jitter=finish_jitter,
        crossing_angle=crossing_angle,
        min_separation=min_separation,
        history_angles_avoided=list(history_angles),
        relaxations=relaxations,
        warnings=warnings,
        provisional=provisional,
    )
