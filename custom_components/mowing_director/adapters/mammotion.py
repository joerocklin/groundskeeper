"""Mammotion adapter: read-only observations (issue #8, spec §3.2, §3.6, §3.8).

Turns one Mammotion Mower's HA state into normalised snapshots, signals,
limits and metadata. Commands land in ticket #10; here they are typed
stubs that refuse.

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

from datetime import datetime
from typing import Any

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_DEVICE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from ..adapter import (
    AdapterSignal,
    AdapterSink,
    Channel,
    CommandResult,
    Limits,
    Movement,
    NativeTask,
    Refused,
    RunSettings,
    SelfCheckReason,
    Snapshot,
    Zone,
)
from ..heights import from_display

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

# Readback granularity of the height sensor, mm (spec §3.6, live-verified).
_HEIGHT_READBACK_MM = 5

_NO_ZONE = ""


def _state_of(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """State string of an entity, or None when it has no state."""
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    return None if state is None else state.state


def _attr(state, key: str) -> Any:
    return state.attributes.get(key) if state is not None else None


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


class MammotionAdapter:
    """One Mower's read-only observations, from fake-or-real HA state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._device_id: str = entry.data[CONF_DEVICE]
        self._name = self._device_name()
        self._sinks: list[AdapterSink] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listening = False
        self._snapshot = self._derive_snapshot()
        self._seen_error_times: set[datetime] = set()
        self._seen_task_states: dict[str, str] = {}
        self._was_mowing = False

    # -- discovery ---------------------------------------------------------

    @callback
    def _device_name(self) -> str:
        """The Mammotion device name behind the configured device_id.

        The device identifier is `("mammotion", name)`; entity unique_ids
        are `{name}_{key}` (research §6).
        """
        device = dr.async_get(self._hass).async_get(self._device_id)
        if device is None:
            raise HomeAssistantError("Mowing Director Mower device not found")
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
        unique_id ends in a long integer (research §5)."""
        reg = er.async_get(self._hass)
        found: dict[str, er.RegistryEntry] = {}
        for entry in er.async_entries_for_device(reg, self._device_id):
            if entry.domain != "switch" or entry.platform != MAMMOTION:
                continue
            suffix = entry.unique_id.removeprefix(f"{self._name}_")
            if not suffix.lstrip("-").isdigit() or len(suffix.lstrip("-")) < 10:
                continue
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
        activity: LawnMowerActivity | None
        try:
            activity = LawnMowerActivity(activity_state) if available else None
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
            async_track_state_change_event(self._hass, None, self._on_state_event)
        )
        self._unsubs.append(
            self._hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_event)
        )
        self._unsubs.append(
            self._hass.bus.async_listen("mammotion_event", self._on_mammotion_event)
        )

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
    def _on_registry_event(self, event: Event) -> None:
        del event

    @callback
    def _on_mammotion_event(self, event: Event) -> None:
        del event

    @callback
    def _check_errors(self) -> None:
        """NewError from the rolling error sensors (works over BLE)."""

    @callback
    def _check_task_areas(self) -> None:
        """ZoneTaskComplete/Aborted from the ephemeral task sensors."""

    @callback
    def _check_job_started(self) -> None:
        """JobStarted on a transition into mowing."""

    @property
    def limits(self) -> Limits:
        """Device limits (spec §3.6)."""
        raise NotImplementedError  # the limits slice fills this in

    # -- commands (ticket #10) ----------------------------------------------

    async def start_run(self, zone: str, settings: RunSettings) -> CommandResult:
        """Not implemented until ticket #10."""
        del zone, settings
        return Refused("not_implemented")

    async def abort_run(self) -> CommandResult:
        """Not implemented until ticket #10."""
        return Refused("not_implemented")

    async def dock(self) -> CommandResult:
        """Not implemented until ticket #10."""
        return Refused("not_implemented")

    async def resume_run(self) -> CommandResult:
        """Not implemented until ticket #10."""
        return Refused("not_implemented")

    async def refresh(self) -> CommandResult:
        """Not implemented until ticket #10."""
        return Refused("not_implemented")

    async def set_task_enabled(self, task: str, enabled: bool) -> CommandResult:
        """Not implemented until ticket #10."""
        del task, enabled
        return Refused("not_implemented")

    # -- catalogues (read-only state; geometry is ticket #9's service) ------

    async def list_zones(self) -> list[Zone]:
        """Zones from the Zone switches: hash, name, no polygon."""
        return [
            Zone(ref, self._switch_name(entry), None)
            for ref, entry in self._zone_switches().items()
        ]

    async def list_channels(self) -> list[Channel]:
        """Channels need `get_geojson`, which starts a 5-minute report
        stream; ticket #10 wires them through the catalogue."""
        return [Channel(Channel.DOCK, Channel.DOCK, Channel.DOCK)]

    async def list_native_tasks(self) -> list[NativeTask]:
        """Native tasks come from `mammotion.get_tasks` (ticket #10)."""
        return [NativeTask("dock", "dock", True)]

    @callback
    def _switch_name(self, entry: er.RegistryEntry) -> str:
        state = self._hass.states.get(entry.entity_id)
        return (
            entry.name
            or entry.original_name
            or (state.attributes.get("friendly_name") if state else None)
            or entry.entity_id
        )

    # -- lifecycle and metadata (spec §3.8, §15) -----------------------------

    async def async_start(self) -> None:
        """Begin observing the Mower."""
        self._ensure_listening()
        self._check_errors()
        self._check_task_areas()
        self._publish()

    async def async_close(self) -> None:
        """Stop observing; never commands the Mower."""
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
        return device.sw_version if device else None

    @property
    def model(self) -> str | None:
        """Device model (`model_id`, e.g. `Luba2AWD5000H`)."""
        device = dr.async_get(self._hass).async_get(self._device_id)
        return device.model_id if device else None

    @property
    def source_version(self) -> str | None:
        """Installed Mammotion-HA version (spec §3.8); None if unknown."""
        return None

    @property
    def redact(self) -> list[str]:
        """Serial-bearing strings (spec §15)."""
        return [self._name, self._name.split("-", 1)[-1]]


def adapter_factory(hass: HomeAssistant, entry: ConfigEntry) -> MammotionAdapter:
    """Registry factory for the `mammotion` domain (spec §3.1)."""
    return MammotionAdapter(hass, entry)
