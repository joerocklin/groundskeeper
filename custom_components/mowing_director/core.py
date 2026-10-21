"""Pure reducer core for the Run and Schedule state machines (spec §2.2, §6, §7).

`reduce(core_state, input, env) -> Reduction(state, effects)` is a pure
function (ADR 0004): no I/O, no clock, no HA. Every input carries its own
`now`; every wait is a persisted absolute deadline; every side effect is a
returned value for the effect runner (`runtime.py`) to perform AFTER the new
state is saved.

`core_state` is the JSON dict of the three Store sections the reducer owns:

    state    schedule, plans, runs, active_run_id, deadlines, intent,
             command_log, snapshot, last_error_time, blocking_error,
             waiting_on
    learned  per-Zone duration samples
    history  finished Runs and the inputs of Plans that left the Schedule

Keeping `history` in the same value as `state` is what makes "the finished
Run moves from `state` to `history` in the same atomic save" true by
construction (spec §14.3).

This slice is the happy path (ticket #13): failures beyond "refused" and
"did not start", on hold, rain stop, controls and the remaining Checks are
later tickets.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo
from enum import StrEnum
from typing import Any

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.util import ulid as ulid_util

from .adapter import (
    AdapterSignal,
    CommandKind,
    JobStarted,
    CommandResult as AdapterCommandResult,
    Movement,
    NewError,
    Position,
    RainStop,
    RunSettings,
    SelfCheckReason,
    Snapshot,
    ZoneTaskAborted,
    ZoneTaskComplete,
)

STABILITY_WINDOW = timedelta(seconds=60)  # spec §7.7, §19
DEFAULT_START_WINDOW = timedelta(minutes=5)  # spec §7.1, §19
COMMAND_LOG_MAX = 20  # spec §14.6
DURATION_SAMPLES_MAX = 20  # spec §14.6

DEADLINE_STABILITY = "stability"
DEADLINE_START_WINDOW = "start_window"
DEADLINE_DONE_STABLE = "done_stable"
DEADLINE_STOPPED_STABLE = "stopped_stable"
DEADLINE_STALL = "stall"
DEADLINE_HEIGHT_VERIFY = "height_verify"  # spec §7.6: window per `mowing` segment
DEADLINE_HEIGHT_STABLE = "height_stable"  # spec §7.6: 60 s in tolerance
DEFAULT_HEIGHT_VERIFY_WINDOW = timedelta(minutes=10)  # spec §7.6, §19
DEFAULT_STALL_WINDOW = timedelta(minutes=60)  # spec §7.3, §19
DEADLINE_PAUSE_STABLE = "pause_stable"
DEADLINE_HOLD_CLEAR_STABLE = "hold_clear_stable"
DEADLINE_RECHARGE_STALL = "recharge_stall"
DEADLINE_CONTACT_LOST = "contact_lost"
RECHARGE_STALL_WINDOW = timedelta(minutes=60)  # spec §7.4, §19
CONTACT_LOST_GRACE = timedelta(minutes=10)  # spec §7.4, §19
# Stability windows are evidence about the live Mower: they restart on load.
RESTART_WINDOWS = (
    DEADLINE_STABILITY,
    DEADLINE_DONE_STABLE,
    DEADLINE_STOPPED_STABLE,
    DEADLINE_PAUSE_STABLE,
    DEADLINE_HOLD_CLEAR_STABLE,
    DEADLINE_HEIGHT_STABLE,
    DEADLINE_CONTACT_LOST,
)
PROMPT_REMINDER_AFTER = timedelta(hours=1)  # spec §12.4: first reminder at +1 h
DEADLINE_PROMPT_REMINDER = "prompt_reminder"  # earliest due Prompt reminder
DEFAULT_WORK_START = time(8, 0)  # spec §19: Work hours 08:00-00:00 daily
DEFAULT_WORK_END = time(0, 0)  # 00:00 means midnight, the end of the day

PROMPT_RUN_FAILED = "run_failed"  # spec §12.1: Retry / Skip Plan / Abort
ACTION_RETRY = "retry"
ACTION_SKIP_PLAN = "skip_plan"
ACTION_ABORT = "abort"

PROMPT_RUN_ON_HOLD = "run_on_hold"  # spec §12.1: Resume / End Run
ACTION_RESUME = "resume"
ACTION_END_RUN = "end_run"

HOLD_EXTERNAL_PAUSE = "external_pause"  # spec §7.4 causes
HOLD_RECHARGE_STALL = "recharge_stall"
HOLD_CONTACT_LOST = "contact_lost"
HOLD_DEVICE_WORK_HOURS = "device_work_hours"


class ScheduleStatus(StrEnum):
    """Schedule statuses (spec §6.1)."""

    IDLE = "idle"
    WAITING = "waiting"
    RUNNING = "running"
    PAUSED = "paused"


class RunStatus(StrEnum):
    """Run statuses (spec §6.2). `on_hold` is `on hold` in prose."""

    PENDING = "pending"
    STARTING = "starting"
    MOWING = "mowing"
    ON_HOLD = "on_hold"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


ACTIVE_RUN = (RunStatus.STARTING, RunStatus.MOWING, RunStatus.ON_HOLD)
TERMINAL_RUN = (RunStatus.DONE, RunStatus.SKIPPED, RunStatus.CANCELLED)


class PlanStatus(StrEnum):
    """Plan status, derived from Runs and never stored (spec §6.4)."""

    ACTIVE = "active"
    COMPLETE = "complete"
    ENDED_EARLY = "ended_early"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AddPlan:
    """Place a built Plan and its Runs on the Schedule; optionally start.

    `position`: `end` (default), `front` (before the first pending Run) or
    `merge` (by step index, panel add path; spec §5.8).
    """

    now: datetime
    plan: dict[str, Any]
    runs: list[dict[str, Any]]
    start: bool = False
    position: str = "end"


@dataclass(frozen=True)
class EditRun:
    """Edit a pending Run: `height_mm`, `angle_deg`, `settings` (patch)."""

    now: datetime
    run_id: str
    changes: dict[str, Any]


@dataclass(frozen=True)
class DeleteRun:
    """Delete a pending Run."""

    now: datetime
    run_id: str


@dataclass(frozen=True)
class MoveRun:
    """Move a pending Run to `position` among the pending Runs (0-based)."""

    now: datetime
    run_id: str
    position: int


@dataclass(frozen=True)
class RemovePlan:
    """Cancel a Plan's pending Runs (never commands the Mower)."""

    now: datetime
    plan_id: str


@dataclass(frozen=True)
class ReplacePlan:
    """Swap a not-started Plan's Runs for re-derived ones (spec §5.1)."""

    now: datetime
    plan: dict[str, Any]
    runs: list[dict[str, Any]]


@dataclass(frozen=True)
class RedrawAngles:
    """Replace the angles (and angle-draw records) of pending Runs: Re-roll, or
    a provisional redraw when the Zone's range arrives (spec §5.5)."""

    now: datetime
    plan_id: str
    runs: list[dict[str, Any]]  # each: id, angle_deg, angle_draw
    note: str | None = None
    warnings: list[str] | None = None


@dataclass(frozen=True)
class StartSchedule:
    """`start` service / button: `idle`/`paused` to `waiting`."""

    now: datetime


@dataclass(frozen=True)
class PauseSchedule:
    """`pause` service / button: soft Pause (spec §6.4); never commands the Mower."""

    now: datetime


@dataclass(frozen=True)
class StopNow:
    """`stop_now` service / button: the active Run returns to `pending`."""

    now: datetime


@dataclass(frozen=True)
class ResumeSchedule:
    """`resume` service / button: `paused` to `waiting`."""

    now: datetime



@dataclass(frozen=True)
class CancelSchedule:
    """`cancel` service / button: Abort."""

    now: datetime


@dataclass(frozen=True)
class RetryRun:
    """`retry_run` service: answer a failure Prompt with Retry."""

    now: datetime


@dataclass(frozen=True)
class SkipPlan:
    """`skip_plan` service: answer a failure Prompt with Skip Plan."""

    now: datetime


@dataclass(frozen=True)
class ResumeRun:
    """`resume_run` service: answer an on-hold Run with Resume (spec §13.2)."""

    now: datetime


@dataclass(frozen=True)
class EndRun:
    """`end_run` service: answer an on-hold Run with End Run (spec §13.2)."""

    now: datetime


@dataclass(frozen=True)
class AnswerPrompt:
    """Answer a pending Prompt by id from any path (spec §12.5, §12.12)."""

    now: datetime
    prompt_id: str
    action: str


