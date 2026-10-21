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

import copy
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
from .runtime import async_get_director
from .zone_sync import async_get_zone_sync

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
    for command in (ws_list_checks, ws_create_check, ws_update_check, ws_delete_check):
        websocket_api.async_register_command(hass, command)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_refresh_zones)
    websocket_api.async_register_command(hass, ws_set_zone_hidden)
    websocket_api.async_register_command(hass, ws_get_zones)
    websocket_api.async_register_command(hass, ws_list_prompts)
    websocket_api.async_register_command(hass, ws_subscribe_prompts)
    for cmd in (
        ws_get_zone_angles,
        ws_set_zone_bearing,
        ws_reset_zone_bearing,
        ws_set_zone_range,
        ws_reset_zone_range,
        ws_set_zone_range_tolerance,
    ):
        websocket_api.async_register_command(hass, cmd)


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


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/refresh_zones", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_refresh_zones(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Panel "Refresh Zones" (spec §4.4)."""
    sync = async_get_zone_sync(hass, msg["entry_id"])
    if sync is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    await sync.async_sync()
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/get_zones", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_get_zones(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Zones and Channels for the Zones & Gates tab map and list (spec §16.3)."""
    sync = async_get_zone_sync(hass, msg["entry_id"])
    if sync is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    connection.send_result(msg["id"], await sync.async_zone_view())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_zone_hidden",
        vol.Required("entry_id"): str,
        vol.Required("zone_id"): str,
        vol.Required("hidden"): bool,
    }
)
@websocket_api.async_response
async def ws_set_zone_hidden(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Hide or unhide a Zone (spec §4.4)."""
    sync = async_get_zone_sync(hass, msg["entry_id"])
    if sync is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    try:
        await sync.async_set_hidden(msg["zone_id"], msg["hidden"])
    except KeyError:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Zone")
        return
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"])


# -- Zone bearing and Allowed angle range (spec §4.5, §5.4) ------------------

_ZONE = {vol.Required("entry_id"): str, vol.Required("zone_id"): str}
_ANGLE = vol.All(vol.Coerce(float), vol.Range(min=-90, max=90))


async def _zone_angles_call(hass, connection, msg, call) -> None:
    """Run `call(sync)` for a Zone, answering with its angle records."""
    sync = async_get_zone_sync(hass, msg["entry_id"])
    if sync is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    try:
        result = await call(sync)
    except KeyError:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Zone")
        return
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/get_zone_angles",
        vol.Required("entry_id"): str,
        vol.Optional("zone_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_zone_angles(hass, connection, msg) -> None:
    """Read bearing and range records (one Zone, or all)."""
    sync = async_get_zone_sync(hass, msg["entry_id"])
    if sync is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    try:
        zones = await sync.async_get_angles(msg.get("zone_id"))
    except KeyError:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Zone")
        return
    connection.send_result(msg["id"], {"zones": zones})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_zone_bearing",
        **_ZONE,
        vol.Required("value"): vol.All(vol.Coerce(float), vol.Range(min=0, max=360)),
    }
)
@websocket_api.async_response
async def ws_set_zone_bearing(hass, connection, msg) -> None:
    """Manual Zone bearing."""
    await _zone_angles_call(
        hass, connection, msg, lambda s: s.async_set_bearing(msg["zone_id"], msg["value"])
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/reset_zone_bearing", **_ZONE}
)
@websocket_api.async_response
async def ws_reset_zone_bearing(hass, connection, msg) -> None:
    """Back to the derived bearing."""
    await _zone_angles_call(
        hass, connection, msg, lambda s: s.async_reset_bearing(msg["zone_id"])
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_zone_range",
        **_ZONE,
        vol.Optional("min"): _ANGLE,
        vol.Optional("max"): _ANGLE,
        vol.Optional("full"): True,
    }
)
@websocket_api.async_response
async def ws_set_zone_range(hass, connection, msg) -> None:
    """Manual Allowed angle range: `min` and `max` (min <= 0 <= max), or `full`."""
    if msg.get("full"):
        rng: Any = "full"
    elif "min" in msg and "max" in msg and msg["min"] <= 0 <= msg["max"]:
        rng = {"min": msg["min"], "max": msg["max"]}
    else:
        connection.send_error(
            msg["id"], websocket_api.ERR_INVALID_FORMAT,
            "Need full, or min <= 0 <= max",
        )
        return
    await _zone_angles_call(
        hass, connection, msg, lambda s: s.async_set_range(msg["zone_id"], rng)
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/reset_zone_range", **_ZONE}
)
@websocket_api.async_response
async def ws_reset_zone_range(hass, connection, msg) -> None:
    """Back to the derived range."""
    await _zone_angles_call(
        hass, connection, msg, lambda s: s.async_reset_range(msg["zone_id"])
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_zone_range_tolerance",
        **_ZONE,
        vol.Required("tolerance"): vol.Any(
            None, vol.All(vol.Coerce(float), vol.Range(min=0, max=1))
        ),
    }
)
@websocket_api.async_response
async def ws_set_zone_range_tolerance(hass, connection, msg) -> None:
    """Per-Zone tolerance override (null clears it)."""
    await _zone_angles_call(
        hass, connection, msg,
        lambda s: s.async_set_range_tolerance(msg["zone_id"], msg["tolerance"]),
    )


# -- Pending Prompts (spec §12.12) -----------------------------------------------


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_prompts", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_list_prompts(hass, connection, msg) -> None:
    """Pending Prompts for the panel's cards, with title and body."""
    director = async_get_director(hass, msg["entry_id"])
    if director is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    connection.send_result(msg["id"], {"prompts": await director.async_prompt_views()})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_prompts", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_subscribe_prompts(hass, connection, msg) -> None:
    """Pending Prompts now, then whenever the list changes."""
    director = async_get_director(hass, msg["entry_id"])
    if director is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    last: list[Any] = [None]  # the raw list last pushed

    async def _push() -> None:
        raw = copy.deepcopy(director.state["prompts"])
        views = await director.async_prompt_views()
        last[0] = raw
        connection.send_message(
            websocket_api.event_message(msg["id"], {"prompts": views})
        )

    @callback
    def _changed() -> None:
        # Only a changed Prompt list is worth a push, not every snapshot.
        if director.state["prompts"] != last[0]:
            hass.async_create_task(_push())

    connection.subscriptions[msg["id"]] = director.async_add_listener(_changed)
    connection.send_result(msg["id"])
    await _push()


# -- Checks (spec §9, §13.5) -------------------------------------------------------

_TIMEOUT = vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=1)))
_POLICY = vol.In(["notify", "pause"])
_CHECK_FIELDS = {
    vol.Optional("name"): str,
    vol.Optional("enabled"): bool,
    vol.Optional("config"): dict,
    vol.Optional("timeout_s"): _TIMEOUT,
    vol.Optional("policy"): _POLICY,
}


