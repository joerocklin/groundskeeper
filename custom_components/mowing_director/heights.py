"""Blade height derivation, validation and unit helpers (spec 5.2, 5.3, 5.9).

Pure and HA-free. Heights are SI (mm); the device limits come from the
Mower's limits descriptor (spec 3.6), never hard-coded here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_MAX_CUT = 0.13
MIN_MAX_CUT = 0.05
MAX_MAX_CUT = 0.33  # the one-third rule
_EPS = 1e-9

_MM_PER_INCH = 25.4
_CM_PER_INCH = 2.54
_M_PER_FOOT = 0.3048
SYSTEMS = ("metric", "us_customary")


class HeightError(ValueError):
    """A height or max cut was rejected."""


@dataclass(frozen=True)
class HeightLimits:
    """Height part of the adapter limits descriptor (mm)."""

    min_mm: float
    max_mm: float
    step_mm: float

    def describe(self) -> str:
        return f"{self.min_mm:g}-{self.max_mm:g} mm in steps of {self.step_mm:g} mm"


@dataclass(frozen=True)
class HeightPlan:
    """Derived blade heights, first Run to last, with the rounded inputs."""

    heights: tuple[float, ...]
    start: float
    end: float
    max_cut: float
    rounded: bool  # start or end was moved to the device step


@dataclass(frozen=True)
class HeightInput:
    """A converted user input, rounded to the device step and echoed."""

    mm: float
    display: float
    rounded: bool


def _num(x: float) -> float:
    return int(x) if float(x).is_integer() else x


def round_to_step(mm: float, limits: HeightLimits) -> float:
    """Nearest device step, anchored at the minimum height (half rounds up)."""
    n = math.floor((mm - limits.min_mm) / limits.step_mm + 0.5)
    return _num(round(limits.min_mm + n * limits.step_mm, 6))


def _check_limits(name: str, mm: float, limits: HeightLimits) -> None:
    if not (limits.min_mm - _EPS <= mm <= limits.max_mm + _EPS):
        raise HeightError(
            f"{name} height {mm:g} mm is outside the device limits "
            f"({limits.describe()})"
        )


def _within(heights: list[float], m: float) -> bool:
    return all((a - b) / a <= m + _EPS for a, b in zip(heights, heights[1:]))


def derive_heights(
    start: float,
    end: float,
    limits: HeightLimits,
    max_cut: float = DEFAULT_MAX_CUT,
) -> HeightPlan:
    """Fewest-Runs geometric heights from start to end, every step <= max cut."""
    if not (MIN_MAX_CUT - _EPS <= max_cut <= MAX_MAX_CUT + _EPS):
        raise HeightError(
            f"max cut {max_cut:.0%} is outside the allowed range "
            f"{MIN_MAX_CUT:.0%}-{MAX_MAX_CUT:.0%}"
        )
    _check_limits("start", start, limits)
    _check_limits("end", end, limits)
    s, e = round_to_step(start, limits), round_to_step(end, limits)
    rounded = (s, e) != (start, end)
    if s < e:
        raise HeightError(f"start height {s:g} mm is below end height {e:g} mm")
    if s == e:
        return HeightPlan((s,), s, e, max_cut, rounded)

    n = max(1, math.ceil(math.log(s / e) / -math.log(1 - max_cut) - _EPS))
    max_steps = math.ceil((s - e) / limits.step_mm)  # every grid height used
    while n <= max_steps:
        heights: list[float] = []
        for i in range(n + 1):
            h = s if i == 0 else e if i == n else round_to_step(
                s * (e / s) ** (i / n), limits
            )
            if not heights or h != heights[-1]:
                heights.append(h)
        if _within(heights, max_cut):
            return HeightPlan(tuple(heights), s, e, max_cut, rounded)
        n += 1
    raise HeightError(
        f"cannot get from {s:g} to {e:g} mm with every step at or below the "
        f"max cut of {max_cut:.0%} at the device step of {limits.step_mm:g} mm"
    )


def to_display(kind: str, si: float, system: str) -> float:
    """SI to display. kind: height (mm), spacing (cm), speed (m/s)."""
    _check_system(system)
    if system == "metric":
        return si
    if kind == "height":
        return round(si / _MM_PER_INCH, 2)
    if kind == "spacing":
        return round(si / _CM_PER_INCH, 2)
    if kind == "speed":
        return round(si / _M_PER_FOOT, 2)
    raise ValueError(f"unknown kind {kind!r}")


def from_display(kind: str, value: float, system: str) -> float:
    """Display to SI (unrounded)."""
    _check_system(system)
    if system == "metric":
        return value
    if kind == "height":
        return value * _MM_PER_INCH
    if kind == "spacing":
        return value * _CM_PER_INCH
    if kind == "speed":
        return value * _M_PER_FOOT
    raise ValueError(f"unknown kind {kind!r}")


def height_input(value: float, system: str, limits: HeightLimits) -> HeightInput:
    """Convert a displayed height to mm, check limits, round to step, echo."""
    mm = from_display("height", value, system)
    _check_limits("input", mm, limits)
    stepped = round_to_step(mm, limits)
    return HeightInput(
        mm=stepped,
        display=to_display("height", stepped, system),
        rounded=abs(stepped - mm) > 1e-6,
    )


def _check_system(system: str) -> None:
    if system not in SYSTEMS:
        raise ValueError(f"unknown unit system {system!r}")