@dataclass(frozen=True)
class RaisePrompt:
    """Raise a Prompt of any kind through the shared model (spec §12.6).

    Later tickets raise their own kinds from inside the reducer; this input
    is the same path for callers outside it.
    """

    now: datetime
    kind: str
    subject: str | None
    actions: list[str]
    context: dict[str, Any]


@dataclass(frozen=True)
class SnapshotReceived:
    """An adapter snapshot."""

    now: datetime
    snapshot: Snapshot


@dataclass(frozen=True)
class SignalReceived:
    """An adapter signal (acts immediately, no stability window)."""

    now: datetime
    signal: AdapterSignal


@dataclass(frozen=True)
class CommandOutcome:
    """The result of a Mower command the runner performed."""

    now: datetime
    run_id: str
    command: str
    result: AdapterCommandResult


@dataclass(frozen=True)
class TimerFired:
    """A deadline timer fired; the reducer re-evaluates every deadline."""

    now: datetime


@dataclass(frozen=True)
class Reconcile:
    """Restart reconciliation (spec §8): the one load path for every Mower.

    `snapshot` is the first usable live snapshot, or None when none arrived
    within the wait-for-Mower limit. `signals` are what the adapter pushed
    while the reducer was frozen. `loaded_at` anchors the contact-lost grace:
    HA downtime is not evidence of lost contact.
    """

    now: datetime
    loaded_at: datetime
    snapshot: Snapshot | None
    signals: tuple[AdapterSignal, ...] = ()


type CoreInput = (
    Reconcile
    | AddPlan
    | EditRun
    | DeleteRun
    | MoveRun
    | RemovePlan
    | ReplacePlan
    | RedrawAngles
    | StartSchedule
    | PauseSchedule
    | StopNow
    | ResumeSchedule
    | CancelSchedule
    | RetryRun
    | SkipPlan
    | ResumeRun
    | EndRun
    | AnswerPrompt
    | RaisePrompt
    | SnapshotReceived
    | SignalReceived
    | CommandOutcome
    | TimerFired
)


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartRunEffect:
    """Command: start exactly one Zone (performed after the save)."""

    run_id: str
    zone_ref: str
    settings: RunSettings


@dataclass(frozen=True)
class AbortRunEffect:
    """Command: cancel the job and dock."""

    reason: str


@dataclass(frozen=True)
class ResumeRunEffect:
    """Command: ask the Mower to continue its paused job."""

    run_id: str


@dataclass(frozen=True)
class EmitEvent:
    """A `mowing_director_event` (the runner adds the entry and device ids)."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArmTimer:
    """Arm (or replace) the timer for a persisted deadline."""

    name: str
    at: datetime


@dataclass(frozen=True)
class CancelTimer:
    """Cancel a deadline timer."""

    name: str


type Effect = StartRunEffect | AbortRunEffect | ResumeRunEffect | EmitEvent | ArmTimer | CancelTimer


@dataclass(frozen=True)
class Reduction:
    """New core state plus the effects to perform once it is saved."""

    state: dict[str, Any]
    effects: tuple[Effect, ...]


# ---------------------------------------------------------------------------
# Environment (read-only facts the reducer may consult)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ZoneView:
    """What the reducer needs to know about a Director Zone."""

    native_ref: str
    name: str
    missing: bool = False
    bearing: float | None = None  # Zone bearing; None while unset or pending


@dataclass(frozen=True)
class Env:
    """Read-only environment for one reduction."""

    zones: Mapping[str, ZoneView]
    min_start_battery_pct: int
    start_window: timedelta = DEFAULT_START_WINDOW
    stall_window: timedelta = DEFAULT_STALL_WINDOW
    height_verify_window: timedelta = DEFAULT_HEIGHT_VERIFY_WINDOW
    # Limits.height_readback_resolution_mm; None = no readback declared (§7.6.6)
    height_readback_resolution_mm: int | None = None
    entry_id: str = ""  # for Prompt tags (spec §12.6)
    new_id: Callable[[], str] = ulid_util.ulid  # for Prompt ids (spec §12.5)
    # Work hours window for reminders (spec §12.4), in the HA time zone.
    work_start: time = DEFAULT_WORK_START
    work_end: time = DEFAULT_WORK_END
    tz: tzinfo = timezone.utc


# ---------------------------------------------------------------------------
# State shape
# ---------------------------------------------------------------------------


def initial_core_state() -> dict[str, Any]:
    """An empty, safe-idle core state."""
    return normalise_core_state({})


def normalise_core_state(data: Mapping[str, Any]) -> dict[str, Any]:
    """Fill every key the reducer reads, keeping what is already there."""
    state = dict(data.get("state") or {})
    state.setdefault(
        "schedule", {"status": "idle", "reason": None, "pause_requested": False}
    )
    state.setdefault("plans", {})
    state.setdefault("runs", [])
    state.setdefault("active_run_id", None)
    state.setdefault("deadlines", {})
    state.setdefault("intent", None)
    state.setdefault("command_log", [])
    state.setdefault("snapshot", None)
    state.setdefault("last_error_time", None)
    state.setdefault("blocking_error", None)
    state.setdefault("waiting_on", [])
    state.setdefault("stall_movement", None)
    state.setdefault("battery_trend", None)
    state.setdefault("height_verify", None)
    state.setdefault("prompts", [])
    history = dict(data.get("history") or {})
    history.setdefault("runs", [])
    history.setdefault("plans", {})
    learned = dict(data.get("learned") or {})
    learned.setdefault("durations", {})
    return {"state": state, "learned": learned, "history": history}


def iso(moment: datetime) -> str:
    """UTC ISO 8601 (spec §14.5)."""
    return moment.isoformat()


def parse(moment: str) -> datetime:
    """Inverse of `iso`."""
    return datetime.fromisoformat(moment)


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    """The persisted snapshot summary (position kept: it is a stall indicator)."""
    return {
        "activity": snapshot.activity.value if snapshot.activity else None,
        "available": snapshot.available,
        "recharging": snapshot.recharging,
        "job_active": snapshot.job_active,
        "battery": snapshot.battery,
        "progress": snapshot.progress,
        "zone": snapshot.zone,
        "blade_height_mm": snapshot.blade_height_mm,
        "movement": {
            "progress_pct": snapshot.movement.progress_pct,
            "area_m2": snapshot.movement.area_m2,
            "position": (
                [snapshot.movement.position.latitude, snapshot.movement.position.longitude]
                if snapshot.movement.position
                else None
            ),
        },
        "job_ref": snapshot.job_ref,
        "rain": snapshot.rain,
        "self_check": snapshot.self_check.value if snapshot.self_check else None,
    }


def movement_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Every `movement` indicator, position included, for the stall rule."""
    movement = snapshot.movement
    position = movement.position
    return {
        "progress_pct": movement.progress_pct,
        "area_m2": movement.area_m2,
        "position": [position.latitude, position.longitude] if position else None,
    }


def snapshot_from_dict(data: Mapping[str, Any]) -> Snapshot:
    """Rebuild a Snapshot from its persisted summary."""
    movement = data.get("movement") or {}
    return Snapshot(
        activity=LawnMowerActivity(data["activity"]) if data["activity"] else None,
        available=data["available"],
        recharging=data["recharging"],
        job_active=data["job_active"],
        battery=data["battery"],
        progress=data["progress"],
        zone=data["zone"],
        blade_height_mm=data["blade_height_mm"],
        movement=Movement(
            progress_pct=movement.get("progress_pct"),
            area_m2=movement.get("area_m2"),
            position=Position(*movement["position"]) if movement.get("position") else None,
        ),
        job_ref=data["job_ref"],
        rain=data.get("rain"),
        self_check=SelfCheckReason(data["self_check"]) if data.get("self_check") else None,
    )


# ---------------------------------------------------------------------------
# Derived views (never stored)
# ---------------------------------------------------------------------------


def plan_status(core_state: Mapping[str, Any], plan_id: str) -> PlanStatus:
    """active / complete / ended early, derived from the Plan's Runs (§6.4).

    A Run still in `state` (including a `failed` one awaiting a decision)
    keeps its Plan active; otherwise all-done is complete, anything else
    (skipped, cancelled, failed) ended early.
    """
    live = [r for r in core_state["state"]["runs"] if r["plan_id"] == plan_id]
    if live:
        return PlanStatus.ACTIVE
    finished = [r for r in core_state["history"]["runs"] if r["plan_id"] == plan_id]
    if finished and all(r["status"] == RunStatus.DONE for r in finished):
        return PlanStatus.COMPLETE
    return PlanStatus.ENDED_EARLY


