"""Mowing Director services (spec §13.1-13.3).

Every service takes an optional `config_entry_id` (default: the only loaded
Mower) and raises a translated `ServiceValidationError` where it does not
apply. Registered once in `async_setup`, never per entry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

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

from .const import DOMAIN
from .core import DETECTORS
from . import controls
from .schedule_ops import POSITIONS, ScheduleError, create_plan, remove_plan
from .runtime import Director, async_get_director
from .transfer import async_export

ATTR_ZONE = "zone"
ATTR_START_HEIGHT = "start_height_mm"
ATTR_END_HEIGHT = "end_height_mm"
ATTR_START = "start"
ATTR_MAX_CUT = "max_cut_pct"
ATTR_BASE_ANGLE = "base_angle"
ATTR_FINISHING = "finishing"
ATTR_SETTINGS = "settings"
ATTR_POSITION = "position"
ATTR_PLAN_ID = "plan_id"
ATTR_PROMPT_ID = "prompt_id"
ATTR_ACTION = "action"
ATTR_DETECTOR = "detector"
ATTR_INCLUDE_HISTORY = "include_history"

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
    """Register `add_plan`, the Schedule controls and `answer_prompt`."""

    async def add_plan(call: ServiceCall) -> ServiceResponse:
        director = _director(hass, call)
        data = call.data
        try:
            return await create_plan(
                director,
                zone=data[ATTR_ZONE],
                start_height_mm=data[ATTR_START_HEIGHT],
                end_height_mm=data[ATTR_END_HEIGHT],
                max_cut_pct=data.get(ATTR_MAX_CUT),
                base_angle=data.get(ATTR_BASE_ANGLE),
                finishing=data.get(ATTR_FINISHING),
                settings=data.get(ATTR_SETTINGS),
                position=data[ATTR_POSITION],
                start=data[ATTR_START],
            )
        except ScheduleError as err:
            raise _fail(err.key, **err.placeholders) from None

    async def remove_plan_service(call: ServiceCall) -> None:
        director = _director(hass, call)
        try:
            await remove_plan(director, call.data[ATTR_PLAN_ID])
        except ScheduleError as err:
            raise _fail(err.key, **err.placeholders) from None

    def control(action: Callable[[Director], Awaitable[None]]):
        async def handler(call: ServiceCall) -> None:
            await action(_director(hass, call))

        return handler

    async def answer_prompt(call: ServiceCall) -> None:
        director = _director(hass, call)
        prompt = director.find_prompt(call.data[ATTR_PROMPT_ID])
        if prompt is None:
            raise _fail("prompt_not_pending")
        if call.data[ATTR_ACTION] not in prompt["actions"]:
            raise _fail("prompt_action_invalid", action=call.data[ATTR_ACTION])
        await director.async_answer_prompt(prompt["prompt_id"], call.data[ATTR_ACTION])

    async def dismiss_anomaly(call: ServiceCall) -> None:
        await controls.async_dismiss_anomaly(_director(hass, call), call.data[ATTR_DETECTOR])

    async def export_config(call: ServiceCall) -> ServiceResponse:
        director = _director(hass, call)
        return async_export(director, call.data.get(ATTR_INCLUDE_HISTORY, False))

    hass.services.async_register(
        DOMAIN,
        "export_config",
        export_config,
        schema=vol.Schema({**_ENTRY, vol.Optional(ATTR_INCLUDE_HISTORY): bool}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "dismiss_anomaly",
        dismiss_anomaly,
        schema=vol.Schema({**_ENTRY, vol.Required(ATTR_DETECTOR): vol.In(DETECTORS)}),
    )
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
                vol.Optional(ATTR_MAX_CUT): vol.Coerce(float),
                vol.Optional(ATTR_BASE_ANGLE): vol.Coerce(float),
                vol.Optional(ATTR_FINISHING): bool,
                vol.Optional(ATTR_SETTINGS): dict,
                vol.Optional(ATTR_POSITION, default="end"): vol.In(
                    [p for p in POSITIONS if p != "merge"]
                ),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "remove_plan",
        remove_plan_service,
        schema=vol.Schema({**_ENTRY, vol.Required(ATTR_PLAN_ID): str}),
    )
    for name, action in (
        ("start", controls.async_start),
        ("pause", controls.async_pause),
        ("stop_now", controls.async_stop_now),
        ("resume", controls.async_resume),
        ("cancel", controls.async_cancel),
        ("retry_run", controls.async_retry_run),
        ("skip_plan", controls.async_skip_plan),
        ("resume_run", controls.async_resume_run),
        ("end_run", controls.async_end_run),
        ("wet_grass_done_today", controls.async_wet_grass_done_today),
    ):
        hass.services.async_register(
            DOMAIN, name, control(action), schema=vol.Schema(_ENTRY)
        )
