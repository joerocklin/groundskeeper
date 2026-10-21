"""Per-Mower JSON Store (spec §14.3, ADR 0001).

One HA Store per config entry, key `mowing_director.<entry_id>`, holding
everything the panel edits plus all runtime state. `entry.data` holds only
the Mower's device_id; `entry.options` is unused.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage

STORAGE_VERSION = 1
STORAGE_VERSION_MINOR = 0


def empty_store_data() -> dict:
    """Initial payload: the four top-level sections, all empty.

    `config.recurrences` is reserved for the later Recurrence feature
    (spec §14.3); the other sections are filled in by later tickets.
    """
    return {
        "config": {"recurrences": []},
        "state": {},
        "learned": {},
        "history": {},
    }


class MowerStore(storage.Store[dict]):
    """One Store per Mower, atomically written."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise the Store for one config entry."""
        super().__init__(
            hass,
            STORAGE_VERSION,
            f"mowing_director.{entry_id}",
            atomic_writes=True,
            minor_version=STORAGE_VERSION_MINOR,
        )

        self._shared: dict | None = None

    async def async_data(self) -> dict:
        """The one in-memory copy of this Mower's data, loaded once.

        Every writer (core, Zone sync) mutates this same dict, so whole-file
        saves never clobber each other's sections.
        """
        if self._shared is None:
            data = await self.async_load()
            if data is None:
                # First install, or a corrupt file HA renamed away.
                data = empty_store_data()
                await super().async_save(copy.deepcopy(data))
            self._shared = data
        return self._shared

    async def async_save(self, data: dict) -> None:
        """Save a snapshot copy: serialisation runs in an executor thread."""
        await super().async_save(copy.deepcopy(data))

    def async_delay_save_shared(self, delay: float) -> None:
        """Throttled save of the shared data (snapshot summary, `learned`)."""
        shared = self._shared
        if shared is not None:
            self.async_delay_save(lambda: copy.deepcopy(shared), delay)
