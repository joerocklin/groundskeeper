"""The Now tab's view of one Mower (spec §16.2) and its control actions.

`now_view` is a pure read of the Director's state; `CONTROLS` maps the panel's
run-control actions to the same `controls` functions the services and buttons
call, so the panel can never disagree with them about semantics.
"""

from __future__ import annotations

from typing import Any

from . import controls
from .core import (
    ACTIVE_RUN,
    RunStatus,
    ScheduleStatus,
    active_run,
    move_blocker,
    pending_runs,
)
from .runtime import Director

CONTROLS = {
    "pause": controls.async_pause,  # Pause after Run
    "resume": controls.async_resume,
    "stop_now": controls.async_stop_now,
    "retry": controls.async_retry_run,
    "skip_plan": controls.async_skip_plan,
    "abort": controls.async_cancel,
    "resume_run": controls.async_resume_run,
    "end_run": controls.async_end_run,
    "done_today": controls.async_wet_grass_done_today,
}


def _run_row(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["id"],
        "plan_id": run["plan_id"],
        "zone_id": run["zone_id"],
        "zone_name": run["zone_name"],
        "height_mm": run["height_mm"],
        "angle_deg": run["angle_deg"],
        "n": run["index"],
        "of": run["of"],
    }


def _available_controls(director: Director, status: str, failed: bool,
                        active: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    if status in (ScheduleStatus.WAITING, ScheduleStatus.RUNNING):
        out.append("pause")
    if active is not None:
        out.append("stop_now")
    if status == ScheduleStatus.PAUSED:
        if failed:
            out += ["retry", "skip_plan", "abort"]
        elif any(r["status"] == RunStatus.PENDING for r in director.state["runs"]):
            out.append("resume")
    if active is not None and active["status"] == RunStatus.ON_HOLD:
        out += ["resume_run", "end_run"]
    if any(c["type"] == "wet_grass" and c["enabled"] for c in director.checks):
        out.append("done_today")
    return out


def now_view(director: Director) -> dict[str, Any]:
    """Everything the Now tab shows, apart from the map geometry (`get_zones`)."""
    state = director.state
    status = state["schedule"]["status"]
    snap = state.get("snapshot") or {}
    battery = snap.get("battery")
    zones = director.data.get("config", {}).get("zones", [])
    names = {c["id"]: c["name"] for c in director.checks}
    waiting = [
        {"check": w["check"], "name": names.get(w["check"], w["check"]), "reason": w["reason"]}
        for w in state["waiting_on"]
    ] if status == ScheduleStatus.WAITING else []
    failing = {w["check"] for w in waiting}

    active = active_run(state)
    failed_run = next((r for r in state["runs"] if r["status"] == RunStatus.FAILED), None)
    current = None
    if active is not None:
        current = {**_run_row(active), "status": active["status"], "battery": battery,
                   "height_check": active.get("height_check"),
                   "on_hold_reason": active.get("on_hold_reason")}

    current_zone_id = active["zone_id"] if active is not None else None
    if current_zone_id is None and snap.get("zone") is not None:
        current_zone_id = next(
            (z["id"] for z in zones if z.get("native_ref") == snap["zone"]), None)

    pending = pending_runs(state)
    up_next = []
    for i, run in enumerate(pending):
        up_next.append({
            **_run_row(run),
            "can_up": i > 0 and move_blocker(state, run["id"], i - 1) is None,
            "can_down": i < len(pending) - 1 and move_blocker(state, run["id"], i + 1) is None,
        })

    return {
        "status": status,
        "pause_requested": bool(state["schedule"].get("pause_requested")),
        "paused_reason": state["schedule"]["reason"] if status == ScheduleStatus.PAUSED else None,
        "battery": battery,
        "current_run": current,
        "current_zone_id": current_zone_id,
        "next_run": up_next[0] if up_next else None,
        "up_next": up_next,
        "waiting_on": waiting,
        "failure": ({**_run_row(failed_run), "reason": failed_run.get("reason")}
                    if failed_run is not None else None),
        "checks": [
            {"id": c["id"], "name": c["name"], "type": c["type"],
             "state": ("failing" if c["id"] in failing
                       else "ok" if status == ScheduleStatus.WAITING else "idle")}
            for c in director.checks if c["enabled"]
        ],
        "controls": _available_controls(director, status, failed_run is not None, active),
    }
