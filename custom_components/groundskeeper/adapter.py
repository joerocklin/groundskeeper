"""Mower adapter seam (spec §3).

The brand-neutral protocol the Groundskeeper core is written against.
One adapter instance per Mower config entry; adapters come from the
registry in `adapters/` keyed by source integration domain (spec §3.1).
The adapter only normalises observations - the core owns every outcome
rule and attributes the cause of every pause.

The core never sees brand service names, entity keys or Zone hashes:
Zones, Channels and native tasks travel as neutral `native_ref` strings
chosen by the adapter.
"""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers import selector


# ---------------------------------------------------------------------------
# Command results (spec §3.3, ADR 0002)
# ---------------------------------------------------------------------------


class CommandKind(StrEnum):
    """The closed set of command outcomes (spec §3.3)."""

    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class Accepted:
    """The Mower took the command."""

    kind: ClassVar[CommandKind] = CommandKind.ACCEPTED


@dataclass(frozen=True, slots=True)
class Refused:
    """A definite no: a 400, or a mode the adapter pre-checks (spec §3.3)."""

    reason: str
    kind: ClassVar[CommandKind] = CommandKind.REFUSED


@dataclass(frozen=True, slots=True)
class Unconfirmed:
    """Outcome unknown: a 500 `command_unconfirmed` or a mode-wait timeout.

    The core verifies through snapshots and never re-issues (ADR 0002).
    """

    kind: ClassVar[CommandKind] = CommandKind.UNCONFIRMED


type CommandResult = Accepted | Refused | Unconfirmed


# ---------------------------------------------------------------------------
# Snapshot (spec §3.2) — exactly these twelve fields
# ---------------------------------------------------------------------------


class SelfCheckReason(StrEnum):
    """Normalised self-check reasons the Mower gives before refusing work."""

    DEVICE_NON_WORK_HOURS = "device_non_work_hours"
    LOW_BATTERY = "low_battery"
    RAIN = "rain"


@dataclass(frozen=True, slots=True)
class Position:
    """Where the Mower is, when the source exposes it."""

    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Movement:
    """Generic progress indicators the core's stall rule reads (spec §3.2)."""

    progress_pct: int | None = None
    area_m2: float | None = None
    position: Position | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One normalised observation of the Mower (spec §3.2).

    Exactly the twelve spec fields; `activity` None means unknown. The
    adapter computes `job_active` and `recharging` from its source's
    modes - the core never sees them.
    """

    activity: LawnMowerActivity | None
    available: bool
    recharging: bool
    job_active: bool
    battery: int | None
    progress: int | None
    zone: str | None
    blade_height_mm: int | None
    movement: Movement
    job_ref: str | None
    rain: bool | None = None
    self_check: SelfCheckReason | None = None


# ---------------------------------------------------------------------------
# Signals (spec §3.2) — act immediately, no stability window
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewError:
    """A new Mower error, passed through as (code, message, time).

    The core dedupes by `time`; the adapter emits an error whenever it
    first observes it, including after a restart.
    """

    code: str
    message: str
    time: datetime


@dataclass(frozen=True, slots=True)
class ZoneTaskComplete:
    """The Mower's own per-Zone task reported COMPLETE."""

    zone: str


@dataclass(frozen=True, slots=True)
class ZoneTaskAborted:
    """The Mower's own per-Zone task reported ABORTED."""

    zone: str


@dataclass(frozen=True, slots=True)
class JobStarted:
    """A job started on the Mower.

    Neutral: the core decides whether the start was external by its own
    command log (design ticket 06 §5a).
    """

    job_ref: str | None
    zone: str | None
    started_at: datetime


@dataclass(frozen=True, slots=True)
class RainStop:
    """The job ended by the Mower for rain (Mammotion code 1003)."""


@dataclass(frozen=True, slots=True)
class ZonesChanged:
    """The Zone list changed; the core should re-run `list_zones` (issue #9)."""


type AdapterSignal = (
    NewError
    | ZoneTaskComplete
    | ZoneTaskAborted
    | JobStarted
    | RainStop
    | ZonesChanged
)


class AdapterSink(Protocol):
    """The core's per-entry receiver for adapter pushes (decision A1)."""

    @callback
    def on_snapshot(self, snapshot: Snapshot) -> None:
        """A normalised snapshot changed."""

    @callback
    def on_signal(self, signal: AdapterSignal) -> None:
        """A signal arrived; act immediately."""


# ---------------------------------------------------------------------------
# Catalogues (spec §3.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Zone:
    """A mowable Zone as the source names it.

    `polygon` is a GeoJSON geometry (WGS84) or None until fetched -
    geometry never blocks setup (issue #11).
    """

    native_ref: str
    name: str
    polygon: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Channel:
    """A route between two ends; an end is a Zone native_ref or DOCK."""

    DOCK: ClassVar[str] = "dock"

    native_ref: str
    end_a: str
    end_b: str
    # GeoJSON LineString (WGS84) or None until fetched - like Zone.polygon,
    # geometry never blocks setup (issue #11).
    path: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class NativeTask:
    """One read-only task from the Mower's own scheduler."""

    task_id: str
    name: str
    enabled: bool


