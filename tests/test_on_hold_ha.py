"""HA integration tests for on hold: services, Prompt, sensor, rain stop (#18)."""

from dataclasses import replace

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.mowing_director.adapter import RainStop
from custom_components.mowing_director.const import DOMAIN, EVENT_TYPE
from tests.test_prompts import list_prompts  # noqa: F401
from tests.test_tracer_bullet import (  # noqa: F401
    MOWING,
    add_plan,
    advance,
    director,
    state_of,
)

PAUSED = replace(MOWING, activity=LawnMowerActivity.PAUSED)
CURRENT = "sensor.example_mower_current_run"


async def hold_run(hass, mower, freezer):
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    mower.emit_snapshot(PAUSED)
    await hass.async_block_till_done()
    await advance(hass, freezer, 61)
    assert state_of(hass, CURRENT).state == "on_hold"


async def test_on_hold_reason_and_prompt_are_exposed(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    await hold_run(hass, mower, freezer)
    assert state_of(hass, CURRENT).attributes["on_hold_reason"] == "external_pause"
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    assert prompt["kind"] == "run_on_hold"
    assert prompt["actions"] == ["resume", "end_run"]
    assert prompt["title"] == "Example Mower: Run on hold"
    assert prompt["body"].splitlines() == [
        "Zone: Front Lawn",
        "Plan: 70 mm",
        "Run 1 of 1 at 70 mm",
        "The Mower was paused from outside Mowing Director.",
    ]


async def test_resume_run_service_sends_resume_and_run_stays_on_hold(
    hass, director, freezer
) -> None:
    _, mower, _ = director
    await hold_run(hass, mower, freezer)
    await hass.services.async_call(DOMAIN, "resume_run", {}, blocking=True)
    assert [c[0] for c in mower.commands][-1] == "resume_run"
    assert state_of(hass, CURRENT).state == "on_hold"
    assert state_of(hass, "sensor.example_mower_pending_prompts").state == "0"


async def test_end_run_service_aborts_and_fails_the_run(hass, director, freezer) -> None:
    _, mower, _ = director
    events = async_capture_events(hass, EVENT_TYPE)
    await hold_run(hass, mower, freezer)
    await hass.services.async_call(DOMAIN, "end_run", {}, blocking=True)
    assert [c[0] for c in mower.commands][-1] == "abort_run"
    assert state_of(hass, "sensor.example_mower_schedule").state == "paused"
    failed = [e.data for e in events if e.data["type"] == "run_status_changed"][-1]
    assert (failed["to"], failed["reason"]) == ("failed", "ended_by_user")


async def test_hold_services_raise_when_no_run_is_on_hold(hass, director) -> None:
    for service in ("resume_run", "end_run"):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(DOMAIN, service, {}, blocking=True)
        assert err.value.translation_key == "no_hold_to_answer"


async def test_answer_prompt_resume_goes_through_the_same_path(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    await hold_run(hass, mower, freezer)
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    await hass.services.async_call(
        DOMAIN,
        "answer_prompt",
        {"prompt_id": prompt["prompt_id"], "action": "resume"},
        blocking=True,
    )
    assert [c[0] for c in mower.commands][-1] == "resume_run"


async def test_rain_stop_returns_run_pending_with_no_command(hass, director, freezer) -> None:
    _, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    before = list(mower.commands)
    mower.emit_signal(RainStop())
    await hass.async_block_till_done()
    assert mower.commands == before
    assert state_of(hass, CURRENT).state == "none"
    assert state_of(hass, "sensor.example_mower_schedule").state == "waiting"
    assert state_of(hass, "sensor.example_mower_next_run").state == "Front Lawn"
    # nothing confirms the rain, so the Wet grass flow asks (spec §7.5, §9.4)
    assert state_of(hass, "sensor.example_mower_pending_prompts").state == "1"
