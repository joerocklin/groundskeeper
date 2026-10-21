"""Mowing Director integration (spec §2.4).

One config entry per Mower (ADR 0003). The entry owns a per-Mower Store;
`entry.data` holds only the Mower's device_id (ADR 0001). Unload, reload
and disable never command the Mower - in the skeleton there is nothing to
command, and the Store is simply released.
"""

from __future__ import annotations

from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .housekeeping import async_detach_housekeeping
from .panel import async_register_panel, async_remove_panel
from .websocket_api import async_register_commands
from .store import MowerStore, empty_store_data
from .zone_sync import async_detach_zone_sync, async_get_zone_sync

type MowingDirectorConfigEntry = ConfigEntry[MowerStore]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Mowing Director. Services are registered here, never per entry."""
    async_register_commands(hass)
    # The rest of the service set lands in later tickets.

    async def _refresh_zones(call: ServiceCall) -> None:
        entry = _target_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
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


def _target_entry(hass: HomeAssistant, entry_id: str | None) -> ConfigEntry:
    """Resolve a service's Mower: explicit id, else the only loaded entry (§13.1)."""
    loaded = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id is not None:
        for entry in loaded:
            if entry.entry_id == entry_id:
                return entry
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="unknown_entry"
        )
    if len(loaded) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_ambiguous"
        )
    return loaded[0]


async def async_setup_entry(
    hass: HomeAssistant, entry: MowingDirectorConfigEntry
) -> bool:
    """Load a Mower's Store and mark the entry ready."""
    store = MowerStore(hass, entry.entry_id)
    data = await store.async_load()
    if data is None:
        # First install, or a corrupt file HA renamed away; never treat a
        # None load as "nothing in flight" beyond seeding the empty shape.
        data = empty_store_data()
        await store.async_save(data)
    entry.runtime_data = store
    await async_register_panel(hass)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MowingDirectorConfigEntry
) -> bool:
    """Unload a Mower. Never commands the Mower; state is already persisted."""
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
    """Delete the Mower's Store and the `.bak` files our migrations create."""
    store = MowerStore(hass, entry.entry_id)
    await store.async_remove()

    def _remove_backups() -> None:
        storage_dir = Path(hass.config.path(".storage"))
        for path in storage_dir.glob(f"mowing_director.{entry.entry_id}*.bak"):
            path.unlink(missing_ok=True)

    await hass.async_add_executor_job(_remove_backups)
