"""Schedule, Current Run and Next Run sensors (spec §13.5)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .core import ACTIVE_RUN, RunStatus, ScheduleStatus, active_run, next_run
from .runtime import Director, async_get_director


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the three Run and Schedule sensors for one Mower."""
    director = async_get_director(hass, entry.entry_id)
    if director is None:
        return
    async_add_entities(
        [
            ScheduleSensor(director, entry),
            CurrentRunSensor(director, entry),
            NextRunSensor(director, entry),
            PendingPromptsSensor(director, entry),
            WaitingOnSensor(director, entry),
        ]
    )


class _DirectorSensor(SensorEntity):
    """Base: pushes whenever the Director's state changes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    key: str

    def __init__(self, director: Director, entry: ConfigEntry) -> None:
        self._director = director
        self._attr_unique_id = f"{entry.entry_id}_{self.key}"
        self._attr_translation_key = self.key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._director.async_add_listener(self._handle_change)
        )

    @callback
    def _handle_change(self) -> None:
        self.async_write_ha_state()

    @property
    def _state(self) -> dict[str, Any]:
        return self._director.state


def _run_attrs(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "zone": run["zone_name"],
        "plan_id": run["plan_id"],
        "run_id": run["id"],
        "height_mm": run["height_mm"],
        "angle_deg": run["angle_deg"],
        "n": run["index"],
        "m": run["of"],
    }


class ScheduleSensor(_DirectorSensor):
    """Schedule status."""

    key = "schedule"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.value for s in ScheduleStatus]

    @property
    def native_value(self) -> str:
        return self._state["schedule"]["status"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sched = self._state["schedule"]
        remaining = [
            r
            for r in self._state["runs"]
            if r["status"] == RunStatus.PENDING or r["status"] in ACTIVE_RUN
        ]
        return {
            "pause_requested": sched["pause_requested"],
            "paused_reason": (
                sched["reason"] if sched["status"] == ScheduleStatus.PAUSED else None
            ),
            "runs_remaining": len(remaining),
        }


class CurrentRunSensor(_DirectorSensor):
    """The active Run's status, or `none`."""

    key = "current_run"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["none", *[s.value for s in RunStatus]]

    @property
    def native_value(self) -> str:
        run = active_run(self._state)
        return run["status"] if run else "none"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        run = active_run(self._state)
        if run is None:
            return {}
        return {
            **_run_attrs(run),
            "started_at": run.get("started_at"),
            "on_hold_reason": run.get("on_hold_reason"),
        }


class NextRunSensor(_DirectorSensor):
    """Zone name of the head pending Run."""

    key = "next_run"

    @property
    def native_value(self) -> str | None:
        run = next_run(self._state)
        return run["zone_name"] if run else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        run = next_run(self._state)
        if run is None:
            return {}
        return {
            "height_mm": run["height_mm"],
            "angle_deg": run["angle_deg"],
            "plan_id": run["plan_id"],
        }


class PendingPromptsSensor(_DirectorSensor):
    """How many Prompts are waiting for an answer."""

    key = "pending_prompts"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        return len(self._state["prompts"])


class WaitingOnSensor(_DirectorSensor):
    """Diagnostic: how many Checks are failing, and why (spec §9.1, §13.5)."""

    key = "waiting_on"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        return len({w["check"] for w in self._state["waiting_on"]})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        names = {c["id"]: c["name"] for c in self._director.checks}
        return {
            "reasons": [
                {"check": w["check"], "name": names.get(w["check"], w["check"]),
                 "reason": w["reason"]}
                for w in self._state["waiting_on"]
            ]
        }
