"""Mammotion adapter: observations and commands (issues #8/#10, spec §3).

Turns one Mammotion Mower's HA state into normalised snapshots, signals,
limits and metadata, and issues its commands through the closed result
union (spec §3.3): one service call per command, mapped errors, never a
retry (ADR 0002).

Everything is discovered through the device and `unique_id`
(`{device_name}_{key}`, research/mammotion-ha-surface.md §6), never
through entity_ids: the live install carries three entity_id prefixes
(`luba_serial1_*`, `example_mower_*`, `garden_example_mower_*`) for the same
unique_ids. One Mammotion config entry is one *account* and may hold
several devices (Mower + RTK); only the device named by
`entry.data[CONF_DEVICE]` is read.

Entity ids are never cached: Zone switches are re-keyed on a map rebuild
and the per-Zone task sensors are created and removed while a job runs.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_DEVICE,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.util import dt as dt_util

from ..adapter import (
    Accepted,
    AdapterSignal,
    AdapterSink,
    Channel,
    CommandResult,
    ExtrasField,
    JobStarted,
    Limits,
    Movement,
    NativeTask,
    NewError,
    RainStop,
    Refused,
    RunSettings,
    SelfCheckReason,
    Snapshot,
    Unconfirmed,
    Zone,
    ZoneTaskAborted,
    ZoneTaskComplete,
)
from ..heights import from_display
from .mammotion_catalogue import MammotionCatalogue

MAMMOTION = "mammotion"

# Raw `activity_mode` sensor values that mean a job or its breakpoint is
# live (pymammotion MOWING_ACTIVE_MODES, research §8).
_ACTIVE_MODES = frozenset(
    {"MODE_WORKING", "MODE_RETURNING", "MODE_PAUSE", "MODE_CHARGING_PAUSE"}
)
# Mid-job charging pause: `paused` activity plus charging (issue #8 AC).
_RECHARGE_MODES = frozenset({"MODE_PAUSE", "MODE_CHARGING_PAUSE"})

# `work_area` values that are not a Zone name.
_NOT_A_ZONE = frozenset({"path", "Not working", "unknown", ""})

# `self_check` enum -> normalised reason (spec §3.2). The `non_work_hours`
# sensor is never used: two device messages share its field and it flips
# many times a day (research/mammotion-work-hours-and-battery.md §1.2).
_SELF_CHECK = {
    "non_working_hours": SelfCheckReason.DEVICE_NON_WORK_HOURS,
    "low_battery": SelfCheckReason.LOW_BATTERY,
    "rain": SelfCheckReason.RAIN,
}

_INCH_UNITS = frozenset({"in", "inches", '"'})

# Mammotion code for "rain sensor triggered, job ended" (spec §3.2).
_RAIN_STOP_CODE = 1003

# Readback granularity of the height sensor, mm (spec §3.6, live-verified).
_HEIGHT_READBACK_MM = 5

# The app and the device both refuse a start below 15 % (error 2700).
_MIN_START_BATTERY_PCT = 15

# Upstream 500 keys whose outcome is unknown: the command may still have
# been carried out (`command_unconfirmed`, research §2) or a 60 s mode
# wait expired after the command was sent (`*_failed`).
_UNCONFIRMED_KEYS = frozenset(
    {
        "command_unconfirmed",
        "pause_failed",
        "start_failed",
        "resume_failed",
        "dock_failed",
        "dock_cancel_failed",
    }
)
# Upstream 500 keys that are definite nos (research §3).
_REFUSED_KEYS = frozenset(
    {
        "device_not_ready",
        "account_in_use",
        "api_limit_exceeded",
        "device_not_in_account",
        "command_failed",
        "no_job_to_continue",
    }
)


def _int_field(key: str, label: str, minimum: int, maximum: int, default: int) -> ExtrasField:
    """One integer extras setting; the value round-trips as an int, which
    is what `start_mow`'s schema takes."""
    return ExtrasField(
        key=key,
        label=label,
        selector=selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=minimum, max=maximum, step=1, mode=selector.NumberSelectorMode.BOX
            )
        ),
        default=default,
    )


