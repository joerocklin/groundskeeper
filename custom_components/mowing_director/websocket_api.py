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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from .const import DOMAIN
from .core import DETECTORS, plan_status
from .heights import MAX_MAX_CUT, MIN_MAX_CUT
from .run_settings import SettingsError, finishing_set, mower_run_defaults, validate_overrides
from .notifier import (
    CATEGORY_DEFAULTS,
    CONFIG_CATEGORIES,
    CONFIG_TARGETS,
    LOCKED_CATEGORIES,
    notify_settings,
)
from .now_view import CONTROLS as RUN_CONTROLS, now_view
from .runtime import Director, async_get_director
from .schedule_ops import (
    ScheduleError,
    create_plan,
    delete_run,
    edit_run,
    move_run,
    rederive,
    reroll_angles,
    remove_plan,
)
from .transfer import async_export, async_import, async_undo_import
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
    for command in (ws_list_checks, ws_create_check, ws_update_check, ws_delete_check,
                    ws_list_gates, ws_create_gate, ws_update_gate, ws_delete_gate):
        websocket_api.async_register_command(hass, command)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_refresh_zones)
    websocket_api.async_register_command(hass, ws_set_zone_hidden)
    websocket_api.async_register_command(hass, ws_get_zones)
    websocket_api.async_register_command(hass, ws_list_prompts)
    websocket_api.async_register_command(hass, ws_subscribe_prompts)
    for cmd in (ws_get_now, ws_subscribe_now, ws_run_control, ws_answer_prompt):
        websocket_api.async_register_command(hass, cmd)
    for cmd in (
        ws_get_schedule,
        ws_add_plan,
        ws_edit_run,
        ws_delete_run,
        ws_move_run,
        ws_remove_plan,
        ws_rederive_plan,
        ws_reroll_angles,
        ws_get_run_settings,
        ws_set_mower_run_settings,
        ws_set_zone_run_settings,
        ws_get_zone_angles,
        ws_set_zone_bearing,
        ws_reset_zone_bearing,
        ws_set_zone_range,
        ws_reset_zone_range,
        ws_set_zone_range_tolerance,
        ws_get_notify_settings,
        ws_set_notify_settings,
        ws_dismiss_anomaly,
        ws_set_anomaly_action,
        ws_history,
        ws_get_retention,
        ws_set_retention_limits,
        ws_delete_zone,
        ws_export_config,
        ws_import_config,
        ws_undo_import,
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
    view = await sync.async_zone_view()
    director = async_get_director(hass, msg["entry_id"])
    if director is not None:
        view = {**view, **director.gate_views()}
    connection.send_result(msg["id"], view)


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


# -- Now tab (spec §16.2) ----------------------------------------------------------


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/get_now", vol.Required("entry_id"): str}
)
@callback
def ws_get_now(hass, connection, msg) -> None:
    """Status card, Up next and Check pills for the Now tab."""
    director = _director_or_error(hass, connection, msg)
    if director is not None:
        connection.send_result(msg["id"], now_view(director))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_now", vol.Required("entry_id"): str}
)
@callback
def ws_subscribe_now(hass, connection, msg) -> None:
    """The Now view now, then on every Director change (live updates)."""
    director = _director_or_error(hass, connection, msg)
    if director is None:
        return

    @callback
    def _push() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], now_view(director)))

    connection.subscriptions[msg["id"]] = director.async_add_listener(_push)
    connection.send_result(msg["id"])
    _push()


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/run_control", vol.Required("entry_id"): str,
     vol.Required("action"): vol.In(sorted(RUN_CONTROLS))}
)
@websocket_api.async_response
async def ws_run_control(hass, connection, msg) -> None:
    """A status-card button: the same control the matching service runs."""
    director = _director_or_error(hass, connection, msg)
    if director is None:
        return
    try:
        await RUN_CONTROLS[msg["action"]](director)
    except ServiceValidationError as err:
        connection.send_error(msg["id"], err.translation_key or "invalid", str(err))
        return
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], {})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/answer_prompt", vol.Required("entry_id"): str,
     vol.Required("prompt_id"): str, vol.Required("action"): str}
)
@websocket_api.async_response
async def ws_answer_prompt(hass, connection, msg) -> None:
    """Answer a pending Prompt from its panel card (same as the `answer_prompt` service)."""
    director = _director_or_error(hass, connection, msg)
    if director is None:
        return
    prompt = director.find_prompt(msg["prompt_id"])
    if prompt is None:
        connection.send_error(msg["id"], "prompt_not_pending", "That Prompt is no longer pending")
        return
    if msg["action"] not in prompt["actions"]:
        connection.send_error(msg["id"], "prompt_action_invalid", f"Not an action: {msg['action']}")
        return
    await director.async_answer_prompt(prompt["prompt_id"], msg["action"])
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], {})


