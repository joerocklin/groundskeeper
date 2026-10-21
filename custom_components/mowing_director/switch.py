"""The manual `switch` Open flag of a Gate without a state entity (spec §4.7)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .gate_entities import GateEntity, async_track_gate_entities
from .runtime import async_get_director


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    director = async_get_director(hass, entry.entry_id)
    if director is None:
        return
    async_track_gate_entities(
        hass, entry, director, "switch", async_add_entities,
        {"open_flag": (lambda gate: not gate["state_entity"], GateOpenSwitch)},
    )


class GateOpenSwitch(GateEntity, SwitchEntity):
    """On while the user says the Gate is open; the director resets it after the crossing."""

    key = "open_flag"
    _attr_name = "Open"

    @property
    def is_on(self) -> bool:
        return bool(self._director.state.get("gate_flags", {}).get(self.gate_id, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._director.async_set_gate_flag(self.gate_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._director.async_set_gate_flag(self.gate_id, False)
