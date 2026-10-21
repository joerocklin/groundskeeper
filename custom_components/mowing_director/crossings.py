"""Predicting recharge crossings from learned battery levels (spec §10).

Pure and HA-free. The return level is the battery at `mowing` -> `returning`
that leads to `recharging`; the resume level is the battery when the Mower
leaves the dock to continue. Each is the median of the last few observations
(`MIN_SAMPLES` or more), else the Mower's configured level, else 25 % / 95 %.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from statistics import median

MIN_SAMPLES = 3
DEFAULT_LEVELS = {"return": 25, "resume": 95}
SLOPE_WINDOW = timedelta(minutes=10)
MIN_SLOPE_SPAN = timedelta(minutes=2)
FALLBACK_MARGIN = 5  # battery points, when there is no usable slope
RUN_END_PROGRESS_PCT = 95  # unproven; tune in the live trial

History = Sequence[tuple[datetime, int]]


def effective_level(
    kind: str, samples: Sequence[int], configured: int | None
) -> tuple[int, str]:
    """(level, source) with source `learned`, `configured` or `default`."""
    if len(samples) >= MIN_SAMPLES:
        value = median(samples)
        return (int(value) if value == int(value) else value), "learned"
    if configured is not None:
        return configured, "configured"
    return DEFAULT_LEVELS[kind], "default"


def battery_slope(history: History, now: datetime) -> float | None:
    """Battery points per minute over the last 10 min, None when unusable."""
    recent = [(t, b) for t, b in history if now - t <= SLOPE_WINDOW]
    if len(recent) < 2:
        return None
    (t0, b0), (t1, b1) = recent[0], recent[-1]
    if t1 - t0 < MIN_SLOPE_SPAN:
        return None
    return (b1 - b0) / ((t1 - t0).total_seconds() / 60)


def should_warn(
    kind: str,
    battery: int,
    level: float,
    history: History,
    now: datetime,
    lead: timedelta,
) -> bool:
    """True when the Mower reaches `level` within `lead` (return: falling,
    resume: rising); without a usable slope, within 5 points of it."""
    gap = battery - level if kind == "return" else level - battery
    if gap <= 0:
        return True
    slope = battery_slope(history, now)
    moving = None if slope is None else (-slope if kind == "return" else slope)
    if moving is None or moving <= 0:
        return gap <= FALLBACK_MARGIN
    return gap / moving <= lead.total_seconds() / 60