# -- Schedule: add / edit / reorder / remove (spec §5.7, §5.8, §13.2) --------------
# Panel-only: heights and settings are SI (the panel converts for display, §5.9).


def _director_or_error(hass, connection, msg) -> Director | None:
    director = async_get_director(hass, msg["entry_id"])
    if director is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
    return director


async def _schedule_call(hass, connection, msg, call) -> None:
    """Run `call(director)`; a refused operation is a clean error carrying its key."""
    director = _director_or_error(hass, connection, msg)
    if director is None:
        return
    try:
        result = await call(director)
    except ScheduleError as err:
        connection.send_error(msg["id"], err.key, err.placeholders.get("error", err.key))
        return
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], result)


def schedule_view(director: Director) -> dict[str, Any]:
    """Plans (with derived status) and pending/active Runs on the Schedule."""
    core = director.core_state
    state = core["state"]
    plans = [
        {**plan, "status": plan_status(core, plan_id).value}
        for plan_id, plan in state["plans"].items()
    ]
    return {
        "schedule": dict(state["schedule"]),
        "plans": plans,
        "runs": [dict(r) for r in state["runs"]],
    }


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/get_schedule", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_get_schedule(hass, connection, msg) -> None:
    """The Schedule: status, Plans and Runs in order."""
    await _schedule_call(hass, connection, msg, lambda d: _ready(schedule_view(d)))


async def _ready(value):
    return value


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/add_plan",
        vol.Required("entry_id"): str,
        vol.Required("zone"): str,
        vol.Required("start_height_mm"): vol.Coerce(float),
        vol.Required("end_height_mm"): vol.Coerce(float),
        vol.Optional("max_cut_pct"): vol.Coerce(float),
        vol.Optional("base_angle"): vol.Coerce(float),
        vol.Optional("finishing"): vol.Any(None, bool),
        vol.Optional("settings"): dict,
        vol.Optional("start", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_add_plan(hass, connection, msg) -> None:
    """Panel add path: the new Plan merges into the Schedule by step index."""
    await _schedule_call(
        hass, connection, msg,
        lambda d: create_plan(
            d,
            zone=msg["zone"],
            start_height_mm=msg["start_height_mm"],
            end_height_mm=msg["end_height_mm"],
            max_cut_pct=msg.get("max_cut_pct"),
            base_angle=msg.get("base_angle"),
            finishing=msg.get("finishing"),
            settings=msg.get("settings"),
            start=msg["start"],
            position="merge",
            origin="panel",
        ),
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/edit_run",
        vol.Required("entry_id"): str,
        vol.Required("run_id"): str,
        vol.Optional("height_mm"): vol.Coerce(float),
        vol.Optional("angle_deg"): vol.Coerce(float),
        vol.Optional("settings"): dict,
    }
)
@websocket_api.async_response
async def ws_edit_run(hass, connection, msg) -> None:
    """Edit a pending Run within the device limits; warnings come back."""
    changes = {k: msg[k] for k in ("height_mm", "angle_deg", "settings") if k in msg}
    await _schedule_call(hass, connection, msg, lambda d: edit_run(d, msg["run_id"], changes))


