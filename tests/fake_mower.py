"""FakeMower: a scriptable in-process MowerAdapter for tests (spec §18.2).

Pure Python, no hass, so layer-1 pure reducer tests can drive the core
against it. Scriptable: snapshot sequences, signal and error injection,
per-command results (including `unconfirmed`), and DELAYED effects -
snapshots queued by a command and applied later by `apply_effects()`,
which is what unconfirmed-verify tests need. Every command is recorded
in `commands` as (name, args).

No clock control lives here: all windows are core-side persisted
deadlines (spec §19); HA tests drive them with phacc
`async_fire_time_changed`. FakeMower only emits on demand.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers import selector

from custom_components.groundskeeper.adapter import (
    Accepted,
    AdapterSignal,
    AdapterSink,
    Channel,
    CommandResult,
    ExtrasField,
    Limits,
    Movement,
    MowerAdapter,
    NativeTask,
    NewError,
    Refused,
    RunSettings,
    Snapshot,
    Zone,
)

DEFAULT_ZONES: tuple[Zone, ...] = (
    Zone(
        native_ref="11111111",
        name="Front Lawn",
        polygon={
            "type": "Polygon",
            "coordinates": [
                [[13.4050, 52.5200], [13.4060, 52.5200],
                 [13.4060, 52.5208], [13.4050, 52.5208],
                 [13.4050, 52.5200]]
            ],
        },
    ),
    Zone(native_ref="22222222", name="Backyard", polygon=None),
)

DEFAULT_CHANNELS: tuple[Channel, ...] = (
    Channel(native_ref="p-1", end_a="11111111", end_b=Channel.DOCK),
    Channel(native_ref="p-2", end_a="11111111", end_b="22222222"),
)

DEFAULT_TASKS: tuple[NativeTask, ...] = (
    NativeTask(task_id="t-1", name="Weekday mow", enabled=True),
)

DEFAULT_LIMITS = Limits(
    height_min_mm=55,
    height_max_mm=100,
    height_step_mm=1,
    has_height_readback=True,
    height_readback_resolution_mm=5,
    spacing_min_cm=20.0,
    spacing_max_cm=60.0,
    speed_min_m_s=0.3,
    speed_max_m_s=1.2,
    extras_schema=(
        ExtrasField(
            key="border_grid_order",
            label="Border/grid order",
            selector=selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="border", label="Border first"),
                        selector.SelectOptionDict(value="grid", label="Grid first"),
                    ]
                )
            ),
            default="border",
        ),
        ExtrasField(
            key="ultra_wave",
            label="Ultra wave",
            selector=selector.BooleanSelector(),
            default=False,
        ),
    ),
    min_start_battery_pct=15,
    recharge_battery_pct=None,
    resume_battery_pct=None,
)

DEFAULT_SNAPSHOT = Snapshot(
    activity=LawnMowerActivity.DOCKED,
    available=True,
    recharging=False,
    job_active=False,
    battery=80,
    progress=0,
    zone=None,
    blade_height_mm=70,
    movement=Movement(progress_pct=0),
    job_ref=None,
    rain=None,
    self_check=None,
)


@dataclass
class FakeMower:
    """A fully scriptable MowerAdapter (see module docstring)."""

    zones: tuple[Zone, ...] = DEFAULT_ZONES
    channels: tuple[Channel, ...] = DEFAULT_CHANNELS
    native_tasks: tuple[NativeTask, ...] = DEFAULT_TASKS
    limits_descriptor: Limits = DEFAULT_LIMITS
    source_domain_name: str = "fake_mower"
    firmware: str | None = "1.2.3"
    model_name: str | None = "Fake 9000"
    source_ver: str | None = "0.6.18"
    redact_terms: list[str] = field(default_factory=lambda: ["SN-FAKE-1"])

    # What the core saw, in order: (command name, args tuple).
    commands: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    _snapshot: Snapshot = DEFAULT_SNAPSHOT
    _sinks: list[AdapterSink] = field(default_factory=list)
    _results: dict[str, deque[CommandResult]] = field(default_factory=dict)
    _effects: dict[str, deque[Snapshot]] = field(default_factory=dict)
    _pending_effects: list[Snapshot] = field(default_factory=list)
    _queued_snapshots: deque[Snapshot] = field(default_factory=deque)
    _error_times: set[datetime] = field(default_factory=set)
    _task_flags: dict[str, bool] = field(default_factory=dict)

    # -- scripting ----------------------------------------------------------

    def script_result(self, command: str, result: CommandResult) -> None:
        """Queue one result for the next call of `command`."""
        self._results.setdefault(command, deque()).append(result)

    def script_effect(self, command: str, snapshot: Snapshot) -> None:
        """Queue a snapshot the command leaves pending (delayed effect).

        `apply_effects()` publishes it - so an `unconfirmed` command can
        return before the state change is visible.
        """
        self._effects.setdefault(command, deque()).append(snapshot)

    def script_snapshot(self, snapshot: Snapshot) -> None:
        """Queue a snapshot; `push_next_snapshot()` publishes it."""
        self._queued_snapshots.append(snapshot)

    def push_next_snapshot(self) -> bool:
        """Publish the next queued snapshot; False when the queue is empty."""
        if not self._queued_snapshots:
            return False
        self.emit_snapshot(self._queued_snapshots.popleft())
        return True

    def apply_effects(self) -> int:
        """Publish snapshots left pending by commands; returns how many."""
        count = len(self._pending_effects)
        for snapshot in self._pending_effects:
            self.emit_snapshot(snapshot)
        self._pending_effects.clear()
        return count

    def emit_snapshot(self, snapshot: Snapshot) -> None:
        """Publish a snapshot to `snapshot` and every sink."""
        self._snapshot = snapshot
        for sink in list(self._sinks):
            sink.on_snapshot(snapshot)

    def emit_signal(self, signal: AdapterSignal) -> None:
        """Push a signal to every sink; NewError deduped by time (§3.2)."""
        if isinstance(signal, NewError):
            if signal.time in self._error_times:
                return
            self._error_times.add(signal.time)
        for sink in list(self._sinks):
            sink.on_signal(signal)

    # -- MowerAdapter observations -------------------------------------------

    @property
    def snapshot(self) -> Snapshot:
        """Latest published snapshot."""
        return self._snapshot

    @property
    def limits(self) -> Limits:
        """The limits descriptor given at construction."""
        return self.limits_descriptor

    def subscribe(self, sink: AdapterSink) -> CALLBACK_TYPE:
        """Register a sink; returns unsubscribe."""
        self._sinks.append(sink)

        @callback
        def _unsubscribe() -> None:
            if sink in self._sinks:
                self._sinks.remove(sink)

        return _unsubscribe

    # -- MowerAdapter commands ------------------------------------------------

    async def start_run(self, zone: str, settings: RunSettings) -> CommandResult:
        """Record and return the scripted result."""
        return await self._run("start_run", (zone, settings))

    async def abort_run(self) -> CommandResult:
        """Record and return the scripted result."""
        return await self._run("abort_run", ())

    async def dock(self) -> CommandResult:
        """Record and return the scripted result."""
        return await self._run("dock", ())

    async def resume_run(self) -> CommandResult:
        """Record and return the scripted result."""
        return await self._run("resume_run", ())

    async def refresh(self) -> CommandResult:
        """Record and return the scripted result."""
        return await self._run("refresh", ())

    async def set_task_enabled(self, task: str, enabled: bool) -> CommandResult:
        """Record, apply the flag, and return the scripted result."""
        result = await self._run("set_task_enabled", (task, enabled))
        if isinstance(result, Accepted):
            self._task_flags[task] = enabled
        return result

    async def _run(self, name: str, args: tuple[Any, ...]) -> CommandResult:
        """Shared command body: record, pop scripted result, queue effect."""
        self.commands.append((name, args))
        queue = self._results.get(name)
        result = queue.popleft() if queue else Accepted()
        effects = self._effects.get(name)
        if effects:
            self._pending_effects.append(effects.popleft())
        return result

    # -- MowerAdapter catalogues -----------------------------------------------

    async def list_zones(self) -> list[Zone]:
        """The scripted zones."""
        return list(self.zones)

    async def list_channels(self) -> list[Channel]:
        """The scripted channels."""
        return list(self.channels)

    async def list_native_tasks(self) -> list[NativeTask]:
        """The scripted tasks, with `set_task_enabled` effects applied."""
        return [
            NativeTask(
                task_id=t.task_id,
                name=t.name,
                enabled=self._task_flags.get(t.task_id, t.enabled),
            )
            for t in self.native_tasks
        ]

    # -- MowerAdapter lifecycle and metadata ------------------------------------

    async def async_start(self) -> None:
        """No-op: FakeMower only emits on demand."""

    async def async_close(self) -> None:
        """No-op: FakeMower holds no subscriptions."""

    @property
    def source_domain(self) -> str:
        """Registry key for this fake."""
        return self.source_domain_name

    @property
    def firmware_version(self) -> str | None:
        """Scripted firmware version."""
        return self.firmware

    @property
    def model(self) -> str | None:
        """Scripted model name."""
        return self.model_name

    @property
    def source_version(self) -> str | None:
        """Scripted source-integration version."""
        return self.source_ver

    @property
    def redact(self) -> list[str]:
        """Scripted redaction terms."""
        return list(self.redact_terms)


def make_sink_recorder() -> tuple[AdapterSink, list[Any]]:
    """A sink plus its recording list, for one-off test use."""
    received: list[Any] = []

    class _Recorder:
        @callback
        def on_snapshot(self, snapshot: Snapshot) -> None:
            received.append(("snapshot", snapshot))

        @callback
        def on_signal(self, signal: AdapterSignal) -> None:
            received.append(("signal", signal))

    return _Recorder(), received


def fake_mower_factory(hass: Any, entry: Any) -> MowerAdapter:
    """Registry-shaped factory returning a FakeMower (for injection tests)."""
    return FakeMower()
