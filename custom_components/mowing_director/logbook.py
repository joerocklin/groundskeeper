"""Logbook descriptions for `mowing_director_event` (spec §13.4)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_TYPE


def describe(data: dict[str, Any]) -> str:
    """One sentence for a Mowing Director event payload."""
    kind = data.get("type")
    zone = data.get("zone_name") or "a Zone"
    reason = f" ({data['reason']})" if data.get("reason") else ""
    match kind:
        case "run_status_changed":
            return f"Run on {zone} went from {data.get('from')} to {data.get('to')}{reason}"
        case "schedule_status_changed":
            return f"Schedule went from {data.get('from')} to {data.get('to')}{reason}"
        case "schedule_completed":
            return "Schedule completed"
        case "plan_added":
            return f"Plan added for {zone}"
        case "plan_removed":
            return f"Plan removed for {zone}"
        case "prompt_sent":
            return f"Prompt sent: {data.get('kind')}"
        case "prompt_answered":
            return f"Prompt {data.get('kind')} answered: {data.get('action')}"
        case "prompt_ignored":
            return f"Ignored a stale Prompt answer: {data.get('action')}"
        case "height_unverified":
            return f"Blade height on {zone} could not be verified"
        case _:
            return str(kind)


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe the one Mowing Director event type."""

    @callback
    def _describe(event: Event) -> dict[str, str]:
        return {LOGBOOK_ENTRY_NAME: "Mowing Director", LOGBOOK_ENTRY_MESSAGE: describe(dict(event.data))}

    async_describe_event(DOMAIN, EVENT_TYPE, _describe)