# The adapter-owned Run settings (spec §3.5). Defaults are valid
# `start_mow` inputs: ints for the int fields, and `toward_included_angle`
# only matters when `channel_mode` is the double grid.
_EXTRAS_SCHEMA: tuple[ExtrasField, ...] = (
    _int_field("border_mode", "Border/grid order", 0, 1, 0),
    _int_field("obstacle_laps", "No-go zone laps", 0, 4, 0),
    _int_field("channel_mode", "Cutting path mode", 0, 3, 0),
    _int_field("toward_included_angle", "Double-grid angle", -180, 180, 90),
    ExtrasField(
        # DetectionStrategy values only; a free number would allow invalid codes.
        key="ultra_wave",
        label="Obstacle detection",
        selector=selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value="0", label="Off"),
                    selector.SelectOptionDict(value="1", label="Ultra wave"),
                    selector.SelectOptionDict(value="2", label="Radio wave"),
                    selector.SelectOptionDict(value="10", label="Ultra wave + camera"),
                    selector.SelectOptionDict(value="11", label="Ultra wave + radio"),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        default="1",
    ),
    _int_field("auto_change_direction", "Auto change direction", 0, 1, 0),
    ExtrasField(
        key="ride_boundary_distance",
        label="Ride boundary distance",
        selector=selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0, max=1.0, step=0.1, mode=selector.NumberSelectorMode.SLIDER
            )
        ),
        default=0.0,
    ),
)


