"""Schedule operations shared by the services and the panel's websocket API.

One path for "create a Plan", "edit / delete / move a Run", "remove a Plan" and
"re-derive a Plan", so the service (`end` / `front`) and the panel (`merge` by
step index) cannot drift apart. Failures raise `ScheduleError`, carrying a
translation key; callers map it to a `ServiceValidationError` or a websocket
error. Nothing here commands the Mower.
"""

from __future__ import annotations

import random
from typing import Any

from homeassistant.util import dt as dt_util

from .angle_draw import normalise_angle
from .core import (
    ScheduleStatus,
    move_blocker,
    remove_plan_blocker,
)
from .heights import HeightError, round_to_step
from .plans import (
    AmbiguousZone,
    UnknownZone,
    build_plan,
    find_zone,
    height_limits,
    redraw_plan,
    rederive_plan,
)
from .zone_angles import run_estimate_s
from .run_settings import SettingsError, validate_overrides
from .runtime import Director

POSITIONS = ("end", "front", "merge")


class ScheduleError(Exception):
    """A refused Schedule operation: a translation key and its placeholders."""

    def __init__(self, key: str, **placeholders: str) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


def _config(director: Director) -> dict[str, Any]:
    return director.data.get("config", {})


def _history_runs(director: Director) -> list[dict[str, Any]]:
    return director.data.get("history", {}).get("runs", [])


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["id"],
        "height_mm": run["height_mm"],
        "angle_deg": run["angle_deg"],
        "provisional": bool(run.get("angle_draw", {}).get("provisional")),
        "n": run["index"],
        "m": run["of"],
    }


def _derive(
    director: Director,
    *,
    zone: str,
    start_height_mm: float,
    end_height_mm: float,
    max_cut_pct: float | None = None,
    base_angle: float | None = None,
    finishing: bool | None = None,
    settings: dict[str, Any] | None = None,
    origin: str,
    rng: random.Random | None = None,
):
    """Derive a Plan without placing it; the Zone and the BuiltPlan."""
    config = _config(director)
    try:
        found = find_zone(config.get("zones", []), zone)
    except UnknownZone:
        raise ScheduleError("unknown_zone", zone=zone) from None
    except AmbiguousZone:
        raise ScheduleError("ambiguous_zone", zone=zone) from None
    try:
        built = build_plan(
            zone=found,
            config=config,
            limits=director.adapter.limits,
            start=start_height_mm,
            end=end_height_mm,
            origin=origin,
            now=dt_util.utcnow(),
            max_cut_pct=max_cut_pct,
            base_angle=base_angle,
            finishing=finishing,
            plan_settings=settings,
            history_runs=_history_runs(director),
            rng=rng,
        )
    except HeightError as err:
        raise ScheduleError("invalid_height", error=str(err)) from None
    except SettingsError as err:
        raise ScheduleError("invalid_settings", error=str(err)) from None
    return found, built


def preview_plan(director: Director, **kwargs: Any) -> dict[str, Any]:
    """The Runs a new Plan would get (height, angle, estimated time) and the
    derivation warnings, without placing anything (spec §16.3, §5.5)."""
    found, built = _derive(director, origin="panel", **kwargs)
    runs = []
    for run in built.runs:
        view = _run_view(run)
        view["estimated_time_s"] = run_estimate_s(found, run["angle_deg"])
        runs.append(view)
    return {
        "runs": runs,
        "start_height_mm": built.heights.start,
        "end_height_mm": built.heights.end,
        "max_cut_pct": round(built.heights.max_cut * 100, 6),
        "rounded": built.heights.rounded,
        "warnings": built.warnings,
        "provisional": any(r["provisional"] for r in runs),
    }


