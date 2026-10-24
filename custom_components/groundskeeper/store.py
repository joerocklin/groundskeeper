"""Per-Mower JSON Store (spec §14.3, ADR 0001).

One HA Store per config entry, key `groundskeeper.<entry_id>`, holding
everything the panel edits plus all runtime state. `entry.data` holds only
the Mower's device_id; `entry.options` is unused.
"""

from __future__ import annotations

import copy
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import storage
from homeassistant.util import dt as dt_util

from .store_validation import validate_store

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_VERSION_MINOR = 0  # minor bumps are additive: no migration (spec §14.7)

# Chained major migrations: MIGRATIONS[n] turns a version-n payload into n+1.
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}

# Why the Store is in safe idle (spec §14.7); None when usable.
SAFE_IDLE_NEWER_VERSION = "newer_version"
SAFE_IDLE_INVALID_CONFIG = "invalid_config"
SAFE_IDLE_CORRUPT = "corrupt"


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
            f"groundskeeper.{entry_id}",
            atomic_writes=True,
            minor_version=STORAGE_VERSION_MINOR,
        )

        self._shared: dict | None = None
        self.safe_idle: str | None = None
        self.state_discarded = False

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict
    ) -> dict:
        """Minor bumps pass through; major bumps chain `MIGRATIONS`, backed up first."""
        if old_major_version == self.version:
            return old_data
        await self._async_backup(f"v{old_major_version}")
        data = old_data
        for major in range(old_major_version, self.version):
            data = MIGRATIONS[major](data)
        return data

    async def _async_backup(self, suffix: str) -> None:
        """Copy the raw file to `<key>.<suffix>.bak` beside it, if it exists."""

        def _copy() -> None:
            src = Path(self.path)
            if src.exists():
                shutil.copyfile(src, f"{src}.{suffix}.bak")

        await self.hass.async_add_executor_job(_copy)

    def _corrupt_file_seen(self) -> bool:
        """HA renames an undecodable file away and returns None; it leaves an issue."""
        prefix = f"storage_corruption_{self.key}_"
        return any(
            issue_id.startswith(prefix)
            for (_domain, issue_id) in ir.async_get(self.hass).issues
        )

    async def async_data(self) -> dict:
        """The one in-memory copy of this Mower's data, loaded once.

        Every writer (core, Zone sync) mutates this same dict, so whole-file
        saves never clobber each other's sections. Unusable files put the
        Store in safe idle (`safe_idle` set, writes ignored); see spec §14.7.
        """
        if self._shared is not None:
            return self._shared
        try:
            raw = await self.async_load()
        except storage.UnsupportedStorageVersionError:
            _LOGGER.error("Store %s is from a newer version; safe idle", self.key)
            self.safe_idle = SAFE_IDLE_NEWER_VERSION
            raw = None
        else:
            if raw is None and self._corrupt_file_seen():
                _LOGGER.error("Store %s was corrupt; safe idle", self.key)
                self.safe_idle = SAFE_IDLE_CORRUPT
            elif raw is None:
                data = empty_store_data()
                await super().async_save(copy.deepcopy(data))
                self._shared = data
                return data
        if self.safe_idle:
            self._shared = empty_store_data()
            return self._shared
        result = validate_store(raw)
        for note in result.dropped:
            _LOGGER.warning("Dropped bad Store record in %s: %s", self.key, note)
        data = result.data
        if not result.config_ok:
            _LOGGER.error("Store %s has an invalid config; safe idle", self.key)
            self.safe_idle = SAFE_IDLE_INVALID_CONFIG
            self._shared = empty_store_data()
            return self._shared
        if not result.state_ok:
            _LOGGER.error("Store %s has invalid state; discarded", self.key)
            await self._async_backup(f"state_invalid.{dt_util.utcnow():%Y%m%dT%H%M%S}")
            self.state_discarded = True
            await super().async_save(copy.deepcopy(data))
        self._shared = data
        return data

    async def async_start_fresh(self) -> None:
        """Leave safe idle by replacing everything with an empty Store."""
        await self.async_restore(None, None)

    async def async_restore(self, config: dict | None, history: dict | None) -> None:
        """Leave safe idle with `config` (and `history`) from an export (spec §14.7)."""
        self.safe_idle = None
        self.state_discarded = False
        data = empty_store_data()
        if config is not None:
            data["config"] = config
        if history is not None:
            data["history"] = history
        self._shared = data
        await super().async_save(copy.deepcopy(self._shared))
        registry = ir.async_get(self.hass)
        for domain, issue_id in list(registry.issues):
            if issue_id.startswith(f"storage_corruption_{self.key}_"):
                ir.async_delete_issue(self.hass, domain, issue_id)

    async def async_save(self, data: dict) -> None:
        """Save a snapshot copy: serialisation runs in an executor thread.

        Ignored in safe idle: the user's file is never overwritten (spec §14.7).
        """
        if self.safe_idle:
            return
        await super().async_save(copy.deepcopy(data))

    def async_delay_save_shared(self, delay: float) -> None:
        """Throttled save of the shared data (snapshot summary, `learned`)."""
        shared = self._shared
        if shared is not None and not self.safe_idle:
            self.async_delay_save(lambda: copy.deepcopy(shared), delay)
