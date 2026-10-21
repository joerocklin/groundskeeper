"""Mowing Director integration (spec §2.4).

One config entry per Mower (ADR 0003). The entry owns a per-Mower Store;
`entry.data` holds only the Mower's device_id (ADR 0001). Unload, reload
and disable never command the Mower - in the skeleton there is nothing to
command, and the Store is simply released.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .panel import async_register_panel, async_remove_panel
from .websocket_api import async_register_commands
from .store import MowerStore, empty_store_data

type MowingDirectorConfigEntry = ConfigEntry[MowerStore]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Mowing Director. Services are registered here, never per entry."""
    async_register_commands(hass)
    # The service set lands in a later ticket; nothing to register yet.
    return True


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
