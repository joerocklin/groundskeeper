"""Mowing Director integration (spec §2.4).

One config entry per Mower (ADR 0003). The entry owns a per-Mower Store;
`entry.data` holds only the Mower's device_id (ADR 0001). Unload, reload
and disable never command the Mower - in the skeleton there is nothing to
command, and the Store is simply released.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_CONFIG_ENTRY_ID, CONF_DEVICE, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .adapters import get_adapter_factory
from .const import DOMAIN
from .core import active_run, normalise_core_state
from .housekeeping import async_detach_housekeeping
from .notifier import ACTION_EVENT, async_handle_action_event
from .panel import async_register_panel, async_remove_panel
from .websocket_api import async_register_commands
from .runtime import Director, async_register_director, async_unregister_director
from .schedule_ops import redraw_provisional
from .services import async_register_services, target_entry
from .store import MowerStore
from .zone_sync import (
    async_attach_zone_sync,
    async_detach_zone_sync,
    async_get_zone_sync,
)

_LOGGER = logging.getLogger(__name__)

type MowingDirectorConfigEntry = ConfigEntry[MowerStore]

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Mowing Director. Services are registered here, never per entry."""
    async_register_commands(hass)
    async_register_services(hass)

    async def _on_action_tap(event: Event) -> None:
        await async_handle_action_event(hass, event)

    hass.bus.async_listen(ACTION_EVENT, _on_action_tap)
    # The rest of the service set lands in later tickets.

    async def _refresh_zones(call: ServiceCall) -> None:
        entry = target_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        sync = async_get_zone_sync(hass, entry.entry_id)
        if sync is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_zone_sync"
            )
        await sync.async_sync()

    hass.services.async_register(
        DOMAIN,
        "refresh_zones",
        _refresh_zones,
        schema=vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): str}),
    )
    return True


def _adapter_for(hass: HomeAssistant, entry: ConfigEntry):
    """Build the adapter for the entry's Mower device, or None if unsupported."""
    device = dr.async_get(hass).async_get(entry.data.get(CONF_DEVICE, ""))
    if device is None:
        return None
    for domain, _identifier in device.identifiers:
        factory = get_adapter_factory(domain)
        if factory is not None:
            return factory(hass, entry)
    return None


async def async_setup_entry(
    hass: HomeAssistant, entry: MowingDirectorConfigEntry
) -> bool:
    """Load a Mower's Store, start its adapter and Director, add entities."""
    store = MowerStore(hass, entry.entry_id)
    await store.async_data()  # seeds the empty shape on first install
    entry.runtime_data = store
    await async_register_panel(hass)

    adapter = _adapter_for(hass, entry)
    if adapter is not None:
        sync = async_attach_zone_sync(hass, entry.entry_id, adapter, store)
        director = Director(hass, entry, adapter, store)
        async_register_director(hass, director)
        await director.async_load()

        @callback
        def _range_changed(zone_id: str, _angle_range: Any) -> None:
            """A Zone's range arrived or changed: redraw provisional angles."""
            entry.async_create_background_task(
                hass, redraw_provisional(director, zone_id), "mowing_director_redraw"
            )

        entry.async_on_unload(sync.subscribe_range_changes(_range_changed))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MowingDirectorConfigEntry
) -> bool:
    """Unload a Mower. Never commands the Mower; state is already persisted."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    async_unregister_director(hass, entry.entry_id)
    async_detach_zone_sync(hass, entry.entry_id)
    async_detach_housekeeping(hass, entry.entry_id)
    others = [
        e
        for e in hass.config_entries.async_loaded_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]
    if not others:
        async_remove_panel(hass)
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: MowingDirectorConfigEntry
) -> None:
    """Delete the Mower's Store and the `.bak` files our migrations create.

    Deleting an entry while a Run is active sends `abort_run` first (spec
    §2.4); HA's own delete confirmation dialog is the user's confirmation.
    The entry is already unloaded, so the adapter is rebuilt from the device.
    """
    store = MowerStore(hass, entry.entry_id)
    if active_run(normalise_core_state(await store.async_data())["state"]) is not None:
        adapter = _adapter_for(hass, entry)
        if adapter is not None:
            try:
                await adapter.abort_run()
            except Exception:  # noqa: BLE001 - removal must still finish
                _LOGGER.exception("abort_run on entry removal raised")
    await store.async_remove()

    def _remove_backups() -> None:
        storage_dir = Path(hass.config.path(".storage"))
        for path in storage_dir.glob(f"mowing_director.{entry.entry_id}*.bak"):
            path.unlink(missing_ok=True)

    await hass.async_add_executor_job(_remove_backups)
