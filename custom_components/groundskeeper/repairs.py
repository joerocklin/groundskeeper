"""Repairs fix flows (spec §4.8)."""

from __future__ import annotations

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .housekeeping import async_get_housekeeping
from .zone_sync import async_get_zone_sync


class MissingZonesFlow(RepairsFlow):
    """Missing Zones: delete them and their Plans, or wait."""

    def __init__(self, entry_id: str) -> None:
        """Remember which Mower the issue belongs to."""
        self._entry_id = entry_id

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Offer the two choices of spec §4.4."""
        return self.async_show_menu(step_id="init", menu_options=["delete", "wait"])

    async def async_step_delete(self, user_input=None) -> FlowResult:
        """Delete the missing Zones and their Plans."""
        sync = async_get_zone_sync(self.hass, self._entry_id)
        if sync is not None:
            await sync.async_delete_missing()
        return self.async_create_entry(data={})

    async def async_step_wait(self, user_input=None) -> FlowResult:
        """Leave everything as is; the issue stays until the Zones return."""
        return self.async_abort(reason="waiting")


class NativeScheduleFlow(RepairsFlow):
    """Native schedule enabled: disable the tasks after confirmation, or ignore."""

    def __init__(self, entry_id: str) -> None:
        """Remember which Mower the issue belongs to."""
        self._entry_id = entry_id

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Offer the two choices of spec §4.8."""
        return self.async_show_menu(step_id="init", menu_options=["disable", "ignore"])

    async def async_step_disable(self, user_input=None) -> FlowResult:
        """Disable every enabled native task, only after explicit confirmation."""
        hk = async_get_housekeeping(self.hass, self._entry_id)
        if user_input is None:
            placeholders = {"tasks": await hk.async_enabled_task_names() if hk else ""}
            return self.async_show_form(
                step_id="disable", description_placeholders=placeholders
            )
        if hk is not None:
            await hk.async_disable_native_tasks()
        return self.async_create_entry(data={})

    async def async_step_ignore(self, user_input=None) -> FlowResult:
        """Leave the Mower's own schedule alone; dismiss the issue to hide it."""
        return self.async_abort(reason="ignored")


class StoreSafeIdleFlow(RepairsFlow):
    """Safe idle: the only ways out are Start fresh here or Restore export."""

    def __init__(self, entry_id: str) -> None:
        """Remember which Mower the issue belongs to."""
        self._entry_id = entry_id

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Confirm, then wipe the Store and reload the Mower."""
        if user_input is None:
            return self.async_show_form(
                step_id="init", description_placeholders=self._placeholders()
            )
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None:
            await entry.runtime_data.async_start_fresh()
            self.hass.config_entries.async_schedule_reload(self._entry_id)
        return self.async_create_entry(data={})

    def _placeholders(self) -> dict[str, str]:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        store = entry.runtime_data if entry else None
        return {
            "mower": entry.title if entry else "",
            "reason": getattr(store, "safe_idle", "") or "",
        }


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    """Create the fix flow for a Groundskeeper issue."""
    if issue_id.startswith("missing_zones_") and data:
        return MissingZonesFlow(data["entry_id"])
    if issue_id.startswith("native_schedule_") and data:
        return NativeScheduleFlow(data["entry_id"])
    if issue_id.startswith("store_safe_idle_") and data:
        return StoreSafeIdleFlow(data["entry_id"])
    raise ValueError(f"unknown issue {issue_id}")