async def _done(coro) -> dict:
    await coro
    return {}


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/delete_run", vol.Required("entry_id"): str,
     vol.Required("run_id"): str}
)
@websocket_api.async_response
async def ws_delete_run(hass, connection, msg) -> None:
    """Delete a pending Run."""
    await _schedule_call(
        hass, connection, msg, lambda d: _done(delete_run(d, msg["run_id"]))
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/move_run", vol.Required("entry_id"): str,
     vol.Required("run_id"): str, vol.Required("position"): vol.All(int, vol.Range(min=0))}
)
@websocket_api.async_response
async def ws_move_run(hass, connection, msg) -> None:
    """Move a pending Run to a position among the pending Runs."""
    await _schedule_call(
        hass, connection, msg, lambda d: _done(move_run(d, msg["run_id"], msg["position"]))
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/remove_plan", vol.Required("entry_id"): str,
     vol.Required("plan_id"): str}
)
@websocket_api.async_response
async def ws_remove_plan(hass, connection, msg) -> None:
    """Remove a Plan from the Schedule (the `remove_plan` service's panel twin)."""
    await _schedule_call(
        hass, connection, msg, lambda d: _done(remove_plan(d, msg["plan_id"]))
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/rederive_plan", vol.Required("entry_id"): str,
     vol.Required("plan_id"): str}
)
@websocket_api.async_response
async def ws_rederive_plan(hass, connection, msg) -> None:
    """Re-derive a not-started Plan from current defaults (spec §5.1)."""
    await _schedule_call(hass, connection, msg, lambda d: rederive(d, msg["plan_id"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/reroll_angles", vol.Required("entry_id"): str,
     vol.Required("plan_id"): str}
)
@websocket_api.async_response
async def ws_reroll_angles(hass, connection, msg) -> None:
    """Re-roll angles: redraw a not-started Plan's angles (spec §5.5)."""
    await _schedule_call(hass, connection, msg, lambda d: reroll_angles(d, msg["plan_id"]))


# -- Default Run settings, Max cut and the Finishing Run override (spec §5.1, §5.6) --


def run_settings_view(director: Director) -> dict[str, Any]:
    cfg = director.data.get("config", {})
    return {
        "mower": {
            "run_defaults": mower_run_defaults(director.adapter.limits, cfg),
            "overrides": cfg.get("run_defaults") or {},
            "max_cut_pct": cfg.get("max_cut_pct", 13),
            "finishing": cfg.get("finishing"),
        },
        "zones": [
            {
                "zone_id": z["id"],
                "run_defaults": z.get("run_defaults") or {},
                "finishing": z.get("finishing"),
                "effective_finishing": finishing_set(cfg, z),
            }
            for z in cfg.get("zones", [])
        ],
    }


_SETTINGS_SET = vol.Any(None, dict)


def _check_sets(director: Director, msg: dict) -> None:
    for key in ("run_defaults", "finishing"):
        if msg.get(key):
            validate_overrides(director.adapter.limits, msg[key])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/get_run_settings", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_get_run_settings(hass, connection, msg) -> None:
    """Mower and Zone default Run settings, Max cut and Finishing Run overrides."""
    await _schedule_call(hass, connection, msg, lambda d: _ready(run_settings_view(d)))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_mower_run_settings",
        vol.Required("entry_id"): str,
        vol.Optional("run_defaults"): _SETTINGS_SET,
        vol.Optional("max_cut_pct"): vol.All(
            vol.Coerce(float), vol.Range(min=MIN_MAX_CUT * 100, max=MAX_MAX_CUT * 100)
        ),
        vol.Optional("finishing"): _SETTINGS_SET,
    }
)
@websocket_api.async_response
async def ws_set_mower_run_settings(hass, connection, msg) -> None:
    """Edit Mower defaults (`null` clears a set). Plans already built keep theirs."""

    async def call(director: Director) -> dict:
        try:
            _check_sets(director, msg)
        except SettingsError as err:
            raise ScheduleError("invalid_settings", error=str(err)) from None

        def edit(cfg: dict) -> None:
            for key in ("run_defaults", "finishing"):
                if key in msg:
                    if msg[key]:
                        cfg[key] = msg[key]
                    else:
                        cfg.pop(key, None)
            if "max_cut_pct" in msg:
                cfg["max_cut_pct"] = msg["max_cut_pct"]

        await director.async_edit_config(edit)
        return run_settings_view(director)

    await _schedule_call(hass, connection, msg, call)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_zone_run_settings",
        vol.Required("entry_id"): str,
        vol.Required("zone_id"): str,
        vol.Optional("run_defaults"): _SETTINGS_SET,
        vol.Optional("finishing"): _SETTINGS_SET,
    }
)
@websocket_api.async_response
async def ws_set_zone_run_settings(hass, connection, msg) -> None:
    """Edit a Zone's default Run settings / Finishing override (`null` clears)."""

    async def call(director: Director) -> dict:
        zone = next(
            (z for z in director.data.get("config", {}).get("zones", [])
             if z["id"] == msg["zone_id"]),
            None,
        )
        if zone is None:
            raise ScheduleError("unknown_zone", zone=msg["zone_id"])
        try:
            _check_sets(director, msg)
        except SettingsError as err:
            raise ScheduleError("invalid_settings", error=str(err)) from None

        def edit(cfg: dict) -> None:
            for z in cfg["zones"]:
                if z["id"] == msg["zone_id"]:
                    for key in ("run_defaults", "finishing"):
                        if key in msg:
                            if msg[key]:
                                z[key] = msg[key]
                            else:
                                z.pop(key, None)

        await director.async_edit_config(edit)
        return run_settings_view(director)

    await _schedule_call(hass, connection, msg, call)


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