def active_run(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """The Run that is `starting`, `mowing` or `on hold`, if any."""
    for run in state["runs"]:
        if run["status"] in ACTIVE_RUN:
            return run
    return None


def next_run(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """The head `pending` Run."""
    for run in state["runs"]:
        if run["status"] == RunStatus.PENDING:
            return run
    return None


def pending_runs(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every `pending` Run, in Schedule order."""
    return [r for r in state["runs"] if r["status"] == RunStatus.PENDING]


def _plan_in_order(runs: list[Mapping[str, Any]]) -> bool:
    """Each Plan's Runs appear in step order."""
    last: dict[str, int] = {}
    for run in runs:
        if run["index"] < last.get(run["plan_id"], 0):
            return False
        last[run["plan_id"]] = run["index"]
    return True


def move_blocker(state: Mapping[str, Any], run_id: str, position: int) -> str | None:
    """Why a manual reorder is refused, or None (spec §5.7).

    `position` indexes the pending Runs. A Run may never be moved ahead of an
    earlier Run of its own Plan, and a Run may never be pushed behind a later
    one: either leaves the Plan out of step order.
    """
    pending = pending_runs(state)
    run = next((r for r in pending if r["id"] == run_id), None)
    if run is None:
        known = any(r["id"] == run_id for r in state["runs"])
        return "not_pending" if known else "unknown_run"
    if not 0 <= position < len(pending):
        return "bad_position"
    moved = [r for r in pending if r is not run]
    moved.insert(position, run)
    return None if _plan_in_order(moved) else "before_earlier_run"


def remove_plan_blocker(state: Mapping[str, Any], plan_id: str) -> str | None:
    """Why `remove_plan` is refused, or None (spec §13.2)."""
    runs = [r for r in state["runs"] if r["plan_id"] == plan_id]
    if plan_id not in state["plans"] and not runs:
        return "unknown_plan"
    if any(r["status"] in ACTIVE_RUN for r in runs):
        return "run_active"
    return None


def run_ordinal(state: Mapping[str, Any], run: Mapping[str, Any]) -> tuple[int, int]:
    """`(n, m)`: the Run's place among its Plan's Runs."""
    return run["index"], run["of"]


# ---------------------------------------------------------------------------
# Checks (spec §9.2): only the two built-ins this slice needs
# ---------------------------------------------------------------------------


def evaluate_checks(
    snapshot: Snapshot,
    zone: ZoneView,
    env: Env,
    blocking_error: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    """Failing Checks as `[{"check", "reason"}]`; empty means every Check passes."""
    failing: list[dict[str, str]] = []

    def fail(check: str, reason: str) -> None:
        failing.append({"check": check, "reason": reason})

    if not snapshot.available:
        fail("mower_ready", "unavailable")
    elif snapshot.activity is None:
        fail("mower_ready", "unknown_state")
    if snapshot.job_active:
        fail("mower_ready", "job_active")
    if blocking_error is not None:
        fail("mower_ready", "new_error")
    if snapshot.self_check is SelfCheckReason.DEVICE_NON_WORK_HOURS:
        fail("mower_ready", "device_non_work_hours")
    if snapshot.battery is None or snapshot.battery < env.min_start_battery_pct:
        fail("mower_ready", "battery_low")
    if zone.bearing is None:
        fail("zone_ready", "bearing_unset")
    return failing


# ---------------------------------------------------------------------------
# The reducer
# ---------------------------------------------------------------------------


def reduce(core_state: Mapping[str, Any], event: CoreInput, env: Env) -> Reduction:
    """`(state, input) -> (new state, effects)`; never mutates its argument."""
    machine = _Machine(copy.deepcopy(dict(core_state)), env, event.now)
    machine.apply(event)
    machine.sync_reminder_deadline()
    return Reduction(machine.core, tuple(machine.effects))


class _Machine:
    """One reduction's working copy; all rules live here."""

    def __init__(self, core: dict[str, Any], env: Env, now: datetime) -> None:
        self.core = core
        self.s: dict[str, Any] = core["state"]
        self.h: dict[str, Any] = core["history"]
        self.learned: dict[str, Any] = core["learned"]
        self.env = env
        self.now = now
        self.effects: list[Effect] = []

    # -- dispatch ---------------------------------------------------------

    def apply(self, event: CoreInput) -> None:
        match event:
            case AddPlan():
                self._add_plan(event)
            case EditRun():
                self._edit_run(event)
            case DeleteRun():
                self._delete_run(event)
            case MoveRun():
                self._move_run(event)
            case RemovePlan():
                self._remove_plan(event)
            case ReplacePlan():
                self._replace_plan(event)
            case RedrawAngles():
                self._redraw_angles(event)
            case StartSchedule():
                self._start_schedule("user_start")
            case PauseSchedule():
                self._pause()
            case StopNow():
                self._stop_now()
            case ResumeSchedule():
                self._start_schedule("user_resume")
            case CancelSchedule():
                self._cancel()
            case RetryRun():
                self._retry()
            case SkipPlan():
                self._skip_plan()
            case ResumeRun():
                self._resume_run()
            case EndRun():
                self._end_run()
            case AnswerPrompt():
                self._answer_prompt(event)
            case RaisePrompt():
                self._raise_prompt(
                    event.kind, event.subject, list(event.actions), dict(event.context)
                )
            case SnapshotReceived():
                self.s["snapshot"] = snapshot_to_dict(event.snapshot)
                self._evaluate()
            case SignalReceived():
                self._signal(event.signal)
            case CommandOutcome():
                self._command_outcome(event)
            case TimerFired():
                self._evaluate()
                self._fire_due_reminders()
            case Reconcile():
                self._reconcile(event)

    # -- small helpers ----------------------------------------------------

    @property
    def status(self) -> str:
        return self.s["schedule"]["status"]

    def _snapshot(self) -> Snapshot | None:
        raw = self.s["snapshot"]
        return snapshot_from_dict(raw) if raw else None

    def _emit(self, type_: str, **payload: Any) -> None:
        self.effects.append(EmitEvent(type_, payload))

    def _arm(self, name: str, at: datetime) -> None:
        self.s["deadlines"][name] = iso(at)
        self.effects.append(ArmTimer(name, at))

    def _cancel_deadline(self, name: str) -> None:
        if self.s["deadlines"].pop(name, None) is not None:
            self.effects.append(CancelTimer(name))

    def _deadline_due(self, name: str) -> bool:
        raw = self.s["deadlines"].get(name)
        return raw is not None and parse(raw) <= self.now

    def _set_schedule(self, status: ScheduleStatus, reason: str | None) -> None:
        sched = self.s["schedule"]
        before = sched["status"]
        sched["status"] = status.value
        sched["reason"] = reason
        if before == ScheduleStatus.IDLE and status != ScheduleStatus.IDLE:
            sched["started_at"] = iso(self.now)  # anchors the completion summary
        if before != status.value:
            self._emit(
                "schedule_status_changed",
                **{"from": before, "to": status.value, "reason": reason},
            )

    def _set_run(
        self, run: dict[str, Any], status: RunStatus, reason: str | None = None
    ) -> None:
        before = run["status"]
        run["status"] = status.value
        run["reason"] = reason
        self._emit(
            "run_status_changed",
            plan_id=run["plan_id"],
            run_id=run["id"],
            zone_id=run["zone_id"],
            zone_name=run["zone_name"],
            **{"from": before, "to": status.value, "reason": reason},
        )

    def _log_command(self, entry: dict[str, Any]) -> None:
        log = self.s["command_log"]
        log.append(entry)
        del log[:-COMMAND_LOG_MAX]

    def _archive_run(self, run: dict[str, Any]) -> None:
        """Move a Run to `history`; the Plan follows when its last Run leaves."""
        self.s["runs"].remove(run)
        self.h["runs"].append(run)
        plan_id = run["plan_id"]
        if not any(r["plan_id"] == plan_id for r in self.s["runs"]):
            plan = self.s["plans"].pop(plan_id, None)
            if plan is not None:
                self.h["plans"][plan_id] = plan

    # -- AddPlan / Start --------------------------------------------------

    def _insert_at(self, runs: list[dict[str, Any]], position: str) -> None:
        """Place new Runs: `end`, `front` or `merge` by step index (spec §5.8)."""
        current = self.s["runs"]
        if position == "front":
            at = next(
                (i for i, r in enumerate(current) if r["status"] == RunStatus.PENDING),
                len(current),
            )
            current[at:at] = runs
        elif position == "merge":
            floor = next(
                (i for i, r in enumerate(current) if r["status"] == RunStatus.PENDING),
                len(current),
            )
            for run in runs:
                at = next(
                    (
                        i
                        for i in range(floor, len(current))
                        if current[i]["index"] > run["index"]
                    ),
                    len(current),
                )
                current.insert(at, run)
                floor = at + 1
        else:
            current.extend(runs)

    def _add_plan(self, event: AddPlan) -> None:
        plan = copy.deepcopy(event.plan)
        self.s["plans"][plan["id"]] = plan
        self._insert_at(copy.deepcopy(event.runs), event.position)
        self._emit(
            "plan_added",
            plan_id=plan["id"],
            zone_id=plan["zone_id"],
            zone_name=plan["zone_name"],
            origin=plan["origin"],
            run_ids=[r["id"] for r in event.runs],
        )
        # `start: true` only acts on an idle Schedule; waiting/running append
        # only and paused never resumes (spec §13.3).
        if event.start and self.status == ScheduleStatus.IDLE:
            self._start_schedule("add_plan")

    # -- Editing the Schedule (spec §5.7, §13.2) --------------------------------
    # Only `pending` Runs change, in any Schedule status; the active Run is frozen.

    def _pending(self, run_id: str) -> dict[str, Any] | None:
        return next((r for r in pending_runs(self.s) if r["id"] == run_id), None)

    def _edit_run(self, event: EditRun) -> None:
        run = self._pending(event.run_id)
        if run is None:
            return
        changes = event.changes
        if "height_mm" in changes:
            run["height_mm"] = changes["height_mm"]
            run["settings"]["blade_height_mm"] = int(changes["height_mm"])
        if "angle_deg" in changes:
            run["angle_deg"] = changes["angle_deg"]
            run["angle_edited"] = True
        patch = dict(changes.get("settings") or {})
        extras = patch.pop("extras", None)
        run["settings"].update(patch)
        if extras:
            run["settings"]["extras"] = {**run["settings"]["extras"], **extras}

    def _drop_plan_if_empty(self, plan_id: str) -> None:
        """A Plan with no Run left on the Schedule leaves it; its inputs are kept
        in `history` only if some Run of it was finished."""
        if any(r["plan_id"] == plan_id for r in self.s["runs"]):
            return
        plan = self.s["plans"].pop(plan_id, None)
        if plan is not None and any(r["plan_id"] == plan_id for r in self.h["runs"]):
            self.h["plans"][plan_id] = plan

    def _settle_empty(self, reason: str) -> None:
        """Nothing left to run: a `waiting` Schedule with no pending Run, or a
        `paused` one with no Run at all, goes `idle`."""
        empty = not self.s["runs"]
        if (self.status == ScheduleStatus.WAITING and next_run(self.s) is None) or (
            self.status == ScheduleStatus.PAUSED and empty
        ):
            self._cancel_deadline(DEADLINE_STABILITY)
            self.s["waiting_on"] = []
            self.s["schedule"]["pause_requested"] = False
            self._set_schedule(ScheduleStatus.IDLE, reason)

    def _delete_run(self, event: DeleteRun) -> None:
        run = self._pending(event.run_id)
        if run is None:
            return
        self.s["runs"].remove(run)
        plan = self.s["plans"].get(run["plan_id"])
        if plan is not None and run["id"] in plan.get("run_ids", []):
            plan["run_ids"].remove(run["id"])
        self._drop_plan_if_empty(run["plan_id"])
        self._settle_empty("run_deleted")

    def _move_run(self, event: MoveRun) -> None:
        if move_blocker(self.s, event.run_id, event.position) is not None:
            return
        pending = pending_runs(self.s)
        run = next(r for r in pending if r["id"] == event.run_id)
        slots = [i for i, r in enumerate(self.s["runs"]) if r["status"] == RunStatus.PENDING]
        order = [r for r in pending if r is not run]
        order.insert(event.position, run)
        for slot, item in zip(slots, order):
            self.s["runs"][slot] = item

    def _remove_plan(self, event: RemovePlan) -> None:
        if remove_plan_blocker(self.s, event.plan_id) is not None:
            return
        plan = self.s["plans"].get(event.plan_id)
        for run in [r for r in self.s["runs"] if r["plan_id"] == event.plan_id]:
            if run["status"] == RunStatus.PENDING:
                run["finished_at"] = iso(self.now)
                self._set_run(run, RunStatus.CANCELLED, "plan_removed")
                self._archive_run(run)
        self._emit(
            "plan_removed",
            plan_id=event.plan_id,
            zone_id=plan["zone_id"] if plan else None,
            zone_name=plan["zone_name"] if plan else None,
        )
        self._settle_empty("plan_removed")

    def _replace_plan(self, event: ReplacePlan) -> None:
        plan_id = event.plan["id"]
        old = [r for r in self.s["runs"] if r["plan_id"] == plan_id]
        started = any(r["plan_id"] == plan_id for r in self.h["runs"])
        if not old or started or any(r["status"] != RunStatus.PENDING for r in old):
            return
        at = self.s["runs"].index(old[0])
        self.s["runs"] = [r for r in self.s["runs"] if r["plan_id"] != plan_id]
        self.s["runs"][at:at] = copy.deepcopy(event.runs)
        self.s["plans"][plan_id] = copy.deepcopy(event.plan)

    def _redraw_angles(self, event: RedrawAngles) -> None:
        by_id = {r["id"]: r for r in event.runs}
        for run in self.s["runs"]:
            new = by_id.get(run["id"])
            if new is not None and run["status"] == RunStatus.PENDING:
                run["angle_deg"] = new["angle_deg"]
                run["angle_draw"] = copy.deepcopy(new["angle_draw"])
                run.pop("angle_edited", None)
        plan = self.s["plans"].get(event.plan_id)
        if plan is not None and event.note:
            plan["note"] = event.note
            plan["angle_warnings"] = list(event.warnings or [])

    def _start_schedule(self, reason: str) -> None:
        if self.status not in (ScheduleStatus.IDLE, ScheduleStatus.PAUSED):
            return
        if next_run(self.s) is None:
            return
        # A failed Run leaves only by Retry / Skip Plan / Abort (spec §6.2).
        if any(r["status"] == RunStatus.FAILED for r in self.s["runs"]):
            return
        self.s["blocking_error"] = None
        self.s["schedule"]["pause_requested"] = False
        self._set_schedule(ScheduleStatus.WAITING, reason)
        self._evaluate()

    # -- Pause / Stop now / Retry / Skip Plan (spec §6.3, §6.4) -------------

    def _pause(self) -> None:
        """Soft Pause: set `pause_requested`, never command the Mower."""
        if self.status not in (ScheduleStatus.WAITING, ScheduleStatus.RUNNING):
            return
        self.s["schedule"]["pause_requested"] = True
        if self.status == ScheduleStatus.WAITING:
            self._cancel_deadline(DEADLINE_STABILITY)
            self._set_schedule(ScheduleStatus.PAUSED, "user_pause")
        # `running`: the Run ends first; `_finish_run` then pauses.

    def _stop_now(self) -> None:
        """The active Run returns to `pending` at the head; Schedule `paused`."""
        run = active_run(self.s)
        if run is None:
            return
        self._clear_run_deadlines()
        self._answer_kind(PROMPT_RUN_ON_HOLD, "stop_now")
        for key in ("starting_at", "started_at", "job_ref", "finished_at", "on_hold_reason"):
            run.pop(key, None)
        self._set_run(run, RunStatus.PENDING, "stop_now")
        self.s["runs"].remove(run)
        self.s["runs"].insert(0, run)
        self.effects.append(AbortRunEffect("stop_now"))
        self._set_schedule(ScheduleStatus.PAUSED, "stop_now")


    # -- Restart reconciliation (spec §8) ------------------------------------

    def _summary(self) -> dict[str, Any]:
        run = self._current_run()
        return {"schedule": self.status, "run": run["status"] if run else None}

    def _current_run(self) -> dict[str, Any] | None:
        """The Run in flight, or the failed one awaiting a decision."""
        return active_run(self.s) or next(
            (r for r in self.s["runs"] if r["status"] == RunStatus.FAILED), None
        )

    def _reconcile(self, event: Reconcile) -> None:
        """Test live evidence against the Run rules before any clock fires.

        Never emits `start_run`; the only command is `abort_run`, through the
        normal start-window and stall rules.
        """
        before = self._summary()
        run_before = active_run(self.s)
        run_id = run_before["id"] if run_before else None
        # Stability windows are state-derived: downtime does not count.
        for name in RESTART_WINDOWS:
            self.s["deadlines"].pop(name, None)
        reasons: list[str] = []
        job_check: str | None = None
        snapshot = event.snapshot
        if snapshot is None:
            reasons.append("no_usable_snapshot")
            self._mark_unavailable()
            if (run := active_run(self.s)) is not None and run["status"] == RunStatus.MOWING:
                self.s["deadlines"][DEADLINE_CONTACT_LOST] = iso(
                    event.loaded_at + CONTACT_LOST_GRACE
                )
        else:
            self.s["snapshot"] = snapshot_to_dict(snapshot)
        for signal in event.signals:  # missed errors, external jobs
            self._signal(signal)
        run = active_run(self.s)
        if run is not None and snapshot is not None:
            job_check = self._reconcile_superseded(run, snapshot, event.signals)
        self._rearm_height_verify()
        self._evaluate()
        self._fire_due_reminders()
        run = active_run(self.s)
        if run is None or run["status"] != RunStatus.ON_HOLD:
            self._answer_kind(PROMPT_RUN_ON_HOLD, "reconciled")
        after = self._summary()
        if run_id is None:
            reasons.append("no_run")
        else:
            moved = next(
                (r for r in [*self.s["runs"], *self.h["runs"]] if r["id"] == run_id), None
            )
            if moved is not None and moved["status"] != before["run"]:
                reasons.append(moved.get("reason") or moved["status"])
            else:
                reasons.append("unchanged")
        payload: dict[str, Any] = {
            "before": before,
            "after": after,
            "reasons": reasons,
            "snapshot": "live" if snapshot is not None else "none",
        }
        if job_check is not None:
            payload["job_check"] = job_check
        self._emit("reconciled", **payload)

    def _mark_unavailable(self) -> None:
        """No live evidence: the persisted summary must not read as live."""
        raw = self.s["snapshot"]
        if raw:
            raw.update(
                available=False, activity=None, job_active=False,
                recharging=False, job_ref=None,
            )

    def _reconcile_superseded(
        self, run: dict[str, Any], snapshot: Snapshot, signals: tuple[AdapterSignal, ...]
    ) -> str | None:
        """Fail the Run "superseded" when the live job is not ours.

        Returns which check applied: `job_ref`, or the weaker `zone`.
        """
        if run["status"] == RunStatus.STARTING:
            intent = self.s["intent"]
            issued = parse(intent["issued_at"]) if intent else None
            early = any(
                isinstance(sig, JobStarted) and issued is not None and sig.started_at < issued
                for sig in signals
            )
            mowing = snapshot.activity is LawnMowerActivity.MOWING and snapshot.job_active
            zone_off = (
                mowing and snapshot.zone is not None and snapshot.zone != run.get("native_ref")
            )
            if early or zone_off:
                self._fail_run(run, "superseded", abort=False)
            return "zone" if mowing else None
        if run["status"] not in (RunStatus.MOWING, RunStatus.ON_HOLD) or not snapshot.job_active:
            return None
        if snapshot.job_ref is not None and run.get("job_ref") is not None:
            if snapshot.job_ref != run["job_ref"]:
                self._fail_run(run, "superseded", abort=False)
            return "job_ref"
        if snapshot.zone is not None and snapshot.zone != run.get("native_ref"):
            self._fail_run(run, "superseded", abort=False)
        return "zone"

    def _rearm_height_verify(self) -> None:
        """An open verification restarts: readback is tested first (by the
        evaluation that follows), the stability window is already cleared and
        the deadline is re-armed from now."""
        verify = self.s["height_verify"]
        if verify is None or active_run(self.s) is None:
            return
        verify["unavailable"] = False
        verify["ignore_until"] = None
        self._arm(DEADLINE_HEIGHT_VERIFY, self.now + self.env.height_verify_window)

    def _in_work_hours(self, moment: datetime) -> bool:
        env = self.env
        at = moment.astimezone(env.tz).time()
        if env.work_end == time(0, 0):
            return at >= env.work_start
        if env.work_start < env.work_end:
            return env.work_start <= at < env.work_end
        return at >= env.work_start or at < env.work_end

    def _next_work_open(self, moment: datetime) -> datetime:
        """The next moment the Work hours window opens, strictly after `moment`."""
        local = moment.astimezone(self.env.tz)
        day = local.date()
        for _ in range(3):
            opens = datetime.combine(day, self.env.work_start, tzinfo=self.env.tz)
            if opens > local:
                return opens.astimezone(timezone.utc)
            day += timedelta(days=1)
        raise AssertionError("unreachable")  # pragma: no cover

    def _fire_due_reminders(self) -> None:
        """Send each due reminder once; overnight ones wait for the morning.

        After a reminder (or a deferral) the next one is the next Work hours
        opening, so a Prompt left alone gets one reminder each morning
        (spec §12.4). A reminder that fell due during downtime therefore also
        fires once on load, not once per missed slot (spec §12.11).
        """
        for prompt in self.s["prompts"]:
            due = prompt.get("next_reminder_at")
            if due is None or parse(due) > self.now:
                continue
            prompt["next_reminder_at"] = iso(self._next_work_open(self.now))
            if self._in_work_hours(self.now):
                self._emit(
                    "prompt_reminder",
                    prompt_id=prompt["prompt_id"],
                    kind=prompt["kind"],
                    subject=prompt["subject"],
                    tag=prompt["tag"],
                    plan_id=prompt["context"].get("plan_id"),
                    run_id=prompt["context"].get("run_id"),
                )

    def sync_reminder_deadline(self) -> None:
        """Keep one persisted deadline on the earliest pending reminder."""
        dues = [
            parse(p["next_reminder_at"])
            for p in self.s["prompts"]
            if p.get("next_reminder_at")
        ]
        current = self.s["deadlines"].get(DEADLINE_PROMPT_REMINDER)
        if not dues:
            self._cancel_deadline(DEADLINE_PROMPT_REMINDER)
        elif current != iso(min(dues)):
            self._arm(DEADLINE_PROMPT_REMINDER, min(dues))

    # -- Checks and starting a Run ----------------------------------------

    def _evaluate(self) -> None:
        """Re-evaluate everything that depends on state or time."""
        if self.status == ScheduleStatus.WAITING:
            self._evaluate_waiting()
        elif self.status == ScheduleStatus.RUNNING:
            run = active_run(self.s)
            if run is None:
                return
            if run["status"] == RunStatus.STARTING:
                self._evaluate_starting(run)
            elif run["status"] == RunStatus.MOWING:
                self._evaluate_mowing(run)
            elif run["status"] == RunStatus.ON_HOLD:
                self._evaluate_on_hold(run)

    def _evaluate_waiting(self) -> None:
        run = next_run(self.s)
        snapshot = self._snapshot()
        if run is None or snapshot is None:
            return
        zone = self.env.zones.get(run["zone_id"])
        if zone is None or zone.missing:
            self._cancel_deadline(DEADLINE_STABILITY)
            self._fail_run(run, "zone_missing", abort=False)
            return
        failing = evaluate_checks(snapshot, zone, self.env, self.s["blocking_error"])
        self.s["waiting_on"] = failing
        if failing:
            self._cancel_deadline(DEADLINE_STABILITY)
            return
        if DEADLINE_STABILITY not in self.s["deadlines"]:
            self._arm(DEADLINE_STABILITY, self.now + STABILITY_WINDOW)
        elif self._deadline_due(DEADLINE_STABILITY):
            self._start_run(run, zone)

    def _start_run(self, run: dict[str, Any], zone: ZoneView) -> None:
        """Persist the command intent, then (after the save) command the Mower."""
        self._cancel_deadline(DEADLINE_STABILITY)
        self.s["waiting_on"] = []
        run["native_ref"] = zone.native_ref
        run["starting_at"] = iso(self.now)
        self.s["active_run_id"] = run["id"]
        self._set_run(run, RunStatus.STARTING)
        self._set_schedule(ScheduleStatus.RUNNING, "run_started")
        entry = {"command": "start_run", "run_id": run["id"], "issued_at": iso(self.now)}
        self.s["intent"] = dict(entry)
        self._log_command({**entry, "result": None})
        self._arm(DEADLINE_START_WINDOW, self.now + self.env.start_window)
        settings = run["settings"]
        self.effects.append(
            StartRunEffect(
                run_id=run["id"],
                zone_ref=zone.native_ref,
                settings=RunSettings(
                    blade_height_mm=settings["blade_height_mm"],
                    bearing_deg=((zone.bearing or 0.0) + run["angle_deg"]) % 180,
                    spacing_cm=settings["spacing_cm"],
                    speed_m_s=settings["speed_m_s"],
                    perimeter_laps=settings["perimeter_laps"],
                    extras=dict(settings["extras"]),
                ),
            )
        )

    # -- Outcome detection (spec §7) ---------------------------------------

    def _evaluate_starting(self, run: dict[str, Any]) -> None:
        snapshot = self._snapshot()
        if (
            snapshot is not None
            and snapshot.activity is LawnMowerActivity.MOWING
            and snapshot.job_active
        ):
            self._cancel_deadline(DEADLINE_START_WINDOW)
            run["started_at"] = iso(self.now)
            run["job_ref"] = snapshot.job_ref
            self._set_run(run, RunStatus.MOWING)
            self._arm_stall(snapshot)
            self._begin_height_verify(run, snapshot)
            return
        if self._deadline_due(DEADLINE_START_WINDOW):
            self._fail_run(run, "did_not_start", abort=True)

    def _evaluate_mowing(self, run: dict[str, Any]) -> None:
        snapshot = self._snapshot()
        if snapshot is None:
            return
        # Contact lost: the Run state is frozen for the 10 min grace (clocks
        # keep running but nothing is decided from stale evidence); then hold.
        if not snapshot.available or snapshot.activity is None:
            if self._stable(DEADLINE_CONTACT_LOST, True, CONTACT_LOST_GRACE):
                self._enter_hold(run, HOLD_CONTACT_LOST)
            return
        self._cancel_deadline(DEADLINE_CONTACT_LOST)
        # The Mower's own non-work window began mid-job: it pauses itself and
        # resumes by itself; nobody needs to act (spec §7.4).
        if snapshot.job_active and snapshot.self_check is SelfCheckReason.DEVICE_NON_WORK_HOURS:
            self._enter_hold(run, HOLD_DEVICE_WORK_HOURS)
            return
        # returning/docked + job_active false, stable 60 s. With job_active
        # true it is a breakpoint or mid-job recharge: the Run continues.
        # Progress is never a done signal (it reads 0 when idle).
        off_job = (
            snapshot.activity in (LawnMowerActivity.RETURNING, LawnMowerActivity.DOCKED)
            and not snapshot.job_active
        )
        if self._stable(DEADLINE_DONE_STABLE, off_job):
            self._finish_run(run)
            return
        # idle + no job after start: the Mower stopped off dock (spec §7.3).
        stopped = snapshot.activity is LawnMowerActivity.IDLE and not snapshot.job_active
        if self._stable(DEADLINE_STOPPED_STABLE, stopped):
            self._fail_run(run, "stopped_off_dock", abort=False)
            return
        if self._external_pause(snapshot):
            self._enter_hold(run, HOLD_EXTERNAL_PAUSE)
            return
        if self._recharge_stalled(snapshot):
            self._enter_hold(run, HOLD_RECHARGE_STALL)
            return
        self._evaluate_height(run, snapshot)
        # Off the job (returning/docked/idle, no job) is the finish or stop
        # rule's to decide, never a stall.
        if run["status"] == RunStatus.MOWING and not (off_job or stopped):
            self._evaluate_stall(run, snapshot)

    # -- Blade height verification (spec §7.6) -----------------------------

    def _begin_height_verify(self, run: dict[str, Any], snapshot: Snapshot) -> None:
        """Start a verification segment: called on every entry into `mowing`
        (first start, and re-entry after recharge, hold or resume)."""
        run["height_check"] = None
        self._cancel_deadline(DEADLINE_HEIGHT_STABLE)
        if self.env.height_readback_resolution_mm is None:
            self._height_unverified(run)
            return
        self.s["height_verify"] = {
            "unavailable": not snapshot.available,
            "ignore_until": None,
            "seen": False,
        }
        self._arm(DEADLINE_HEIGHT_VERIFY, self.now + self.env.height_verify_window)

    def _height_unverified(self, run: dict[str, Any]) -> None:
        """Proceed without verification: event only, no push (spec §7.6.6)."""
        run["height_check"] = "unverified"
        self._end_height_verify()
        self._emit("height_unverified", run_id=run["id"], zone_name=run["zone_name"])

    def _end_height_verify(self) -> None:
        self.s["height_verify"] = None
        self._cancel_deadline(DEADLINE_HEIGHT_VERIFY)
        self._cancel_deadline(DEADLINE_HEIGHT_STABLE)

    def _evaluate_height(self, run: dict[str, Any], snapshot: Snapshot) -> None:
        verify = self.s["height_verify"]
        resolution = self.env.height_readback_resolution_mm
        if run.get("height_check") is not None or verify is None or resolution is None:
            return  # decided for this segment; no continuous drift monitoring
        # Readings while unavailable, and in the 60 s after becoming available,
        # are ignored (the false 100 mm reconnect blip).
        if not snapshot.available:
            verify["unavailable"] = True
            verify["ignore_until"] = None
        elif verify["unavailable"]:
            verify["unavailable"] = False
            verify["ignore_until"] = iso(self.now + STABILITY_WINDOW)
        ignore_until = verify["ignore_until"]
        usable = (
            snapshot.available
            and snapshot.blade_height_mm is not None
            and (ignore_until is None or self.now >= parse(ignore_until))
        )
        in_tolerance = False
        if usable:
            verify["seen"] = True
            target = run["settings"]["blade_height_mm"]
            in_tolerance = abs(snapshot.blade_height_mm - target) <= resolution
        if self._stable(DEADLINE_HEIGHT_STABLE, in_tolerance):
            run["height_check"] = "verified"
            run["height_readback_mm"] = snapshot.blade_height_mm
            self._end_height_verify()
        elif self._deadline_due(DEADLINE_HEIGHT_VERIFY):
            if verify["seen"]:
                run["height_check"] = "not_reached"
                self._fail_run(run, "height_not_reached", abort=True)
            else:
                self._height_unverified(run)

    # -- On hold (spec §7.4) -------------------------------------------------

    def _external_pause(self, snapshot: Snapshot) -> bool:
        """A pause the director did not command, not a recharge, stable 60 s.

        Attribution is by the command log: the director's own `pause_run`
        (none is issued in phase 1) is never an external pause.
        """
        commanded = any(c["command"] == "pause_run" for c in self.s["command_log"])
        paused = (
            snapshot.activity is LawnMowerActivity.PAUSED
            and snapshot.job_active
            and not snapshot.recharging
            and not commanded
        )
        return self._stable(DEADLINE_PAUSE_STABLE, paused)

    def _recharge_stalled(self, snapshot: Snapshot) -> bool:
        """Battery not risen for 60 min while `recharging` (spec §7.4)."""
        if not snapshot.recharging:
            self.s["battery_trend"] = None
            self._cancel_deadline(DEADLINE_RECHARGE_STALL)
            return False
        trend = self.s["battery_trend"]
        if snapshot.battery is not None and (
            trend is None or snapshot.battery > trend["battery"]
        ):
            self.s["battery_trend"] = {"battery": snapshot.battery, "since": iso(self.now)}
            self._arm(DEADLINE_RECHARGE_STALL, self.now + RECHARGE_STALL_WINDOW)
            return False
        return self._deadline_due(DEADLINE_RECHARGE_STALL)

    def _enter_hold(self, run: dict[str, Any], cause: str) -> None:
        for name in (
            DEADLINE_DONE_STABLE,
            DEADLINE_STOPPED_STABLE,
            DEADLINE_STALL,
            DEADLINE_PAUSE_STABLE,
            DEADLINE_CONTACT_LOST,
            DEADLINE_RECHARGE_STALL,
        ):
            self._cancel_deadline(name)
        self._end_height_verify()
        run["on_hold_reason"] = cause
        self._set_run(run, RunStatus.ON_HOLD, cause)
        if cause != HOLD_DEVICE_WORK_HOURS:  # the Mower resumes by itself
            self._raise_prompt(
                PROMPT_RUN_ON_HOLD,
                None,
                [ACTION_RESUME, ACTION_END_RUN],
                self._run_context(run, cause),
                remind=True,
            )

    def _leave_hold(self, run: dict[str, Any], snapshot: Snapshot) -> None:
        """The cause cleared: `mowing` again, stall window re-armed."""
        self._cancel_deadline(DEADLINE_HOLD_CLEAR_STABLE)
        run.pop("on_hold_reason", None)
        self._answer_kind(PROMPT_RUN_ON_HOLD, "cleared")
        self._set_run(run, RunStatus.MOWING)
        self._arm_stall(snapshot)
        self.s["battery_trend"] = None
        self._begin_height_verify(run, snapshot)  # re-verify on every return (§7.6.5)
        self._evaluate_mowing(run)

    def _on_hold_run(self) -> dict[str, Any] | None:
        run = active_run(self.s)
        if run is not None and run["status"] == RunStatus.ON_HOLD:
            return run
        return None

    def _resume_run(self) -> None:
        """Resume: send `resume_run`; the Run stays on hold until the cause
        clears (spec §6.3). The command is logged so it is attributable."""
        run = self._on_hold_run()
        if run is None:
            return
        self._answer_kind(PROMPT_RUN_ON_HOLD, ACTION_RESUME)
        self._log_command(
            {
                "command": "resume_run",
                "run_id": run["id"],
                "issued_at": iso(self.now),
                "result": None,
            }
        )
        self.effects.append(ResumeRunEffect(run["id"]))

    def _end_run(self) -> None:
        """End Run: `abort_run`, and the Run fails "ended by user"."""
        run = self._on_hold_run()
        if run is None:
            return
        self._answer_kind(PROMPT_RUN_ON_HOLD, ACTION_END_RUN)
        run.pop("on_hold_reason", None)
        self._fail_run(run, "ended_by_user", abort=True)

    def _rain_stop(self) -> None:
        """The Mower ended the job for rain: not a failure (spec §7.5).

        The Run returns to `pending` and the Schedule to `waiting`, with no
        command. The Wet grass confirm-or-ask flow hooks onto this transition.
        """
        run = active_run(self.s)
        if run is None or run["status"] not in (RunStatus.MOWING, RunStatus.ON_HOLD):
            return
        self._clear_run_deadlines()
        self._cancel_deadline(DEADLINE_STABILITY)
        self._answer_kind(PROMPT_RUN_ON_HOLD, "rain_stop")
        run.pop("on_hold_reason", None)
        run.pop("started_at", None)
        self.s["battery_trend"] = None
        self._set_run(run, RunStatus.PENDING, "rain_stop")
        self._set_schedule(ScheduleStatus.WAITING, "rain_stop")
        self._evaluate()

    def _evaluate_on_hold(self, run: dict[str, Any]) -> None:
        snapshot = self._snapshot()
        if snapshot is None:
            return
        cause = run.get("on_hold_reason")
        if cause == HOLD_RECHARGE_STALL:
            trend = self.s["battery_trend"]
            if (
                trend is not None
                and snapshot.battery is not None
                and snapshot.battery > trend["battery"]
            ):
                self._leave_hold(run, snapshot)
        elif cause == HOLD_CONTACT_LOST:
            if snapshot.available and snapshot.activity is not None:
                self._leave_hold(run, snapshot)  # normal rules re-evaluate
        elif cause == HOLD_DEVICE_WORK_HOURS:
            resumed = (
                snapshot.activity is LawnMowerActivity.MOWING
                and snapshot.job_active
                and snapshot.self_check is not SelfCheckReason.DEVICE_NON_WORK_HOURS
            )
            if self._stable(DEADLINE_HOLD_CLEAR_STABLE, resumed):
                self._leave_hold(run, snapshot)
        elif cause == HOLD_EXTERNAL_PAUSE:
            resumed = snapshot.activity is LawnMowerActivity.MOWING and snapshot.job_active
            if self._stable(DEADLINE_HOLD_CLEAR_STABLE, resumed):
                self._leave_hold(run, snapshot)

    def _run_context(self, run: Mapping[str, Any], reason: str) -> dict[str, Any]:
        plan = self.s["plans"].get(run["plan_id"]) or {}
        return {
            "zone_name": run["zone_name"],
            "plan_id": run["plan_id"],
            "run_id": run["id"],
            "n": run["index"],
            "m": run["of"],
            "height_mm": run["height_mm"],
            "plan_start_height_mm": plan.get("start_height_mm", run["height_mm"]),
            "plan_end_height_mm": plan.get("end_height_mm", run["height_mm"]),
            "reason": reason,
        }

    def _arm_stall(self, snapshot: Snapshot) -> None:
        self.s["stall_movement"] = movement_to_dict(snapshot)
        self._arm(DEADLINE_STALL, self.now + self.env.stall_window)

    def _evaluate_stall(self, run: dict[str, Any], snapshot: Snapshot) -> None:
        """No change in any `movement` indicator for the stall window while
        `mowing`/`returning` aborts the job (spec §7.3). Any other activity
        (a mid-job recharge, a breakpoint) holds the clock: it restarts when
        the Mower moves again. There is no Run time limit."""
        if snapshot.activity not in (LawnMowerActivity.MOWING, LawnMowerActivity.RETURNING):
            self._arm_stall(snapshot)
        elif movement_to_dict(snapshot) != self.s["stall_movement"]:
            self._arm_stall(snapshot)  # evidence first: movement beats the clock
        elif self._deadline_due(DEADLINE_STALL):
            self._fail_run(run, "stalled", abort=True)

    def _stable(
        self, name: str, holds: bool, window: timedelta = STABILITY_WINDOW
    ) -> bool:
        """True once `holds` has held for `window` (60 s stability, spec §7.7).

        The first holding snapshot arms the window, any non-holding one
        cancels it (flicker), and the window counts only when it is due.
        """
        if not holds:
            self._cancel_deadline(name)
            return False
        if name not in self.s["deadlines"]:
            self._arm(name, self.now + window)
            return False
        return self._deadline_due(name)

    def _signal(self, signal: AdapterSignal) -> None:
        if isinstance(signal, ZoneTaskComplete):
            run = active_run(self.s)
            if (
                run is not None
                and run["status"] == RunStatus.MOWING
                and signal.zone == run.get("native_ref")
            ):
                self._finish_run(run)
        elif isinstance(signal, NewError):
            last = self.s["last_error_time"]
            if last is not None and signal.time <= parse(last):
                return  # the adapter re-emits after a restart; dedupe by time
            self.s["last_error_time"] = iso(signal.time)
            error = {
                "code": signal.code,
                "message": signal.message,
                "time": iso(signal.time),
            }
            run = self._mowing_run()
            if run is not None:
                # Every code is treated alike; the Mower is usually locked in
                # `error`, so no command (spec §7.3).
                run["error"] = error
                self._fail_run(run, "error", abort=False)
            elif self.status == ScheduleStatus.WAITING:
                self.s["blocking_error"] = error
                self._evaluate()
        elif isinstance(signal, ZoneTaskAborted):
            run = self._mowing_run()
            if run is not None and signal.zone == run.get("native_ref"):
                self._fail_run(run, "externally_aborted", abort=False)
        elif isinstance(signal, RainStop):
            self._rain_stop()
        elif isinstance(signal, JobStarted):
            run = self._mowing_run()
            if run is not None and self._is_external_job(run, signal):
                self._fail_run(run, "superseded", abort=False)

    def _mowing_run(self) -> dict[str, Any] | None:
        run = active_run(self.s)
        if run is not None and run["status"] == RunStatus.MOWING:
            return run
        return None

    @staticmethod
    def _is_external_job(run: Mapping[str, Any], signal: JobStarted) -> bool:
        """A job start the director did not command (spec §8.5): a different
        `job_ref` than the one recorded at the `mowing` transition, or, with
        no `job_ref` either side, a different Zone."""
        if signal.job_ref is not None and run.get("job_ref") is not None:
            return signal.job_ref != run["job_ref"]
        return signal.zone != run.get("native_ref")

    def _command_outcome(self, event: CommandOutcome) -> None:
        for entry in reversed(self.s["command_log"]):
            if (
                entry["run_id"] == event.run_id
                and entry["command"] == event.command
                and entry["result"] is None
            ):
                entry["result"] = event.result.kind.value
                break
        run = next((r for r in self.s["runs"] if r["id"] == event.run_id), None)
        # Accepted and unconfirmed both wait for the start window; a 500 is
        # never trusted either way and never retried (ADR 0002).
        if (
            event.result.kind is CommandKind.REFUSED
            and run is not None
            and run["status"] == RunStatus.STARTING
        ):
            self._fail_run(run, "did_not_start", abort=False)

    # -- Run endings --------------------------------------------------------

    def _clear_run_deadlines(self) -> None:
        for name in (
            DEADLINE_START_WINDOW,
            DEADLINE_DONE_STABLE,
            DEADLINE_STOPPED_STABLE,
            DEADLINE_STALL,
            DEADLINE_HEIGHT_VERIFY,
            DEADLINE_HEIGHT_STABLE,
            DEADLINE_PAUSE_STABLE,
            DEADLINE_HOLD_CLEAR_STABLE,
            DEADLINE_RECHARGE_STALL,
            DEADLINE_CONTACT_LOST,
        ):
            self._cancel_deadline(name)
        self.s["height_verify"] = None
        self.s["active_run_id"] = None

    def _finish_run(self, run: dict[str, Any]) -> None:
        """Run `done`: archive atomically, then advance the Schedule."""
        self._clear_run_deadlines()
        run["finished_at"] = iso(self.now)
        self._set_run(run, RunStatus.DONE)
        started = run.get("started_at")
        if started is not None:
            samples = self.learned["durations"].setdefault(run["zone_id"], [])
            samples.append((self.now - parse(started)).total_seconds())
            del samples[:-DURATION_SAMPLES_MAX]
        self._archive_run(run)
        if next_run(self.s) is not None:
            if self.s["schedule"]["pause_requested"]:
                self._set_schedule(ScheduleStatus.PAUSED, "pause_requested")
            else:
                self._set_schedule(ScheduleStatus.WAITING, "run_done")
                self._evaluate()
        else:
            self.s["schedule"]["pause_requested"] = False
            plan_ids = sorted({r["plan_id"] for r in self.h["runs"][-50:]})
            self._set_schedule(ScheduleStatus.IDLE, "schedule_completed")
            self._emit(
                "schedule_completed",
                plan_id=run["plan_id"],
                plan_ids=plan_ids,
                summary=self._schedule_summary(),
            )

    def _schedule_summary(self) -> dict[str, Any]:
        """What the finished Schedule did (spec §12.9), from archived Runs."""
        began = self.s["schedule"].get("started_at")
        runs = [
            r
            for r in self.h["runs"]
            if began is None or (r.get("finished_at") or "") >= began
        ]
        done = [r for r in runs if r["status"] == RunStatus.DONE]
        mowing_s = sum(
            (parse(r["finished_at"]) - parse(r["started_at"])).total_seconds()
            for r in done
            if r.get("started_at") and r.get("finished_at")
        )
        return {
            "plans": len({r["plan_id"] for r in runs}),
            "runs_done": len(done),
            "runs_skipped": sum(1 for r in runs if r["status"] == RunStatus.SKIPPED),
            "mowing_s": round(mowing_s),
            "anomalies": len(self.s.get("schedule_anomalies", [])),
        }

    def _fail_run(self, run: dict[str, Any], reason: str, *, abort: bool) -> None:
        """Run `failed`: it stays in `state`; the Schedule pauses for a decision."""
        self._clear_run_deadlines()
        run["finished_at"] = iso(self.now)
        self._set_run(run, RunStatus.FAILED, reason)
        if abort:
            self.effects.append(AbortRunEffect(reason))
        self._set_schedule(ScheduleStatus.PAUSED, reason)
        self._raise_prompt(
            PROMPT_RUN_FAILED,
            None,
            [ACTION_RETRY, ACTION_SKIP_PLAN, ACTION_ABORT],
            self._run_context(run, reason),
            remind=True,
        )

    # -- Prompts (spec §12.5, §12.6) ------------------------------------------

    def _raise_prompt(
        self,
        kind: str,
        subject: str | None,
        actions: list[str],
        context: dict[str, Any],
        *,
        remind: bool = False,
    ) -> None:
        """A new Prompt of the same kind and subject supersedes the old one."""
        self.s["prompts"] = [
            p
            for p in self.s["prompts"]
            if not (p["kind"] == kind and p["subject"] == subject)
        ]
        tag = f"mowdir_{self.env.entry_id}_{kind}" + (f"_{subject}" if subject else "")
        prompt = {
            "prompt_id": self.env.new_id(),
            "kind": kind,
            "subject": subject,
            "tag": tag,
            "actions": actions,
            "context": context,
            "sent_at": iso(self.now),
            "next_reminder_at": iso(self.now + PROMPT_REMINDER_AFTER) if remind else None,
            "status": "pending",
        }
        self.s["prompts"].append(prompt)
        self._emit(
            "prompt_sent",
            prompt_id=prompt["prompt_id"],
            kind=kind,
            subject=subject,
            tag=tag,
            actions=list(actions),
            plan_id=context.get("plan_id"),
            run_id=context.get("run_id"),
        )

    def _answer_kind(self, kind: str, action: str) -> None:
        """A dedicated service answers every pending Prompt of `kind` (§12.12)."""
        for prompt in [p for p in self.s["prompts"] if p["kind"] == kind]:
            self._settle_prompt(prompt, action)

    def _settle_prompt(self, prompt: dict[str, Any], action: str) -> None:
        self.s["prompts"].remove(prompt)
        self._emit(
            "prompt_answered",
            prompt_id=prompt["prompt_id"],
            kind=prompt["kind"],
            subject=prompt["subject"],
            tag=prompt["tag"],
            action=action,
            plan_id=prompt["context"].get("plan_id"),
            run_id=prompt["context"].get("run_id"),
        )

    def _answer_prompt(self, event: AnswerPrompt) -> None:
        prompt = next(
            (p for p in self.s["prompts"] if p["prompt_id"] == event.prompt_id), None
        )
        if prompt is None or event.action not in prompt["actions"]:
            return  # already answered, stale or not offered: nothing happens
        if prompt["kind"] == PROMPT_RUN_ON_HOLD:
            if event.action == ACTION_RESUME:
                self._resume_run()
            else:
                self._end_run()
        elif prompt["kind"] == PROMPT_RUN_FAILED:
            # The dedicated input does the work and answers the Prompt.
            if event.action == ACTION_RETRY:
                self._retry()
            elif event.action == ACTION_SKIP_PLAN:
                self._skip_plan()
            else:
                self._cancel()
        if prompt in self.s["prompts"]:  # no dedicated side effect applied
            self._settle_prompt(prompt, event.action)

    # -- Answering a failure (spec §6.3, §6.4) ---------------------------------

    def _failed_run(self) -> dict[str, Any] | None:
        """The failed Run awaiting a decision while the Schedule is paused."""
        if self.status != ScheduleStatus.PAUSED:
            return None
        for run in self.s["runs"]:
            if run["status"] == RunStatus.FAILED:
                return run
        return None

    def _after_answer(self, reason: str) -> None:
        """Leave `paused`: `waiting` (Runs left) or `idle`; `pause_requested` wins."""
        if self.s["schedule"]["pause_requested"]:
            self._set_schedule(ScheduleStatus.PAUSED, "pause_requested")
        elif next_run(self.s) is not None:
            self._set_schedule(ScheduleStatus.WAITING, reason)
            self._evaluate()
        else:
            self._set_schedule(ScheduleStatus.IDLE, reason)

    def _retry(self) -> None:
        """Retry: the failed Run goes back to `pending` at the head. No command."""
        run = self._failed_run()
        if run is None:
            return
        self._answer_kind(PROMPT_RUN_FAILED, ACTION_RETRY)
        self.s["runs"].remove(run)
        self.s["runs"].insert(0, run)
        run.pop("finished_at", None)
        self._set_run(run, RunStatus.PENDING)
        self._after_answer("retry")

    def _skip_plan(self) -> None:
        """Skip Plan: the failed Run stays `failed`, the Plan's other Runs skip."""
        failed = self._failed_run()
        if failed is None:
            return
        self._answer_kind(PROMPT_RUN_FAILED, ACTION_SKIP_PLAN)
        plan_id = failed["plan_id"]
        for run in [r for r in self.s["runs"] if r["plan_id"] == plan_id]:
            if run["status"] == RunStatus.PENDING:
                run["finished_at"] = iso(self.now)
                self._set_run(run, RunStatus.SKIPPED)
            self._archive_run(run)
        self._after_answer("skip_plan")

    # -- Cancel ---------------------------------------------------------------

    def _cancel(self) -> None:
        """Abort: every Run leaves `state`, the Schedule is `idle` and empty."""
        active = active_run(self.s)
        self._answer_kind(PROMPT_RUN_FAILED, ACTION_ABORT)
        self._answer_kind(PROMPT_RUN_ON_HOLD, "cancelled")
        self._clear_run_deadlines()
        self._cancel_deadline(DEADLINE_STABILITY)
        for run in list(self.s["runs"]):
            if run["status"] not in (RunStatus.FAILED, *TERMINAL_RUN):
                run["finished_at"] = iso(self.now)
                self._set_run(run, RunStatus.CANCELLED)
            self._archive_run(run)
        if active is not None:
            self.effects.append(AbortRunEffect("cancelled"))
        self.s["waiting_on"] = []
        self.s["blocking_error"] = None
        self.s["schedule"]["pause_requested"] = False
        if self.status != ScheduleStatus.IDLE:
            self._set_schedule(ScheduleStatus.IDLE, "cancelled")
