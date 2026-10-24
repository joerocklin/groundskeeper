"""Each Gate is a child device of its Mower's entry (spec §4.7, §13.5).

`async_track_gate_entities` keeps one platform's Gate entities in step with
the Gate list: it adds entities for new Gates, drops those no longer wanted
(a Gate that gains a state entity loses its Open switch), and removes the
device of a deleted Gate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .runtime import Director


def gate_device_identifier(entry: ConfigEntry, gate_id: str) -> tuple[str, str]:
    return (DOMAIN, f"{entry.entry_id}_gate_{gate_id}")


def gate_unique_id(entry: ConfigEntry, gate_id: str, key: str) -> str:
    return f"{entry.entry_id}_gate_{gate_id}_{key}"


class GateEntity(Entity):
    """Shared base: device, naming and refresh on every Director change."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    key = ""

    def __init__(self, director: Director, entry: ConfigEntry, gate_id: str) -> None:
        self._director = director
        self.gate_id = gate_id
        self._attr_unique_id = gate_unique_id(entry, gate_id, self.key)
        gate = self._gate() or {}
        mower = dr.async_get(director.hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={gate_device_identifier(entry, gate_id)},
            name=gate.get("name", gate_id),
            via_device_id=mower.id,
        )

    def _gate(self) -> dict[str, Any] | None:
        return next((g for g in self._director.gates if g["id"] == self.gate_id), None)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._director.async_add_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        if self.hass is not None and self.platform is not None:
            self.async_write_ha_state()


def async_track_gate_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    director: Director,
    domain: str,
    async_add_entities: AddEntitiesCallback,
    kinds: dict[str, tuple[Callable[[dict[str, Any]], bool], type[GateEntity]]],
) -> None:
    """`kinds`: key -> (whether a Gate has this entity, entity class)."""
    known: dict[tuple[str, str], GateEntity] = {}
    devices_seen: set[str] = set()

    @callback
    def _sync() -> None:
        gates = {g["id"]: g for g in director.gates}
        wanted = {
            (gate_id, key)
            for gate_id, gate in gates.items()
            for key, (has, _cls) in kinds.items()
            if has(gate)
        }
        new = []
        for gate_id, key in wanted - set(known):
            entity = kinds[key][1](director, entry, gate_id)
            known[(gate_id, key)] = entity
            new.append(entity)
        if new:
            async_add_entities(new)
        entities = er.async_get(hass)
        for gate_id, key in set(known) - wanted:
            del known[(gate_id, key)]
            if (
                entity_id := entities.async_get_entity_id(
                    domain, DOMAIN, gate_unique_id(entry, gate_id, key)
                )
            ) is not None:
                entities.async_remove(entity_id)
        devices = dr.async_get(hass)
        for gate_id in devices_seen - set(gates):
            if (
                device := devices.async_get_device_by_identifier(
                    gate_device_identifier(entry, gate_id), entry.entry_id
                )
            ) is not None:
                devices.async_remove_device(device.id)
        for gate_id, gate in gates.items():  # a rename follows to the device
            device = devices.async_get_device_by_identifier(
                gate_device_identifier(entry, gate_id), entry.entry_id
            )
            if device is not None and device.name != gate["name"]:
                devices.async_update_device(device.id, name=gate["name"])
        devices_seen.clear()
        devices_seen.update(gates)

    _sync()
    entry.async_on_unload(director.async_add_listener(_sync))
