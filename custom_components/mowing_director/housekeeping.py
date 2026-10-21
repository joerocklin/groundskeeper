"""Setup and housekeeping Repairs issues (spec §4.8).

`Housekeeping` is the per-Mower checker: native schedule enabled,
conflicting automations, Mower device not found and Mammotion-HA too old.
It is attached like `ZoneSync`. Every issue except the automations one is
non-persistent (spec §2.3): HA drops it on restart and the checks that run
on load re-create it.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from homeassistant.components.automation import (
    automations_with_device,
    automations_with_entity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from .adapter import MowerAdapter
from .const import DOMAIN

ISSUE_NATIVE_SCHEDULE = "native_schedule_{}"
ISSUE_CONFLICTING_AUTOMATIONS = "conflicting_automations_{}"
ISSUE_DEVICE_NOT_FOUND = "device_not_found_{}"
ISSUE_SOURCE_TOO_OLD = "source_too_old_{}"

# Spec §3.8: Mammotion-HA below this version raises a Repairs issue.
MIN_MAMMOTION_VERSION = (0, 6, 18)
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def version_too_old(version: str | None) -> bool:
    """True when `version` parses and is below the supported minimum."""
    match = _VERSION.match(version or "")
    if match is None:
        return False  # unknown is not "too old"; the adapter degrades itself
    return tuple(int(p) for p in match.groups()) < MIN_MAMMOTION_VERSION


class Housekeeping:
    """Per-Mower setup and housekeeping checks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        adapter: MowerAdapter,
        store,
    ) -> None:
        """Bind to one Mower's entry, adapter and Store."""
        self._hass = hass
        self._entry = entry
        self._adapter = adapter
        self._store = store

    async def async_check_all(self) -> None:
        """Every check; runs on load."""
        await self.async_check_native_schedule()
        await self.async_check_automations()
        await self.async_check_device()
        self.async_check_source_version()

    async def async_check_device(self) -> None:
        """Raise or clear "Mower device not found".

        The entry and its Store are untouched; the user rebinds through the
        entry's reconfigure flow (spec §4.8).
        """
        issue_id = ISSUE_DEVICE_NOT_FOUND.format(self._entry.entry_id)
        device_id = self._entry.data.get(CONF_DEVICE, "")
        if dr.async_get(self._hass).async_get(device_id) is not None:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="device_not_found",
            translation_placeholders={"mower": self._entry.title},
        )

    @callback
    def async_check_source_version(self) -> None:
        """Raise or clear "Mammotion-HA too old" (spec §3.8)."""
        issue_id = ISSUE_SOURCE_TOO_OLD.format(self._entry.entry_id)
        version = self._adapter.source_version
        if not version_too_old(version):
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="source_too_old",
            translation_placeholders={
                "version": version or "",
                "minimum": ".".join(str(p) for p in MIN_MAMMOTION_VERSION),
            },
        )

    def _conflicting_automations(self) -> list[str]:
        """Automations that reference the Mower's device or its entities."""
        device_id = self._entry.data.get(CONF_DEVICE)
        if not device_id:
            return []
        found = set(automations_with_device(self._hass, device_id))
        for entity in er.async_entries_for_device(
            er.async_get(self._hass), device_id, include_disabled_entities=True
        ):
            found.update(automations_with_entity(self._hass, entity.entity_id))
        return sorted(found)

    async def async_check_automations(self) -> None:
        """Raise the informational "conflicting automations" issue.

        Persistent, so a dismissal survives restarts; the last-seen set is
        kept in `learned` and the issue is only re-raised (dismissal reset)
        when that set changes. The director never edits automations and the
        issue names none of them.
        """
        issue_id = ISSUE_CONFLICTING_AUTOMATIONS.format(self._entry.entry_id)
        current = self._conflicting_automations()
        data = await self._store.async_data()
        learned = data.setdefault("learned", {})
        previous = learned.get("conflicting_automations")
        if current != previous:
            learned["conflicting_automations"] = current
            await self._store.async_save(data)
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
        if not current:
            return
        if ir.async_get(self._hass).async_get_issue(DOMAIN, issue_id) is None:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="conflicting_automations",
                translation_placeholders={"count": str(len(current))},
            )

    async def async_enabled_task_names(self) -> str:
        """Names of the enabled native tasks, for display."""
        tasks = await self._adapter.list_native_tasks()
        return ", ".join(sorted(t.name for t in tasks if t.enabled))

    async def async_disable_native_tasks(self) -> None:
        """Repairs fix: `set_task_enabled false` on every enabled task."""
        for task in await self._adapter.list_native_tasks():
            if task.enabled:
                await self._adapter.set_task_enabled(task.task_id, False)
        await self.async_check_native_schedule()

    async def async_check_native_schedule(self) -> None:
        """Raise or clear the "native schedule enabled" issue.

        Runs on every load and on each Schedule `idle` -> `waiting`/`running`.
        """
        issue_id = ISSUE_NATIVE_SCHEDULE.format(self._entry.entry_id)
        enabled = sorted(
            t.name for t in await self._adapter.list_native_tasks() if t.enabled
        )
        if not enabled:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="native_schedule",
            translation_placeholders={"tasks": ", ".join(enabled)},
            data={"entry_id": self._entry.entry_id},
        )


@callback
def async_get_housekeeping(hass: HomeAssistant, entry_id: str) -> Housekeeping | None:
    """The running Housekeeping for a Mower entry, if any."""
    return hass.data.get(DOMAIN, {}).get("housekeeping", {}).get(entry_id)


@callback
def async_attach_housekeeping(
    hass: HomeAssistant, entry: ConfigEntry, adapter: MowerAdapter, store
) -> Housekeeping:
    """Create and register the Housekeeping for a Mower entry."""
    hk = Housekeeping(hass, entry, adapter, store)
    hass.data.setdefault(DOMAIN, {}).setdefault("housekeeping", {})[entry.entry_id] = hk
    return hk


@callback
def async_detach_housekeeping(hass: HomeAssistant, entry_id: str) -> None:
    """Forget a Mower's Housekeeping (unload)."""
    hass.data.get(DOMAIN, {}).get("housekeeping", {}).pop(entry_id, None)


@callback
def async_set_run_active_provider(
    hass: HomeAssistant, entry_id: str, provider: Callable[[], bool]
) -> None:
    """Register how to ask whether a Run is `starting`/`mowing`/`on hold`.

    The core registers this when it owns Run state; until then no Run is
    ever active and a rebind is never refused.
    """
    hass.data.setdefault(DOMAIN, {}).setdefault("run_active", {})[entry_id] = provider


@callback
def async_run_active(hass: HomeAssistant, entry_id: str) -> bool:
    """Whether a Run is in flight for this Mower (blocks a device rebind)."""
    provider = hass.data.get(DOMAIN, {}).get("run_active", {}).get(entry_id)
    return bool(provider and provider())
