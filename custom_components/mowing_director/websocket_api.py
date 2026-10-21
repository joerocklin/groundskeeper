"""Per-entry websocket API for the panel (spec §2.3, §16.1).

Pattern every later panel ticket follows (see docs/panel-websocket-pattern.md):

- One command per panel action, named `mowing_director/<verb>`, always with
  `require_admin` and an `entry_id` selecting the Mower.
- State the panel shows comes from `build_snapshot`; extend it there.
- Anything that changes what the panel shows calls
  `async_notify_state_changed(hass, entry_id)`; subscribers get a fresh
  snapshot pushed.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from .const import DOMAIN

SIGNAL_STATE_CHANGED = f"{DOMAIN}_state_changed_{{}}"


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        return None
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return entry


def build_snapshot(entry: ConfigEntry) -> dict[str, Any]:
    """Everything the panel shows for one Mower. Extended by later tickets."""
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "device_id": entry.data.get("device_id"),
        # Banner above the tabs until complete (spec §16.3, §4.3).
        "setup_checklist": {"complete": True, "items": []},
    }


@callback
def async_notify_state_changed(hass: HomeAssistant, entry_id: str) -> None:
    """Push a fresh snapshot to every panel subscribed to this Mower."""
    async_dispatcher_send(hass, SIGNAL_STATE_CHANGED.format(entry_id))


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the websocket commands (once, from `async_setup`)."""
    websocket_api.async_register_command(hass, ws_list_mowers)
    websocket_api.async_register_command(hass, ws_get_state)
    websocket_api.async_register_command(hass, ws_subscribe)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list_mowers"})
@callback
def ws_list_mowers(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """List loaded Mowers for the panel's switcher."""
    mowers = [
        {"entry_id": e.entry_id, "title": e.title}
        for e in hass.config_entries.async_loaded_entries(DOMAIN)
    ]
    connection.send_result(msg["id"], {"mowers": mowers})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/get_state", vol.Required("entry_id"): str}
)
@callback
def ws_get_state(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return one Mower's snapshot."""
    entry = _loaded_entry(hass, msg["entry_id"])
    if entry is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    connection.send_result(msg["id"], build_snapshot(entry))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe", vol.Required("entry_id"): str}
)
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Subscribe to one Mower's snapshot: sent now, then on every change."""
    entry_id = msg["entry_id"]
    if _loaded_entry(hass, entry_id) is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return

    @callback
    def _push() -> None:
        entry = _loaded_entry(hass, entry_id)
        if entry is not None:
            connection.send_message(
                websocket_api.event_message(msg["id"], build_snapshot(entry))
            )

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass, SIGNAL_STATE_CHANGED.format(entry_id), _push
    )
    connection.send_result(msg["id"])
    _push()
