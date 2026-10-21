"""Per-Mower JSON Store (spec §14.3, ADR 0001).

One HA Store per config entry, key `mowing_director.<entry_id>`, holding
everything the panel edits plus all runtime state. `entry.data` holds only
the Mower's device_id; `entry.options` is unused.
"""

from __future__ import annotations

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