# -- Gates (spec §4.7) ---------------------------------------------------------------

_GATE_FIELDS = {
    vol.Optional("name"): str,
    vol.Optional("channels"): [str],
    vol.Optional("state_entity"): vol.Any(str, None),
    vol.Optional("open_action"): vol.Any([dict], None),
    vol.Optional("close_action"): vol.Any([dict], None),
    vol.Optional("notify"): vol.Any(bool, None),
}


async def _gates_call(hass, connection, msg, call) -> None:
    director = async_get_director(hass, msg["entry_id"])
    if director is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    try:
        result = await call(director)
    except (StopIteration, KeyError):
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Gate")
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_gates", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_list_gates(hass, connection, msg) -> None:
    """Gates with believed state, the Zones behind them and unrouted Zones."""
    await _gates_call(hass, connection, msg, lambda d: _ready(d.gate_views()))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/create_gate",
        vol.Required("entry_id"): str,
        **{vol.Required(k.schema) if k.schema in ("name", "channels") else k: v
           for k, v in _GATE_FIELDS.items()},
    }
)
@websocket_api.async_response
async def ws_create_gate(hass, connection, msg) -> None:
    """Declare a Gate on one or more Channels."""
    fields = {k: v for k, v in msg.items() if k not in ("id", "type", "entry_id")}
    await _gates_call(hass, connection, msg, lambda d: d.async_create_gate(fields))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/update_gate",
        vol.Required("entry_id"): str,
        vol.Required("gate_id"): str,
        vol.Required("changes"): vol.Schema(_GATE_FIELDS),
    }
)
@websocket_api.async_response
async def ws_update_gate(hass, connection, msg) -> None:
    await _gates_call(
        hass, connection, msg, lambda d: d.async_update_gate(msg["gate_id"], msg["changes"])
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/delete_gate",
        vol.Required("entry_id"): str,
        vol.Required("gate_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_gate(hass, connection, msg) -> None:
    async def call(director):
        await director.async_delete_gate(msg["gate_id"])
        return {}

    await _gates_call(hass, connection, msg, call)


# -- Notification targets and category toggles (spec §12.1, §12.3) -----------------


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/get_notify_settings", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_get_notify_settings(hass, connection, msg) -> None:
    """Legacy `notify.mobile_app_*` service names and the category toggles."""
    await _schedule_call(
        hass, connection, msg, lambda d: _ready(notify_settings(d.data.get("config", {})))
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_notify_settings",
        vol.Required("entry_id"): str,
        vol.Optional("targets"): [str],
        vol.Optional("categories"): {vol.In(list(CATEGORY_DEFAULTS)): bool},
    }
)
@websocket_api.async_response
async def ws_set_notify_settings(hass, connection, msg) -> None:
    """Edit the targets (service names, `notify.` prefix optional) and toggles."""

    async def call(director: Director) -> dict:
        categories = msg.get("categories", {})
        if any(categories.get(locked) is False for locked in LOCKED_CATEGORIES):
            raise ScheduleError(
                "category_locked", error="Failure and on-hold notifications are always on."
            )

        def edit(cfg: dict) -> None:
            if "targets" in msg:
                names = [t.strip().removeprefix("notify.") for t in msg["targets"]]
                cfg[CONFIG_TARGETS] = list(dict.fromkeys(n for n in names if n))
            if categories:
                cfg[CONFIG_CATEGORIES] = {**cfg.get(CONFIG_CATEGORIES, {}), **categories}

        await director.async_edit_config(edit)
        return notify_settings(director.data.get("config", {}))

    await _schedule_call(hass, connection, msg, call)


# -- Anomaly detectors (spec §11, §16) --------------------------------------------


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/dismiss_anomaly",
        vol.Required("entry_id"): str,
        vol.Required("detector"): vol.In(DETECTORS),
    }
)
@websocket_api.async_response
async def ws_dismiss_anomaly(hass, connection, msg) -> None:
    """Dismiss an active Anomaly from the panel."""
    director = async_get_director(hass, msg["entry_id"])
    if director is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    if director.active_anomaly(msg["detector"]) is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Anomaly not active")
        return
    await director.async_dismiss_anomaly(msg["detector"])
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], {})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_anomaly_action",
        vol.Required("entry_id"): str,
        vol.Required("detector"): vol.In(DETECTORS),
        vol.Required("action"): vol.In(["off", "notify", "pause"]),
    }
)
@websocket_api.async_response
async def ws_set_anomaly_action(hass, connection, msg) -> None:
    """Set a detector's per-Mower action (`off` / `notify` / `pause`)."""
    director = async_get_director(hass, msg["entry_id"])
    if director is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    await director.async_set_anomaly_action(msg["detector"], msg["action"])
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], {})