# ---------------------------------------------------------------------------
# Run settings (spec §3.5, §3.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSettings:
    """Portable Run settings in SI, plus adapter `extras`.

    `bearing_deg` is the ABSOLUTE stripe compass bearing (Zone bearing +
    relative path angle); the adapter converts it to its native frame
    (spec §3.7). `extras` keys are validated against the adapter's
    declared schema (`Limits.extras_schema`).

    Not `slots=True`: the `extras` dict field would make the generated
    `__hash__` fail; identity, not equality, is what adapters need.
    """

    blade_height_mm: int
    bearing_deg: float
    spacing_cm: float
    speed_m_s: float
    perimeter_laps: int
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Limits descriptor (spec §3.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtrasField:
    """One adapter-owned Run setting, panel-renderable (decision A7).

    `selector` is an HA selector the panel renders generically; the
    panel serialises it with `Selector.serialize()`.
    """

    key: str
    label: str
    selector: selector.Selector
    default: Any


@dataclass(frozen=True, slots=True)
class Limits:
    """Device limits, read from the device, never hard-coded (spec §3.6).

    `recharge_battery_pct`/`resume_battery_pct` are None when the Mower
    is set to "smart".
    """

    height_min_mm: int
    height_max_mm: int
    height_step_mm: int
    has_height_readback: bool
    height_readback_resolution_mm: int | None
    spacing_min_cm: float
    spacing_max_cm: float
    speed_min_m_s: float
    speed_max_m_s: float
    extras_schema: tuple[ExtrasField, ...]
    min_start_battery_pct: int
    recharge_battery_pct: int | None
    resume_battery_pct: int | None


# ---------------------------------------------------------------------------
# The protocol (spec §3.1)
# ---------------------------------------------------------------------------


@runtime_checkable
class MowerAdapter(Protocol):
    """Brand-neutral seam to one Mower; one instance per config entry.

    `runtime_checkable` so the contract suite can assert the full member
    surface exists; typing alone still drives normal use.
    """

    # -- observations (spec §3.2, decision A1) ------------------------------

    @property
    def snapshot(self) -> Snapshot:
        """Latest normalised snapshot, for pull-at-load."""
        ...

    @property
    def limits(self) -> Limits:
        """Device limits (spec §3.6)."""
        ...

    def subscribe(self, sink: AdapterSink) -> CALLBACK_TYPE:
        """Push snapshot changes and signals to `sink`; returns unsubscribe."""
        ...

    # -- commands (spec §3.3, §3.4) -----------------------------------------
    # Every command returns a CommandResult and never raises for a Mower
    # failure; on `unconfirmed` the core verifies by state, never re-issues.

    async def start_run(self, zone: str, settings: RunSettings) -> CommandResult:
        """Start exactly one Zone with every route field explicit (§3.4)."""
        ...

    async def abort_run(self) -> CommandResult:
        """Cancel without docking, then dock (§3.4)."""
        ...

    async def dock(self) -> CommandResult:
        """Return to the dock (§3.4)."""
        ...

    async def resume_run(self) -> CommandResult:
        """Resume a held job; the adapter picks a path for its version (§3.4)."""
        ...

    async def refresh(self) -> CommandResult:
        """Request fresh telemetry (§3.4, decision A8)."""
        ...

    # -- catalogues (spec §3.4) ---------------------------------------------

    async def list_zones(self) -> list[Zone]:
        """Zones as (native_ref, name, polygon or None)."""
        ...

    async def list_channels(self) -> list[Channel]:
        """Channels as (native_ref, end_a, end_b); an end is a Zone or DOCK."""
        ...

    async def list_native_tasks(self) -> list[NativeTask]:
        """The Mower's own scheduler, read-only."""
        ...

    async def set_task_enabled(self, task: str, enabled: bool) -> CommandResult:
        """Enable/disable one native task (Repairs fix only)."""
        ...

    # -- lifecycle and metadata (spec §15, §3.8) ----------------------------

    async def async_start(self) -> None:
        """Begin observing the Mower (subscriptions, polling)."""
        ...

    async def async_close(self) -> None:
        """Stop observing; never commands the Mower."""
        ...

    @property
    def source_domain(self) -> str:
        """Source integration domain, the registry key (§3.1)."""
        ...

    @property
    def firmware_version(self) -> str | None:
        """Mower firmware, for debug meta and Repairs."""
        ...

    @property
    def model(self) -> str | None:
        """Mower model, for debug meta."""
        ...

    @property
    def source_version(self) -> str | None:
        """Source integration version (spec §3.8 version support)."""
        ...

    @property
    def redact(self) -> list[str]:
        """Strings to redact from raw_state records (serials, spec §15)."""
        ...
