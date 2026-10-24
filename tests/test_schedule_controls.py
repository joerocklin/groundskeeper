"""HA integration tests: Schedule controls via services and buttons (spec §6.3, §13.2)."""

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.exceptions import ServiceValidationError

from custom_components.groundskeeper.adapter import Refused, ZoneTaskComplete
from custom_components.groundskeeper.const import DOMAIN
from tests.fake_mower import DEFAULT_SNAPSHOT
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


async def start_mowing(hass, mower, freezer, **extra):
    await add_plan(hass, start=True, **extra)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.example_mower_current_run").state == "mowing"


async def test_pause_while_waiting_pauses_at_once_without_commands(hass, director) -> None:
    _, mower, _ = director
    await add_plan(hass, start=True)
    await call(hass, "pause")
    assert schedule(hass).state == "paused"
    assert schedule(hass).attributes["pause_requested"] is True
    assert mower.commands == []


async def test_pause_while_running_is_soft_and_takes_effect_when_run_ends(
    hass, director, freezer
) -> None:
    _, mower, _ = director
    await add_plan(hass)  # a second Run so the Schedule is not empty afterwards
    await start_mowing(hass, mower, freezer)
    await call(hass, "pause")
    assert schedule(hass).state == "running"
    assert schedule(hass).attributes["pause_requested"] is True
    assert [c[0] for c in mower.commands] == ["start_run"]  # no command from pause
    mower.emit_signal(ZoneTaskComplete("11111111"))
    await hass.async_block_till_done()
    assert schedule(hass).state == "paused"
    assert [c[0] for c in mower.commands] == ["start_run"]


async def test_stop_now_returns_run_to_head_pauses_and_aborts(hass, director, freezer) -> None:
    _, mower, _ = director
    await start_mowing(hass, mower, freezer)
    await call(hass, "stop_now")
    assert [c[0] for c in mower.commands] == ["start_run", "abort_run"]
    assert schedule(hass).state == "paused"
    assert state_of(hass, "sensor.example_mower_current_run").state == "none"
    assert state_of(hass, "sensor.example_mower_next_run").state == "Front Lawn"


async def test_stop_now_from_starting(hass, director, freezer) -> None:
    _, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    assert state_of(hass, "sensor.example_mower_current_run").state == "starting"
    await call(hass, "stop_now")
    assert [c[0] for c in mower.commands] == ["start_run", "abort_run"]
    assert schedule(hass).state == "paused"
    assert state_of(hass, "sensor.example_mower_next_run").state == "Front Lawn"


async def test_stop_now_from_on_hold(hass, director, freezer) -> None:
    entry, mower, _ = director
    await start_mowing(hass, mower, freezer)
    d = hass.data[DOMAIN]["director"][entry.entry_id]
    d.state["runs"][0]["status"] = "on_hold"  # on hold is a later ticket's trigger
    await call(hass, "stop_now")
    assert [c[0] for c in mower.commands] == ["start_run", "abort_run"]
    assert schedule(hass).state == "paused"


async def test_resume_and_start_from_paused_clear_pause_requested(hass, director) -> None:
    await add_plan(hass, start=True)
    for service in ("resume", "start"):
        await call(hass, "pause")
        assert schedule(hass).state == "paused"
        await call(hass, service)
        assert schedule(hass).state == "waiting"
        assert schedule(hass).attributes["pause_requested"] is False


async def test_resume_after_stop_now_mows_the_same_run_again(hass, director, freezer) -> None:
    _, mower, _ = director
    await start_mowing(hass, mower, freezer)
    await call(hass, "stop_now")
    mower.emit_snapshot(DEFAULT_SNAPSHOT)
    await hass.async_block_till_done()
    await call(hass, "resume")
    await advance(hass, freezer, 61)
    assert [c[0] for c in mower.commands] == ["start_run", "abort_run", "start_run"]


@pytest.mark.parametrize(
    "service", ["pause", "stop_now", "resume", "retry_run", "skip_plan"]
)
async def test_controls_raise_when_they_do_not_apply(hass, director, service) -> None:
    with pytest.raises(ServiceValidationError):
        await call(hass, service)


async def test_resume_refused_while_failed_run_awaits_answer(hass, director, freezer) -> None:
    _, mower, _ = director
    mower.script_result("start_run", Refused("nope"))
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    assert schedule(hass).state == "paused"
    for service in ("resume", "start"):
        with pytest.raises(ServiceValidationError):
            await call(hass, service)


async def test_retry_with_pause_requested_stays_paused(hass, director, freezer) -> None:
    entry, mower, _ = director
    d = hass.data[DOMAIN]["director"][entry.entry_id]
    mower.script_result("start_run", Refused("nope"))
    await add_plan(hass, start=True)
    await add_plan(hass)
    await advance(hass, freezer, 61)
    d.state["schedule"]["pause_requested"] = True
    await call(hass, "retry_run")
    assert schedule(hass).state == "paused"
    assert state_of(hass, "sensor.example_mower_next_run").state == "Front Lawn"


async def test_skip_plan_with_pause_requested_stays_paused(hass, director, freezer) -> None:
    entry, mower, _ = director
    d = hass.data[DOMAIN]["director"][entry.entry_id]
    mower.script_result("start_run", Refused("nope"))
    await add_plan(hass, start=True)
    await add_plan(hass)
    await advance(hass, freezer, 61)
    d.state["schedule"]["pause_requested"] = True
    await call(hass, "skip_plan")
    assert schedule(hass).state == "paused"
    assert schedule(hass).attributes["runs_remaining"] == 1


async def test_skip_plan_without_pause_requested_waits_for_next_plan(
    hass, director, freezer
) -> None:
    _, mower, _ = director
    mower.script_result("start_run", Refused("nope"))
    await add_plan(hass, start=True)
    await add_plan(hass)
    await advance(hass, freezer, 61)
    await call(hass, "skip_plan")
    assert schedule(hass).state == "waiting"


async def test_buttons_exist_and_drive_the_schedule(hass, director, freezer) -> None:
    _, mower, _ = director
    for name in ("start", "pause", "resume", "stop_now", "cancel"):
        assert state_of(hass, f"button.example_mower_{name}") is not None
    await add_plan(hass)
    await press(hass, "start")
    assert schedule(hass).state == "waiting"
    await press(hass, "pause")
    assert schedule(hass).state == "paused"
    await press(hass, "resume")
    assert schedule(hass).state == "waiting"
    await advance(hass, freezer, 61)
    await press(hass, "stop_now")
    assert [c[0] for c in mower.commands] == ["start_run", "abort_run"]
    assert schedule(hass).state == "paused"
    await press(hass, "cancel")
    assert schedule(hass).state == "idle"


async def test_button_press_raises_when_not_applicable(hass, director) -> None:
    with pytest.raises(ServiceValidationError):
        await press(hass, "pause")


async def test_deleting_entry_with_active_run_sends_abort_run(hass, director, freezer) -> None:
    entry, mower, _ = director
    await start_mowing(hass, mower, freezer)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert [c[0] for c in mower.commands] == ["start_run", "abort_run"]


async def test_deleting_idle_entry_never_commands_the_mower(hass, director) -> None:
    entry, mower, _ = director
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert mower.commands == []


async def test_reload_while_running_still_never_commands(hass, director, freezer) -> None:
    entry, mower, _ = director
    await start_mowing(hass, mower, freezer)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert [c[0] for c in mower.commands] == ["start_run"]
