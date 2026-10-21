"""Mowing Director services (spec §13.1-13.3).

Every service takes an optional `config_entry_id` (default: the only loaded
Mower) and raises a translated `ServiceValidationError` where it does not
apply. Registered once in `async_setup`, never per entry.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .core import RunStatus, ScheduleStatus, active_run, next_run
from .heights import HeightError
from .plans import (
    AmbiguousZone,
    NeedsStepDown,
    UnknownZone,
    build_plan,
    derive_single_run_heights,
    find_zone,
    mower_run_defaults,
)
from .runtime import Director, async_get_director

ATTR_ZONE = "zone"
ATTR_START_HEIGHT = "start_height_mm"
ATTR_END_HEIGHT = "end_height_mm"
ATTR_START = "start"
ATTR_PROMPT_ID = "prompt_id"
ATTR_ACTION = "action"

_ENTRY = {vol.Optional(ATTR_CONFIG_ENTRY_ID): str}


def _fail(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def target_entry(hass: HomeAssistant, entry_id: str | None) -> ConfigEntry:
    """Resolve a service's Mower: explicit id, else the only loaded entry (§13.1)."""
    loaded = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id is not None:
        for entry in loaded:
            if entry.entry_id == entry_id:
                return entry
        raise _fail("unknown_entry")
    if len(loaded) != 1:
        raise _fail("entry_ambiguous")
    return loaded[0]


def _director(hass: HomeAssistant, call: ServiceCall) -> Director:
    entry = target_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    director = async_get_director(hass, entry.entry_id)
    if director is None:
        raise _fail("no_zone_sync")
    return director


def async_register_services(hass: HomeAssistant) -> None:
    """Register `add_plan`, `start`, `cancel`, `retry_run`, `skip_plan`, `resume_run`,
    `end_run` and `answer_prompt`."""

    async def add_plan(call: ServiceCall) -> ServiceResponse:
        director = _director(hass, call)
        data = call.data
        zones = director.data.get("config", {}).get("zones", [])
        try:
            zone = find_zone(zones, data[ATTR_ZONE])
        except UnknownZone:
            raise _fail("unknown_zone", zone=data[ATTR_ZONE]) from None
        except AmbiguousZone:
            raise _fail("ambiguous_zone", zone=data[ATTR_ZONE]) from None
        try:
            heights = derive_single_run_heights(
                data[ATTR_START_HEIGHT], data[ATTR_END_HEIGHT], director.adapter.limits
            )
        except HeightError as err:
            raise _fail("invalid_height", error=str(err)) from None
        except NeedsStepDown:
            raise _fail("step_down_unsupported") from None

        now = dt_util.utcnow()
        plan, runs = build_plan(
            zone=zone,
            heights=heights,
            defaults=mower_run_defaults(
                director.adapter.limits, director.data.get("config", {})
            ),
            origin="service",
            now=now,
        )
        before = director.state["schedule"]["status"]
        await director.async_add_plan(plan, runs, data[ATTR_START])
        after = director.state["schedule"]["status"]
        started = before == ScheduleStatus.IDLE and after != ScheduleStatus.IDLE
        response: dict[str, Any] = {
            "plan_id": plan["id"],
            "runs": [
                {
                    "run_id": r["id"],
                    "height_mm": r["height_mm"],
                    "angle_deg": r["angle_deg"],
                    "n": r["index"],
                    "m": r["of"],
                }
                for r in runs
            ],
            "rounded": heights.rounded,
            "schedule_status": after,
            "started": started,
        }
        if data[ATTR_START] and not started:
            response["reason"] = (
                "paused" if before == ScheduleStatus.PAUSED else "already_active"
            )
        return response

    async def start(call: ServiceCall) -> None:
        director = _director(hass, call)
        status = director.state["schedule"]["status"]
        if status not in (ScheduleStatus.IDLE, ScheduleStatus.PAUSED):
            raise _fail("schedule_already_active")
        if next_run(director.state) is None:
            raise _fail("nothing_to_start")
        await director.async_start()

    async def cancel(call: ServiceCall) -> None:
        director = _director(hass, call)
        if director.state["schedule"]["status"] == ScheduleStatus.IDLE and not director.state["runs"]:
            raise _fail("nothing_to_cancel")
        await director.async_cancel()

    def _require_failure(director: Director) -> None:
        paused = director.state["schedule"]["status"] == ScheduleStatus.PAUSED
        if not (
            paused
            and any(r["status"] == RunStatus.FAILED for r in director.state["runs"])
        ):
            raise _fail("no_failure_to_answer")

    async def retry_run(call: ServiceCall) -> None:
        director = _director(hass, call)
        _require_failure(director)
        await director.async_retry_run()

    async def skip_plan(call: ServiceCall) -> None:
        director = _director(hass, call)
        _require_failure(director)
        await director.async_skip_plan()

    def _require_on_hold(director: Director) -> None:
        run = active_run(director.state)
        if run is None or run["status"] != RunStatus.ON_HOLD:
            raise _fail("no_hold_to_answer")

    async def resume_run(call: ServiceCall) -> None:
        director = _director(hass, call)
        _require_on_hold(director)
        await director.async_resume_run()

    async def end_run(call: ServiceCall) -> None:
        director = _director(hass, call)
        _require_on_hold(director)
        await director.async_end_run()

    async def answer_prompt(call: ServiceCall) -> None:
        director = _director(hass, call)
        prompt = director.find_prompt(call.data[ATTR_PROMPT_ID])
        if prompt is None:
            raise _fail("prompt_not_pending")
        if call.data[ATTR_ACTION] not in prompt["actions"]:
            raise _fail("prompt_action_invalid", action=call.data[ATTR_ACTION])
        await director.async_answer_prompt(prompt["prompt_id"], call.data[ATTR_ACTION])

    hass.services.async_register(
        DOMAIN,
        "answer_prompt",
        answer_prompt,
        schema=vol.Schema(
            {**_ENTRY, vol.Required(ATTR_PROMPT_ID): str, vol.Required(ATTR_ACTION): str}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "add_plan",
        add_plan,
        schema=vol.Schema(
            {
                **_ENTRY,
                vol.Required(ATTR_ZONE): str,
                vol.Required(ATTR_START_HEIGHT): vol.Coerce(float),
                vol.Required(ATTR_END_HEIGHT): vol.Coerce(float),
                vol.Optional(ATTR_START, default=False): bool,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "start", start, schema=vol.Schema(_ENTRY))
    hass.services.async_register(DOMAIN, "cancel", cancel, schema=vol.Schema(_ENTRY))
    hass.services.async_register(DOMAIN, "retry_run", retry_run, schema=vol.Schema(_ENTRY))
    hass.services.async_register(DOMAIN, "skip_plan", skip_plan, schema=vol.Schema(_ENTRY))
    hass.services.async_register(DOMAIN, "resume_run", resume_run, schema=vol.Schema(_ENTRY))
    hass.services.async_register(DOMAIN, "end_run", end_run, schema=vol.Schema(_ENTRY))
