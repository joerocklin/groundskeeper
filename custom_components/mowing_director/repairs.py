"""Repairs fix flows (spec §4.8)."""

from __future__ import annotations

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

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


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    """Create the fix flow for a Mowing Director issue."""
    if issue_id.startswith("missing_zones_") and data:
        return MissingZonesFlow(data["entry_id"])
    raise ValueError(f"unknown issue {issue_id}")