async def _checks_call(hass, connection, msg, call) -> None:
    director = async_get_director(hass, msg["entry_id"])
    if director is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    try:
        result = await call(director)
    except StopIteration:  # `next()` found no such Check
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Check")
        return
    except (ValueError, KeyError) as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_checks", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_list_checks(hass, connection, msg) -> None:
    """Every Check of one Mower, built-ins included."""

    async def call(director):
        return {"checks": director.checks}

    await _checks_call(hass, connection, msg, call)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/create_check",
        vol.Required("entry_id"): str,
        vol.Required("name"): str,
        vol.Required("config"): dict,
        vol.Optional("enabled"): bool,
        vol.Optional("timeout_s"): _TIMEOUT,
        vol.Optional("policy"): _POLICY,
    }
)
@websocket_api.async_response
async def ws_create_check(hass, connection, msg) -> None:
    """Add a named Custom Check (entity condition or template)."""
    fields = {k: v for k, v in msg.items() if k not in ("id", "type", "entry_id")}
    await _checks_call(hass, connection, msg, lambda d: d.async_create_check(fields))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/update_check",
        vol.Required("entry_id"): str,
        vol.Required("check_id"): str,
        vol.Required("changes"): vol.Schema(_CHECK_FIELDS),
    }
)
@websocket_api.async_response
async def ws_update_check(hass, connection, msg) -> None:
    """Edit a Check; applies at once while the Schedule is `waiting`."""
    await _checks_call(
        hass, connection, msg, lambda d: d.async_update_check(msg["check_id"], msg["changes"])
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/delete_check",
        vol.Required("entry_id"): str,
        vol.Required("check_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_check(hass, connection, msg) -> None:
    """Delete a Custom Check."""

    async def call(director):
        await director.async_delete_check(msg["check_id"])
        return {}

    await _checks_call(hass, connection, msg, call)
