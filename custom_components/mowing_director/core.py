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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from homeassistant.components.lawn_mower import LawnMowerActivity

from .adapter import (
    AdapterSignal,
    CommandKind,
    JobStarted,
    CommandResult as AdapterCommandResult,
    Movement,
    NewError,
    Position,
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
DEFAULT_STALL_WINDOW = timedelta(minutes=60)  # spec §7.3, §19


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
    """Append a built Plan and its Runs (end of Schedule); optionally start."""

    now: datetime
    plan: dict[str, Any]
    runs: list[dict[str, Any]]
    start: bool = False


@dataclass(frozen=True)
class StartSchedule:
    """`start` service / button: `idle`/`paused` to `waiting`."""

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


type CoreInput = (
    AddPlan
    | StartSchedule
    | CancelSchedule
    | RetryRun
    | SkipPlan
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


type Effect = StartRunEffect | AbortRunEffect | EmitEvent | ArmTimer | CancelTimer


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
            case StartSchedule():
                self._start_schedule("user_start")
            case CancelSchedule():
                self._cancel()
            case RetryRun():
                self._retry()
            case SkipPlan():
                self._skip_plan()
            case SnapshotReceived():
                self.s["snapshot"] = snapshot_to_dict(event.snapshot)
                self._evaluate()
            case SignalReceived():
                self._signal(event.signal)
            case CommandOutcome():
                self._command_outcome(event)
            case TimerFired():
                self._evaluate()

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

    def _add_plan(self, event: AddPlan) -> None:
        plan = copy.deepcopy(event.plan)
        self.s["plans"][plan["id"]] = plan
        self.s["runs"].extend(copy.deepcopy(event.runs))
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

    def _start_schedule(self, reason: str) -> None:
        if self.status not in (ScheduleStatus.IDLE, ScheduleStatus.PAUSED):
            return
        if next_run(self.s) is None:
            return
        self.s["blocking_error"] = None
        self._set_schedule(ScheduleStatus.WAITING, reason)
        self._evaluate()

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
            return
        if self._deadline_due(DEADLINE_START_WINDOW):
            self._fail_run(run, "did_not_start", abort=True)

    def _evaluate_mowing(self, run: dict[str, Any]) -> None:
        snapshot = self._snapshot()
        if snapshot is None:
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
        self._evaluate_stall(run, snapshot)

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

    def _stable(self, name: str, holds: bool) -> bool:
        """True once `holds` has held for the 60 s stability window (spec §7.7).

        The first holding snapshot arms the window, any non-holding one
        cancels it (flicker), and the window counts only when it is due.
        """
        if not holds:
            self._cancel_deadline(name)
            return False
        if name not in self.s["deadlines"]:
            self._arm(name, self.now + STABILITY_WINDOW)
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
            if entry["run_id"] == event.run_id and entry["result"] is None:
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
        for name in (DEADLINE_START_WINDOW, DEADLINE_DONE_STABLE, DEADLINE_STOPPED_STABLE, DEADLINE_STALL):
            self._cancel_deadline(name)
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
            self._set_schedule(ScheduleStatus.WAITING, "run_done")
            self._evaluate()
        else:
            plan_ids = sorted({r["plan_id"] for r in self.h["runs"][-50:]})
            self._set_schedule(ScheduleStatus.IDLE, "schedule_completed")
            self._emit("schedule_completed", plan_id=run["plan_id"], plan_ids=plan_ids)

    def _fail_run(self, run: dict[str, Any], reason: str, *, abort: bool) -> None:
        """Run `failed`: it stays in `state`; the Schedule pauses for a decision."""
        self._clear_run_deadlines()
        run["finished_at"] = iso(self.now)
        self._set_run(run, RunStatus.FAILED, reason)
        if abort:
            self.effects.append(AbortRunEffect(reason))
        self._set_schedule(ScheduleStatus.PAUSED, reason)

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