async def create_plan(
    director: Director,
    *,
    zone: str,
    start_height_mm: float,
    end_height_mm: float,
    max_cut_pct: float | None = None,
    base_angle: float | None = None,
    finishing: bool | None = None,
    settings: dict[str, Any] | None = None,
    position: str = "end",
    start: bool = False,
    origin: str = "service",
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Derive a Plan, place it on the Schedule and report it (spec §13.3)."""
    if position not in POSITIONS:
        raise ScheduleError("invalid_position", position=position)
    found, built = _derive(
        director, zone=zone, start_height_mm=start_height_mm,
        end_height_mm=end_height_mm, max_cut_pct=max_cut_pct, base_angle=base_angle,
        finishing=finishing, settings=settings, origin=origin, rng=rng,
    )
    before = director.state["schedule"]["status"]
    await director.async_add_plan(built.plan, built.runs, start, position)
    after = director.state["schedule"]["status"]
    started = before == ScheduleStatus.IDLE and after != ScheduleStatus.IDLE
    response: dict[str, Any] = {
        "plan_id": built.plan["id"],
        "runs": [_run_view(r) for r in built.runs],
        "start_height_mm": built.heights.start,
        "end_height_mm": built.heights.end,
        "max_cut_pct": round(built.heights.max_cut * 100, 6),
        "rounded": built.heights.rounded,
        "warnings": built.warnings,
        "schedule_status": after,
        "started": started,
    }
    if start and not started:
        response["reason"] = "paused" if before == ScheduleStatus.PAUSED else "already_active"
    return response


# -- editing Runs ------------------------------------------------------------------


def _find_run(director: Director, run_id: str) -> dict[str, Any]:
    run = next((r for r in director.state["runs"] if r["id"] == run_id), None)
    if run is None:
        raise ScheduleError("unknown_run", run_id=run_id)
    if run["status"] != "pending":
        raise ScheduleError("run_not_pending", run_id=run_id)
    return run


def _plan_runs(director: Director, plan_id: str) -> list[dict[str, Any]]:
    runs = [r for r in director.state["runs"] if r["plan_id"] == plan_id]
    runs += [r for r in _history_runs(director) if r["plan_id"] == plan_id]
    return sorted(runs, key=lambda r: r["index"])


def _edit_warnings(
    director: Director, run: dict[str, Any], height: float, angle: float | None
) -> list[str]:
    """Edits are allowed within device limits; these are only warned about."""
    warnings: list[str] = []
    plan = director.state["plans"].get(run["plan_id"], {})
    max_cut = plan.get("max_cut", 0.13)
    siblings = {r["index"]: r for r in _plan_runs(director, run["plan_id"])}
    before, after = siblings.get(run["index"] - 1), siblings.get(run["index"] + 1)
    heights = [
        (before["height_mm"] if before else None, height),
        (height, after["height_mm"] if after else None),
    ]
    if any(a is not None and b is not None and b > a for a, b in heights):
        warnings.append("height_increases")
    if any(a is not None and b is not None and b < a and (a - b) / a > max_cut + 1e-9
           for a, b in heights):
        warnings.append("step_above_max_cut")
    if angle is not None:
        zone = next((z for z in _config(director).get("zones", []) if z["id"] == run["zone_id"]), {})
        rng = (zone.get("angle_range") or {}).get("angle_range", "unset")
        if isinstance(rng, dict) and not rng["min"] <= angle <= rng["max"]:
            warnings.append("angle_outside_range")
    return warnings


async def edit_run(director: Director, run_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Edit a pending Run within the device limits; warn, never reject, on the rest."""
    run = _find_run(director, run_id)
    clean: dict[str, Any] = {}
    limits = director.adapter.limits
    if "height_mm" in changes:
        lim = height_limits(limits)
        h = changes["height_mm"]
        if not lim.min_mm <= h <= lim.max_mm:
            raise ScheduleError(
                "invalid_height",
                error=f"height {h:g} mm is outside the device limits ({lim.describe()})",
            )
        clean["height_mm"] = round_to_step(h, lim)
    if "angle_deg" in changes:
        clean["angle_deg"] = normalise_angle(changes["angle_deg"])
    if changes.get("settings"):
        try:
            validate_overrides(limits, changes["settings"])
        except SettingsError as err:
            raise ScheduleError("invalid_settings", error=str(err)) from None
        clean["settings"] = dict(changes["settings"])
    height = clean.get("height_mm", run["height_mm"])
    angle = clean.get("angle_deg")
    warnings = _edit_warnings(director, run, height, angle)
    await director.async_edit_run(run_id, clean)
    return {"run_id": run_id, "warnings": warnings, **{k: v for k, v in clean.items() if k != "settings"}}


async def delete_run(director: Director, run_id: str) -> None:
    _find_run(director, run_id)
    await director.async_delete_run(run_id)


async def move_run(director: Director, run_id: str, position: int) -> None:
    reason = move_blocker(director.state, run_id, position)
    if reason is not None:
        raise ScheduleError(f"move_{reason}", run_id=run_id)
    await director.async_move_run(run_id, position)


async def remove_plan(director: Director, plan_id: str) -> None:
    """Cancel the Plan's pending Runs; refused while one of its Runs is active."""
    reason = remove_plan_blocker(director.state, plan_id)
    if reason is not None:
        raise ScheduleError(f"remove_plan_{reason}", plan_id=plan_id)
    await director.async_remove_plan(plan_id)


async def rederive(
    director: Director, plan_id: str, rng: random.Random | None = None
) -> dict[str, Any]:
    """Rebuild a not-started Plan from its stored inputs and current defaults."""
    plan = director.state["plans"].get(plan_id)
    if plan is None:
        raise ScheduleError("remove_plan_unknown_plan", plan_id=plan_id)
    started = any(r["plan_id"] == plan_id for r in _history_runs(director))
    if started or any(
        r["status"] != "pending" for r in director.state["runs"] if r["plan_id"] == plan_id
    ):
        raise ScheduleError("plan_started", plan_id=plan_id)
    config = _config(director)
    zone = next((z for z in config.get("zones", []) if z["id"] == plan["zone_id"]), None)
    if zone is None:
        raise ScheduleError("unknown_zone", zone=plan["zone_id"])
    try:
        built = rederive_plan(
            plan, zone=zone, config=config, limits=director.adapter.limits,
            now=dt_util.utcnow(), history_runs=_history_runs(director), rng=rng,
        )
    except (HeightError, SettingsError) as err:
        raise ScheduleError("invalid_settings", error=str(err)) from None
    await director.async_replace_plan(built.plan, built.runs)
    return {"plan_id": plan_id, "runs": [_run_view(r) for r in built.runs], "warnings": built.warnings}


def _zone_of(director: Director, zone_id: str) -> dict[str, Any] | None:
    return next((z for z in _config(director).get("zones", []) if z["id"] == zone_id), None)


def _redraw(
    director: Director, plan_id: str, *, provisional_only: bool, rng: random.Random | None
):
    plan = director.state["plans"].get(plan_id)
    zone = _zone_of(director, plan["zone_id"]) if plan else None
    if plan is None or zone is None:
        return None
    return redraw_plan(
        plan, _plan_runs(director, plan_id), zone=zone, config=_config(director),
        limits=director.adapter.limits, now=dt_util.utcnow(),
        history_runs=_history_runs(director), rng=rng, provisional_only=provisional_only,
    )


async def reroll_angles(
    director: Director, plan_id: str, rng: random.Random | None = None
) -> dict[str, Any]:
    """"Re-roll angles": redraw every angle of a not-started Plan (spec §5.5)."""
    plan = director.state["plans"].get(plan_id)
    if plan is None:
        raise ScheduleError("remove_plan_unknown_plan", plan_id=plan_id)
    started = any(r["plan_id"] == plan_id for r in _history_runs(director))
    if started or any(
        r["status"] != "pending" for r in director.state["runs"] if r["plan_id"] == plan_id
    ):
        raise ScheduleError("plan_started", plan_id=plan_id)
    if _zone_of(director, plan["zone_id"]) is None:
        raise ScheduleError("unknown_zone", zone=plan["zone_id"])
    _, runs, warnings = _redraw(director, plan_id, provisional_only=False, rng=rng)
    await director.async_redraw_angles(plan_id, runs)
    return {"plan_id": plan_id, "runs": [_run_view(r) for r in runs], "warnings": warnings}


async def redraw_provisional(
    director: Director, zone_id: str, rng: random.Random | None = None
) -> list[str]:
    """The Zone's range arrived: redraw every pending, unedited, provisional Run
    on it and note the Plan (spec §5.5). Returns the ids of the Plans changed."""
    changed: list[str] = []
    for plan_id in [p for p, v in director.state["plans"].items() if v["zone_id"] == zone_id]:
        result = _redraw(director, plan_id, provisional_only=True, rng=rng)
        if result is None:
            continue
        plan, runs, warnings = result
        live = {r["id"] for r in director.state["runs"]}
        redrawn = [
            r for r in runs
            if r["id"] in live and not r.get("angle_edited")
            and not r["angle_draw"]["provisional"]
        ]
        await director.async_redraw_angles(plan_id, redrawn, plan.get("note"), warnings)
        changed.append(plan_id)
    return changed
