"""One `binary_sensor` per Check, created and removed with it (spec §13.5)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .runtime import Director, async_get_director


def _unique_id(entry: ConfigEntry, check_id: str) -> str:
    return f"{entry.entry_id}_check_{check_id}"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Track the Check list: add an entity for each new Check, drop deleted ones."""
    director = async_get_director(hass, entry.entry_id)
    if director is None:
        return
    async_add_entities([AnomalySensor(director, entry)])
    known: dict[str, CheckSensor] = {}

    @callback
    def _sync() -> None:
        current = {c["id"] for c in director.checks}
        new = [
            CheckSensor(director, entry, check_id)
            for check_id in current
            if check_id not in known
        ]
        for sensor in new:
            known[sensor.check_id] = sensor
        if new:
            async_add_entities(new)
        registry = er.async_get(hass)
        for check_id in [c for c in known if c not in current]:
            del known[check_id]
            if (
                entity_id := registry.async_get_entity_id(
                    "binary_sensor", DOMAIN, _unique_id(entry, check_id)
                )
            ) is not None:
                registry.async_remove(entity_id)

    _sync()
    entry.async_on_unload(director.async_add_listener(_sync))


class CheckSensor(BinarySensorEntity):
    """On while the Check passes, off while it blocks the next Run."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, director: Director, entry: ConfigEntry, check_id: str) -> None:
        self._director = director
        self.check_id = check_id
        self._attr_unique_id = _unique_id(entry, check_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._director.async_add_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        if self.hass is not None and self.platform is not None:
            self.async_write_ha_state()

    def _record(self) -> dict | None:
        return next((c for c in self._director.checks if c["id"] == self.check_id), None)

    @property
    def name(self) -> str | None:
        rec = self._record()
        return rec["name"] if rec else None

    @property
    def is_on(self) -> bool | None:
        rec = self._record()
        if rec is None or not rec["enabled"]:
            return None
        return all(w["check"] != self.check_id for w in self._director.state["waiting_on"])


class AnomalySensor(BinarySensorEntity):
    """On while any Anomaly is active (dismissed ones included); the active
    list is an attribute (spec §11.6, §13.5)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "anomaly"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, director: Director, entry: ConfigEntry) -> None:
        self._director = director
        self._attr_unique_id = f"{entry.entry_id}_anomaly"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._director.async_add_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        if self.hass is not None and self.platform is not None:
            self.async_write_ha_state()

    @property
    def _active(self) -> list[dict]:
        return self._director.state["anomalies"]["active"]

    @property
    def is_on(self) -> bool:
        return bool(self._active)

    @property
    def extra_state_attributes(self) -> dict:
        return {"active": [dict(a) for a in self._active]}