def _state_of(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """State string of an entity, or None when it has no state."""
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    return None if state is None else state.state


def _number(value: str | None) -> float | None:
    if value is None or value in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


# `source_version` as a comparable tuple; anything unparseable (a dev
# checkout) degrades to the TypeError belt instead of a guess.
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _fold_toward(bearing_deg: float) -> int:
    """Absolute stripe bearing -> Mammotion `toward` in -90..90 (spec §3.7).

    Mode 1 counts counter-clockwise from map east, so `toward = 90 -
    bearing`; a stripe has no direction, so the value is folded into
    -90..90 (mod 180, then the upper half mirrors negative).
    """
    folded = (90.0 - bearing_deg) % 180.0
    if folded > 90.0:
        folded -= 180.0
    return round(folded)


# `sys_status` modes where `start_mow` plans and starts (upstream
# `async_start_mowing` branch list, research §2). Every other mode -
# LOCK, CHARGING, CHARGING_PAUSE, SYSTEM_ERROR, UPDATING, MANUAL_MOWING,
# SLEEPING, unknown - is a silent no-op returning HTTP 200, so the
# adapter pre-checks and refuses instead.
_ROUTE_MODES = frozenset(
    {"MODE_INITIALIZATION", "MODE_READY", "MODE_WORKING", "MODE_RETURNING", "MODE_PAUSE"}
)

# Extras fallbacks, from the declared schema (the core validates extras
# against it, so a missing key only happens on a bare dict).
_EXTRAS_DEFAULTS: dict[str, Any] = {field.key: field.default for field in _EXTRAS_SCHEMA}


class MammotionAdapter:
    """One Mower's read-only observations, from fake-or-real HA state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._device_id: str = entry.data[CONF_DEVICE]
        self._name = self._device_name()
        self._sinks: list[AdapterSink] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listening = False
        self._catalogue = MammotionCatalogue(hass, self._device_id, self._emit_signal)
        self._snapshot = self._derive_snapshot()
        self._seen_error_times: set[datetime] = set()
        self._error_pair: tuple[str, str] = ("", "")
        self._seen_task_states: dict[str, str] = {}
        self._was_mowing = (
            self._state("lawn_mower", "mower") == str(LawnMowerActivity.MOWING)
        )
        self._source_version: str | None = None

    # -- discovery ---------------------------------------------------------

    @callback
    def _device_name(self) -> str:
        """The Mammotion device name behind the configured device_id.

        The device identifier is `("mammotion", name)`; entity unique_ids
        are `{name}_{key}` (research §6).
        """
        device = dr.async_get(self._hass).async_get(self._device_id)
        if device is None:
            raise HomeAssistantError("Groundskeeper Mower device not found")
        for domain, identifier in device.identifiers:
            if domain == MAMMOTION:
                return str(identifier)
        raise HomeAssistantError("configured device is not a Mammotion Mower")

    @callback
    def _entity_id(self, domain: str, key: str) -> str | None:
        """Resolve one entity through the registry: never an entity_id."""
        return er.async_get(self._hass).async_get_entity_id(
            domain, MAMMOTION, f"{self._name}_{key}"
        )

    @callback
    def _state(self, domain: str, key: str) -> str | None:
        return _state_of(self._hass, self._entity_id(domain, key))

    @callback
    def _zone_switches(self) -> dict[str, er.RegistryEntry]:
        """Zone switches by hash: the device's `mammotion` switches whose
        state carries a `hash` attribute, falling back to a long integer
        unique_id suffix when the switch is unavailable (research §5)."""
        reg = er.async_get(self._hass)
        found: dict[str, er.RegistryEntry] = {}
        for entry in er.async_entries_for_device(reg, self._device_id):
            if entry.domain != "switch" or entry.platform != MAMMOTION:
                continue
            state = self._hass.states.get(entry.entity_id)
            zone_hash = state.attributes.get("hash") if state else None
            if zone_hash is not None:
                found[str(zone_hash)] = entry
                continue
            suffix = entry.unique_id.removeprefix(f"{self._name}_")
            digits = suffix.lstrip("-")
            if digits.isdigit() and len(digits) >= 10:
                found[suffix] = entry
        return found

    # -- snapshot (spec §3.2) ----------------------------------------------

    @property
    def snapshot(self) -> Snapshot:
        """Snapshot re-derived from current state; also pull-at-load (A1)."""
        return self._derive_snapshot()

    def _derive_snapshot(self) -> Snapshot:
        """Re-derive the snapshot from current HA state."""
        activity_state = self._state("lawn_mower", "mower")
        available = activity_state not in (None, STATE_UNAVAILABLE, STATE_UNKNOWN)
        activity: LawnMowerActivity | None = None
        if available and activity_state is not None:
            try:
                activity = LawnMowerActivity(activity_state)
            except ValueError:
                activity = None

        mode = self._state("sensor", "activity_mode")
        charging = self._state("binary_sensor", "charging") == "on"
        recharging = mode in _RECHARGE_MODES and charging
        job_active = self._job_active(mode)

        battery = _int(self._state("sensor", "battery_percent"))
        progress = _int(self._state("sensor", "progress"))
        area = _number(self._state("sensor", "area"))

        rain: bool | None = None
        if self._state("switch", "rain_detection_mower_on_off") == "on":
            detected = self._state("binary_sensor", "rain_detected")
            rain = detected == "on" if detected not in (None, STATE_UNKNOWN) else None

        self_check = _SELF_CHECK.get(self._state("sensor", "self_check") or "")

        return Snapshot(
            activity=activity,
            available=available,
            recharging=recharging,
            job_active=job_active,
            battery=battery,
            progress=progress,
            zone=self._zone_ref(),
            blade_height_mm=self._blade_height_mm(),
            movement=Movement(
                progress_pct=progress,
                area_m2=area,
                position=None,  # hass-mammotion exposes no position entity
            ),
            job_ref=None,  # no stable Mammotion job id on v0.6.18
            rain=rain,
            self_check=self_check,
        )

    @callback
    def _job_active(self, mode: str | None) -> bool:
        """Replicate `is_job_active` from HA-visible state (research §8).

        True when the raw mode is a mowing-active mode, or the mode is
        READY while a per-Zone task sensor still exists: hass-mammotion
        removes those sensors when the job ends, so their presence is the
        HA-visible stand-in for `bp_info > 0` (a breakpoint waiting to
        resume). `progress` is never used - it reads 0 while idle.
        """
        if mode in _ACTIVE_MODES:
            return True
        if mode != "MODE_READY":
            return False
        reg = er.async_get(self._hass)
        return any(
            entry.domain == "sensor"
            and entry.platform == MAMMOTION
            and entry.unique_id.startswith(f"{self._name}_")
            and entry.unique_id.endswith("_task_area")
            for entry in er.async_entries_for_device(reg, self._device_id)
        )

    @callback
    def _zone_ref(self) -> str | None:
        """Current Zone as its native_ref (the Zone hash).

        `work_area` reports the Zone *name*, or `path` / `Not working` /
        `unknown` when the Mower is transiting or idle; those map to None.
        """
        name = self._state("sensor", "work_area")
        if name is None or name in _NOT_A_ZONE:
            return None
        for ref, entry in self._zone_switches().items():
            state = self._hass.states.get(entry.entity_id)
            candidates = {entry.name, entry.original_name}
            if state is not None:
                candidates.add(state.attributes.get("friendly_name"))
            if name in candidates:
                return ref
        return None

    @callback
    def _blade_height_mm(self) -> int | None:
        """Height readback in mm, or None when unavailable/unknown/non-numeric.

        ExampleSite reports the sensor in inches (live inventory: `3.1496 in` =
        80 mm), so the unit attribute decides the conversion. Inch values
        are rounded to the readback granularity (5 mm); mm values are
        exact and only rounded to whole mm.
        """
        entity_id = self._entity_id("sensor", "blade_height")
        state = self._hass.states.get(entity_id) if entity_id else None
        if state is None:
            return None
        value = _number(state.state)
        if value is None:
            return None
        unit = str(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or "").lower()
        if unit in _INCH_UNITS:
            mm = from_display("height", value, "us_customary")
            return int(round(mm / _HEIGHT_READBACK_MM) * _HEIGHT_READBACK_MM)
        return int(round(value))

    # -- observing (decision A1) --------------------------------------------

    def subscribe(self, sink: AdapterSink) -> CALLBACK_TYPE:
        """Push snapshot changes and signals to `sink`; returns unsubscribe."""
        self._sinks.append(sink)
        self._ensure_listening()

        @callback
        def _unsubscribe() -> None:
            if sink in self._sinks:
                self._sinks.remove(sink)

        return _unsubscribe

    @callback
    def _ensure_listening(self) -> None:
        """Install the state/registry/bus watchers once."""
        if self._listening:
            return
        self._listening = True
        self._unsubs.append(
            self._hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state_event)
        )
        self._unsubs.append(
            self._hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_event)
        )
        self._unsubs.append(
            self._hass.bus.async_listen("mammotion_event", self._on_mammotion_event)
        )
        # The catalogue owns Zone-list change detection (ticket #9).
        self._catalogue.async_start()

    @callback
    def _emit_signal(self, signal: AdapterSignal) -> None:
        for sink in list(self._sinks):
            sink.on_signal(signal)

    @callback
    def _publish(self) -> None:
        snapshot = self._derive_snapshot()
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        for sink in list(self._sinks):
            sink.on_snapshot(snapshot)

    @callback
    def _on_state_event(self, event: Event[EventStateChangedData]) -> None:
        del event
        self._check_errors()
        self._check_task_areas()
        self._check_job_started()
        self._publish()

    @callback
    def _on_registry_event(self, event: Event[Any]) -> None:
        del event
        # The ephemeral task sensors are created and removed as a job
        # runs, so discovery also runs on registry changes. Zone-list
        # changes are the catalogue's own detection.
        self._check_task_areas()

    @callback
    def _on_mammotion_event(self, event: Event[Any]) -> None:
        """rain_stop from `mammotion_event` (research §7). Over BLE no
        event arrives; the error-sensor path covers errors there."""
        data = event.data
        if data.get("device_id") != self._device_id:
            return
        codes = data.get("codes") or []
        if any(int(code.get("code", 0)) == _RAIN_STOP_CODE for code in codes):
            self._emit_signal(RainStop())

    @callback
    def _check_errors(self) -> None:
        """NewError from the rolling error sensors (works over BLE).

        The error log is rolling ten-slot history that never empties, so
        a non-zero code alone is not "new": a new fault is a change of
        the observed (code, time) pair with code != 0 (research §4.2).
        Deduped by time. While `error_1_time` is still `unknown` the
        fault is held: emitting it with a made-up time would double-count
        the same fault when the firmware stamps the real one.
        """
        code = self._state("sensor", "error_1_code") or ""
        raw_time = self._state("sensor", "error_1_time") or ""
        pair = (code, raw_time)
        if pair == self._error_pair:
            return
        self._error_pair = pair
        if not code or code == "0":
            return
        stamp = dt_util.parse_datetime(raw_time)
        if stamp is None or stamp in self._seen_error_times:
            return
        self._seen_error_times.add(stamp)
        message = self._state("sensor", "error_1_message") or ""
        self._emit_signal(NewError(code=code, message=message, time=stamp))

    @callback
    def _check_task_areas(self) -> None:
        """ZoneTaskComplete/Aborted from the ephemeral `{hash}_task_area`
        sensors. They are removed when the job ends, so discovery runs on
        every registry change too; entity ids are never cached."""
        reg = er.async_get(self._hass)
        live: set[str] = set()
        for entry in er.async_entries_for_device(reg, self._device_id):
            if (
                entry.domain != "sensor"
                or entry.platform != MAMMOTION
                or not entry.unique_id.startswith(f"{self._name}_")
                or not entry.unique_id.endswith("_task_area")
            ):
                continue
            zone = entry.unique_id[len(self._name) + 1 : -len("_task_area")]
            live.add(zone)
            state = _state_of(self._hass, entry.entity_id)
            previous = self._seen_task_states.get(zone)
            self._seen_task_states[zone] = state or ""
            if state == previous:
                continue
            if state == "COMPLETE":
                self._emit_signal(ZoneTaskComplete(zone))
            elif state == "ABORTED":
                self._emit_signal(ZoneTaskAborted(zone))
        # A re-created sensor for a finished job must report its new
        # states, not be suppressed by the previous job's memory.
        for zone in self._seen_task_states.keys() - live:
            del self._seen_task_states[zone]

    @callback
    def _check_job_started(self) -> None:
        """JobStarted when activity transitions into `mowing`. The core
        decides externality from its own command log (ticket 06 §5a)."""
        mowing = self._state("lawn_mower", "mower") == str(LawnMowerActivity.MOWING)
        if mowing and not self._was_mowing:
            self._emit_signal(
                JobStarted(job_ref=None, zone=self._zone_ref(), started_at=dt_util.utcnow())
            )
        self._was_mowing = mowing

    @property
    def limits(self) -> Limits:
        """Device limits, read from the number entities (spec §3.6).

        The `number.*` attributes carry the device limits (the H variant
        reports 55-100 mm step 1); when an entity is missing the
        documented service range stands in, never a per-Mower constant.
        """
        height = self._number_attrs("blade_height")
        spacing = self._number_attrs("path_spacing")
        speed = self._number_attrs("working_speed")
        return Limits(
            height_min_mm=int(height.get("min_value", 15)),
            height_max_mm=int(height.get("max_value", 100)),
            height_step_mm=int(height.get("step", 1)),
            has_height_readback=True,  # the blade_height sensor is the readback
            height_readback_resolution_mm=_HEIGHT_READBACK_MM,
            spacing_min_cm=float(spacing.get("min_value", 20)),
            spacing_max_cm=float(spacing.get("max_value", 35)),
            speed_min_m_s=float(speed.get("min_value", 0.2)),
            speed_max_m_s=float(speed.get("max_value", 0.6)),
            extras_schema=_EXTRAS_SCHEMA,
            min_start_battery_pct=_MIN_START_BATTERY_PCT,
            recharge_battery_pct=self._level("recharge_level", "smart_recharge_level"),
            resume_battery_pct=self._level("resume_level", "smart_resume_level"),
        )

    @callback
    def _number_attrs(self, key: str) -> dict[str, Any]:
        entity_id = self._entity_id("number", key)
        state = self._hass.states.get(entity_id) if entity_id else None
        return dict(state.attributes) if state is not None else {}

    @callback
    def _level(self, number_key: str, smart_key: str) -> int | None:
        """Configured battery level; None when the smart switch is on.

        The smart switch is authoritative: while on, the number entity
        keeps a stale 30/100 (research/mammotion-work-hours-and-battery.md §2.2).
        """
        if self._state("switch", smart_key) == "on":
            return None
        return _int(self._state("number", number_key))

    # -- commands (ticket #10) ----------------------------------------------

    async def _call(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> CommandResult:
        """One service call, mapped to the closed command union (spec §3.3).

        Every command path goes through here exactly once: the adapter
        never re-issues (ADR 0002), so the mapping is the only retry
        question that ever gets asked, and the answer is always no.
        Catch order matters - `ServiceNotFound` and
        `ServiceNotSupported` both subclass `ServiceValidationError`.
        """
        try:
            await self._hass.services.async_call(
                domain, service, data, blocking=True
            )
        except ServiceNotFound:
            # The Mammotion entry has not loaded its services yet.
            return Refused("mammotion_not_ready")
        except ServiceValidationError as err:
            # 400: a definite no (also covers ServiceNotSupported).
            return Refused(str(err))
        except HomeAssistantError as err:
            key = err.translation_key
            if key in _UNCONFIRMED_KEYS:
                # 500 command_unconfirmed or an expired 60 s mode wait:
                # the command may still have been carried out.
                return Unconfirmed()
            if key in _REFUSED_KEYS:
                return Refused(str(err) or key)
            # Unknown 500: outcome unknown, never retry (ADR 0002).
            return Unconfirmed()
        return Accepted()

    async def start_run(self, zone: str, settings: RunSettings) -> CommandResult:
        """One Zone via `mammotion.start_mow`, every route field explicit.

        Omitted fields inherit the HA-side number/select entity values,
        so none are omitted (spec §3.4); `modify` is never sent. Upstream
        silently no-ops (HTTP 200) outside the route modes, so the mode
        is pre-checked and refused locally (issue #10 AC).
        """
        mode = self._state("sensor", "activity_mode")
        if mode not in _ROUTE_MODES:
            return Refused(f"start_mow silently no-ops in mode {mode!r}")

        switches = self._zone_switches()
        entry = switches.get(zone)
        if entry is None:
            return Refused(f"unknown zone {zone}")
        state = self._hass.states.get(entry.entity_id)
        if state is None or "hash" not in state.attributes:
            # Upstream drops area entities without a `hash` attribute and
            # falls back to the ON switches: a wrong-zone hazard
            # (mammotion-ha-surface §5). Refuse instead.
            return Refused(f"zone {zone} switch carries no hash attribute")

        entity_id = self._entity_id("lawn_mower", "mower")
        if entity_id is None:
            return Refused("mammotion_not_ready")

        extras = settings.extras
        return await self._call(
            MAMMOTION,
            "start_mow",
            {
                "entity_id": [entity_id],
                "areas": [entry.entity_id],
                "blade_height": int(settings.blade_height_mm),
                "toward_mode": 1,
                "toward": _fold_toward(settings.bearing_deg),
                "channel_width": int(settings.spacing_cm),
                "speed": float(settings.speed_m_s),
                "mowing_laps": int(settings.perimeter_laps),
                # Explicit 0: a stale number entity must not resume
                # mid-Zone.
                "start_progress": 0,
                # Always sent: pymammotion zeroes toward_included_angle
                # unless channel_mode == 1, so a stale value is harmless.
                "toward_included_angle": int(
                    extras.get("toward_included_angle", _EXTRAS_DEFAULTS["toward_included_angle"])
                ),
                # The select's value is a string ("1"); the schema wants
                # an int.
                "ultra_wave": int(extras.get("ultra_wave", _EXTRAS_DEFAULTS["ultra_wave"])),
                "border_mode": int(extras.get("border_mode", _EXTRAS_DEFAULTS["border_mode"])),
                "obstacle_laps": int(
                    extras.get("obstacle_laps", _EXTRAS_DEFAULTS["obstacle_laps"])
                ),
                "channel_mode": int(
                    extras.get("channel_mode", _EXTRAS_DEFAULTS["channel_mode"])
                ),
                "auto_change_direction": int(
                    extras.get("auto_change_direction", _EXTRAS_DEFAULTS["auto_change_direction"])
                ),
                "ride_boundary_distance": float(
                    extras.get(
                        "ride_boundary_distance", _EXTRAS_DEFAULTS["ride_boundary_distance"]
                    )
                ),
            },
        )

    async def abort_run(self) -> CommandResult:
        """`lawn_mower.stop`, then `lawn_mower.dock` (spec §3.4).

        A stop that is not cleanly accepted leaves the Mower's state
        unknown or unchanged: return that result WITHOUT docking - a
        dock could fight a command whose outcome is unknown (research
        §3). One call each, never a retry (ADR 0002).
        """
        stopped = await self._lawn_mower_call("stop")
        if not isinstance(stopped, Accepted):
            return stopped
        return await self._lawn_mower_call("dock")

    async def dock(self) -> CommandResult:
        """`lawn_mower.dock`: return to the dock (spec §3.4)."""
        return await self._lawn_mower_call("dock")

    async def _lawn_mower_call(self, service: str) -> CommandResult:
        entity_id = self._entity_id("lawn_mower", "mower")
        if entity_id is None:
            return Refused("mammotion_not_ready")
        return await self._call("lawn_mower", service, {"entity_id": [entity_id]})

    async def resume_run(self) -> CommandResult:
        """Resume a held job, choosing a path that works on the installed
        Mammotion-HA (spec §3.4, research §2).

        Pre-check: only PAUSE, or READY while a breakpoint is live
        (`job_active`), may resume. Plain `start_mowing` when NOT
        mid-job plans a fresh job from the ON switches, and CHARGING_PAUSE
        is a recharge the core owns, so both are refused with zero calls.

        v0.6.19+ upstream bug: the READY-breakpoint branch of
        `async_start_mowing` calls
        `async_send_command("start_job", "todev_taskctrl_ack", priority=...)`
        and the second positional binds to `priority` -> TypeError,
        raised AFTER the route query was sent. On >= 0.6.19 press
        `continue_last_job` instead; on older (or unknown) versions try
        `start_mowing` and fall back to the button on TypeError. The
        fallback never re-calls `start_mowing` (ADR 0002).
        """
        mode = self._state("sensor", "activity_mode")
        if mode == "MODE_CHARGING_PAUSE":
            return Refused("recharging")
        if mode == "MODE_READY":
            if not self._job_active(mode):
                return Refused("no job to resume")
        elif mode != "MODE_PAUSE":
            return Refused(f"nothing to resume in mode {mode!r}")

        if self._resume_via_button():
            button = self._entity_id("button", "continue_last_job")
            if button is None:
                return Refused("mammotion_not_ready")
            return await self._call("button", "press", {"entity_id": [button]})

        entity_id = self._entity_id("lawn_mower", "mower")
        if entity_id is None:
            return Refused("mammotion_not_ready")
        try:
            result = await self._call(
                "lawn_mower", "start_mowing", {"entity_id": [entity_id]}
            )
        except TypeError:
            # The v0.6.19+ positional-collision bug fired after the
            # route query was sent: fall back to the button, never
            # re-call start_mowing.
            button = self._entity_id("button", "continue_last_job")
            if button is None:
                # A command was already sent and its outcome is unknown.
                return Unconfirmed()
            return await self._call("button", "press", {"entity_id": [button]})
        return result

    @callback
    def _resume_via_button(self) -> bool:
        """True when the installed Mammotion-HA has the broken plain
        resume path (>= 0.6.19); unknown versions degrade to trying it
        with the TypeError belt."""
        match = _VERSION.match(self._source_version or "")
        if match is None:
            return False
        return tuple(int(part) for part in match.groups()) >= (0, 6, 19)

    async def refresh(self) -> CommandResult:
        """Press `button.{name}_refresh_status`: fresh telemetry at
        Priority.USER (spec §3.4, decision A8)."""
        entity_id = self._entity_id("button", "refresh_status")
        if entity_id is None:
            return Refused("mammotion_not_ready")
        return await self._call("button", "press", {"entity_id": [entity_id]})

    # -- catalogues (delegated to the ticket #9 catalogue) -------------------

    async def list_zones(self) -> list[Zone]:
        """Zones as (native_ref, name, polygon or None)."""
        return await self._catalogue.list_zones()

    async def list_channels(self) -> list[Channel]:
        """Channels from `get_geojson` path features (0 = dock)."""
        return await self._catalogue.list_channels()

    async def list_native_tasks(self) -> list[NativeTask]:
        """The Mower's own scheduler, read-only."""
        return await self._catalogue.list_native_tasks()

    async def set_task_enabled(self, task: str, enabled: bool) -> CommandResult:
        """Enable/disable one native task (Repairs fix flow)."""
        return await self._catalogue.set_task_enabled(task, enabled)

    # -- lifecycle and metadata (spec §3.8, §15) -----------------------------

    async def async_start(self) -> None:
        """Begin observing the Mower."""
        self._ensure_listening()
        try:
            integration = await async_get_integration(self._hass, MAMMOTION)
        except IntegrationNotFound:
            self._source_version = None
        else:
            self._source_version = str(integration.version) if integration.version else None
        self._check_errors()
        self._check_task_areas()
        self._publish()

    async def async_close(self) -> None:
        """Stop observing; never commands the Mower."""
        self._catalogue.async_stop()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._listening = False

    @property
    def source_domain(self) -> str:
        """Registry key (spec §3.1)."""
        return MAMMOTION

    @property
    def firmware_version(self) -> str | None:
        """Device firmware from the device registry (`sw_version`)."""
        device = dr.async_get(self._hass).async_get(self._device_id)
        return getattr(device, "sw_version", None)

    @property
    def model(self) -> str | None:
        """Device model (`model_id`, e.g. `Luba2AWD5000H`)."""
        device = dr.async_get(self._hass).async_get(self._device_id)
        return getattr(device, "model_id", None)

    @property
    def source_version(self) -> str | None:
        """Installed Mammotion-HA version (spec §3.8); None if unknown.

        Resolved once in `async_start` - the protocol member is a
        property, so the lookup cannot be awaited on access.
        """
        return self._source_version

    @property
    def redact(self) -> list[str]:
        """Serial-bearing strings (spec §15)."""
        return [self._name, self._name.split("-", 1)[-1]]


def adapter_factory(hass: HomeAssistant, entry: ConfigEntry) -> MammotionAdapter:
    """Registry factory for the `mammotion` domain (spec §3.1)."""
    return MammotionAdapter(hass, entry)
