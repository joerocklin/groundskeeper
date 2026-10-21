"""Start, Pause, Resume, Stop now and Cancel buttons (spec §13.5)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import controls
from .const import DOMAIN
from .runtime import Director, async_get_director

BUTTONS: dict[str, Callable[[Director], Awaitable[None]]] = {
    "start": controls.async_start,
    "pause": controls.async_pause,
    "resume": controls.async_resume,
    "stop_now": controls.async_stop_now,
    "cancel": controls.async_cancel,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the five Schedule control buttons for one Mower."""
    director = async_get_director(hass, entry.entry_id)
    if director is None:
        return
    async_add_entities(ControlButton(director, entry, key) for key in BUTTONS)


class ControlButton(ButtonEntity):
    """Presses the same control the matching service runs."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, director: Director, entry: ConfigEntry, key: str) -> None:
        self._director = director
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    async def async_press(self) -> None:
        await BUTTONS[self._key](self._director)
