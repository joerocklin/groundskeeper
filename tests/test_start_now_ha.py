"""HA integration: the Start now service and button (issue #99)."""

from dataclasses import replace

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.exceptions import ServiceValidationError

from custom_components.groundskeeper.const import DOMAIN
from tests.test_tracer_bullet import (  # noqa: F401 - `director` is a fixture
    MOWING,
    add_plan,
    advance,
    director,
    state_of,
)


def schedule(hass):
    return state_of(hass, "sensor.example_mower_schedule")


async def call(hass, service, **data):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True)


async def press(hass, name):
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {"entity_id": f"button.example_mower_{name}"}, blocking=True
    )


def commands(mower):
    return [c[0] for c in mower.commands]


async def test_service_skips_the_window_and_sends_exactly_one_start_command(
    hass, director, freezer
) -> None:
    _, mower, _ = director
    await add_plan(hass, start=True)
    assert commands(mower) == []
    await call(hass, "start_now")
    assert commands(mower) == ["start_run"]
    assert schedule(hass).state == "running"
    assert state_of(hass, "sensor.example_mower_current_run").state == "starting"
    # The window's own timer would have fired here; it must not send a second one.
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert commands(mower) == ["start_run"]
    assert state_of(hass, "sensor.example_mower_current_run").state == "mowing"


async def test_service_writes_the_intent_before_the_command(hass, director) -> None:
    _, mower, _ = director
    await add_plan(hass, start=True)
    await call(hass, "start_now")
    # RecordingMower reads the Store at the moment start_run is called.
    assert mower.seen_at_start["intent"]["command"] == "start_run"
    assert mower.seen_at_start["status"] == "running"


async def test_button_does_the_same_as_the_service(hass, director) -> None:
    _, mower, _ = director
    assert state_of(hass, "button.example_mower_start_now") is not None
    await add_plan(hass, start=True)
    await press(hass, "start_now")
    assert commands(mower) == ["start_run"]
    assert schedule(hass).state == "running"


async def test_a_failing_check_refuses_with_the_reason_and_sends_nothing(
    hass, director
) -> None:
    _, mower, _ = director
    mower.emit_snapshot(replace(mower.snapshot, battery=1))
    await add_plan(hass, start=True)
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "start_now")
    assert raised.value.translation_key == "start_now_checks_failing"
    assert "Mower ready" in raised.value.translation_placeholders["checks"]
    assert "battery low" in raised.value.translation_placeholders["checks"]
    with pytest.raises(ServiceValidationError):
        await press(hass, "start_now")
    assert commands(mower) == []
    assert schedule(hass).state == "waiting"


async def test_a_closed_gate_is_named_with_its_own_name_intact(hass, director) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    d = hass.data[DOMAIN]["director"][entry.entry_id]
    d.state["waiting_on"] = [{"check": "gate_open", "reason": "gate_closed:Fence_gate"}]
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "start_now")
    assert raised.value.translation_key == "start_now_checks_failing"
    assert raised.value.translation_placeholders["checks"] == (
        "Gate open (gate closed: Fence_gate)")
    assert commands(mower) == []


async def test_a_check_that_failed_since_the_last_evaluation_still_refuses(
    hass, director
) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    # The window is open; the Mower now reads busy but no event has reached the
    # reducer yet, so the last evaluation still says every Check passes.
    d = hass.data[DOMAIN]["director"][entry.entry_id]
    d.state["snapshot"]["job_active"] = True
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "start_now")
    assert raised.value.translation_key == "start_now_checks_failing"
    assert commands(mower) == []
    assert schedule(hass).state == "waiting"


@pytest.mark.parametrize("prepare", ["idle", "paused", "running"])
async def test_it_refuses_when_the_schedule_is_not_waiting(
    hass, director, freezer, prepare
) -> None:
    _, mower, _ = director
    if prepare != "idle":
        await add_plan(hass, start=True)
    if prepare == "paused":
        await call(hass, "pause")
    if prepare == "running":
        await advance(hass, freezer, 61)
        mower.emit_snapshot(MOWING)
        await hass.async_block_till_done()
    before = commands(mower)
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "start_now")
    assert raised.value.translation_key == "start_now_not_waiting"
    assert commands(mower) == before


async def test_it_refuses_when_no_window_is_open(hass, director) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    d = hass.data[DOMAIN]["director"][entry.entry_id]
    del d.state["deadlines"]["stability"]
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, "start_now")
    assert raised.value.translation_key == "start_now_window_closed"
    assert commands(mower) == []