# -- Run history and retention limits (spec §14.3, §14.6) ----------------------


async def _director_call(hass, connection, msg, call) -> None:
    director = async_get_director(hass, msg["entry_id"])
    if director is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    try:
        result = await call(director)
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/history",
        vol.Required("entry_id"): str,
        vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
        vol.Optional("limit", default=50): vol.All(int, vol.Range(min=1, max=200)),
    }
)
@websocket_api.async_response
async def ws_history(hass, connection, msg) -> None:
    """One page of finished Runs, newest first (History tab)."""

    async def call(director: Director) -> dict:
        return director.history_page(msg["offset"], msg["limit"])

    await _director_call(hass, connection, msg, call)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/get_retention", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_get_retention(hass, connection, msg) -> None:
    """Retention limits and their capability warnings (Settings -> Storage)."""

    async def call(director: Director) -> dict:
        return director.retention()

    await _director_call(hass, connection, msg, call)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_retention_limits",
        vol.Required("entry_id"): str,
        vol.Required("limits"): dict,
        vol.Optional("confirm", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_set_retention_limits(hass, connection, msg) -> None:
    """Set limits. A change that would delete only previews until `confirm`."""

    async def call(director: Director) -> dict:
        return await director.async_set_retention_limits(
            msg["limits"], confirm=msg["confirm"]
        )

    await _director_call(hass, connection, msg, call)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/delete_zone",
        vol.Required("entry_id"): str,
        vol.Required("zone_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_zone(hass, connection, msg) -> None:
    """Delete a `missing` Zone, its samples and overrides (spec §14.6)."""
    sync = async_get_zone_sync(hass, msg["entry_id"])
    if sync is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Mower")
        return
    try:
        await sync.async_delete_zone(msg["zone_id"])
    except KeyError:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Zone")
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_ALLOWED, str(err))
        return
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"])


# -- Export and import (spec §14.8) ----------------------------------------------------

async def _transfer_call(hass, connection, msg, call) -> None:
    """Like `_schedule_call`, but refusals are translated HA errors."""
    director = _director_or_error(hass, connection, msg)
    if director is None:
        return
    try:
        result = await call(director)
    except HomeAssistantError as err:
        key = getattr(err, "translation_key", None) or "error"
        connection.send_error(msg["id"], key, str(err))
        return
    async_notify_state_changed(hass, msg["entry_id"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/export_config",
        vol.Required("entry_id"): str,
        vol.Optional("include_history", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_export_config(hass, connection, msg) -> None:
    """The export file, for the panel to download."""

    async def call(director: Director) -> dict:
        return {"export": async_export(director, msg["include_history"])}

    await _transfer_call(hass, connection, msg, call)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/import_config",
        vol.Required("entry_id"): str,
        vol.Required("export"): dict,
    }
)
@websocket_api.async_response
async def ws_import_config(hass, connection, msg) -> None:
    """Replace `config` from an uploaded export; allowed in safe idle (restore)."""

    async def call(director: Director) -> dict:
        return {"report": await async_import(director, msg["export"])}

    await _transfer_call(hass, connection, msg, call)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/undo_import", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_undo_import(hass, connection, msg) -> None:
    """Restore the config the last import replaced."""

    async def call(director: Director) -> dict:
        await async_undo_import(director)
        return {}

    await _transfer_call(hass, connection, msg, call)
