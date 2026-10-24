"""HA integration for Wet grass and "Is it raining?" (spec §9.3, §9.4, §13.2)."""

from dataclasses import replace

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.groundskeeper.runtime import async_get_director
from custom_components.groundskeeper.const import DOMAIN, EVENT_TYPE
from tests.fake_mower import DEFAULT_SNAPSHOT
from tests.test_checks_ha import add_plan, advance, setup, ws  # noqa: F401
from tests.test_prompts import list_prompts

WET = replace(DEFAULT_SNAPSHOT, rain=True)
DRY = replace(DEFAULT_SNAPSHOT, rain=False)
H = 3600


async def configure(hass, hass_ws_client, entry, **config):
    config = {"morning": False, **config}
    msg = await ws(hass, hass_ws_client, entry, "update_check",
                   check_id="wet_grass", changes={"config": config})
    assert msg["success"], msg
    return msg["result"]


def waiting_reasons(hass):
    return [r["reason"] for r in hass.states.get("sensor.example_mower_waiting_on").attributes["reasons"]
            if r["check"] == "wet_grass"]


def register_entity(hass, domain, name, state):
    reg = er.async_get(hass).async_get_or_create(domain, "test", name, suggested_object_id=name)
    hass.states.async_set(reg.entity_id, state)
    return reg


async def test_wet_grass_is_a_built_in_check_with_defaults(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    msg = await ws(hass, hass_ws_client, entry, "list_checks")
    rec = {c["id"]: c for c in msg["result"]["checks"]}["wet_grass"]
    assert (rec["enabled"], rec["timeout_s"], rec["policy"]) == (True, 12 * H, "notify")
    assert rec["config"]["dry_out_s"] == 3 * H
    assert er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_check_wet_grass") is not None


async def test_sources_are_stored_as_registry_ids_and_block_until_dry_out(
    hass, hass_ws_client, setup, freezer
) -> None:
    entry, mower = setup
    sensor = register_entity(hass, "binary_sensor", "rain_gauge", "on")
    weather = register_entity(hass, "weather", "home", "cloudy")
    result = await configure(
        hass, hass_ws_client, entry,
        sensors=["binary_sensor.rain_gauge"], weather=["weather.home"])
    assert result["config"]["sensors"] == [sensor.id]
    assert result["config"]["weather"] == [weather.id]
    await add_plan(hass)
    await hass.async_block_till_done()
    assert waiting_reasons(hass) == ["sensor_wet"]
    hass.states.async_set("weather.home", "pouring")
    await hass.async_block_till_done()
    assert waiting_reasons(hass) == ["weather_wet", "sensor_wet"]
    hass.states.async_set("weather.home", "cloudy")
    hass.states.async_set("binary_sensor.rain_gauge", "off")
    await hass.async_block_till_done()
    assert waiting_reasons(hass) == ["dry_out"]
    await advance(hass, freezer, 3 * H - 5)
    assert mower.commands == []
    await advance(hass, freezer, 10)
    await advance(hass, freezer, 61)  # tick sees it dry and opens the stability window
    await advance(hass, freezer, 61)
    assert [c[0] for c in mower.commands] == ["start_run"]


async def test_mower_flag_asks_and_no_answer_passes_the_check(
    hass, hass_ws_client, setup, freezer
) -> None:
    entry, mower = setup
    await configure(hass, hass_ws_client, entry)
    await add_plan(hass)
    mower.emit_snapshot(WET)
    await hass.async_block_till_done()
    assert waiting_reasons(hass) == ["mower_rain"]
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    assert prompt["kind"] == "is_raining"
    assert prompt["title"] == "Example Mower: Is it raining?"
    assert prompt["actions"] == ["no", "yes", "yes_done_today"]
    await hass.services.async_call(
        DOMAIN, "answer_prompt",
        {"prompt_id": prompt["prompt_id"], "action": "no"}, blocking=True)
    assert waiting_reasons(hass) == []
    await advance(hass, freezer, 61)
    assert [c[0] for c in mower.commands] == ["start_run"]


async def test_dry_rain_sensor_does_not_count_as_false_alarm_so_it_asks(
    hass, hass_ws_client, setup
) -> None:
    entry, mower = setup
    register_entity(hass, "binary_sensor", "rain_gauge", "off")
    await configure(hass, hass_ws_client, entry, sensors=["binary_sensor.rain_gauge"])
    await add_plan(hass)
    mower.emit_snapshot(WET)
    await hass.async_block_till_done()
    assert len(await list_prompts(hass, hass_ws_client, entry)) == 1


async def test_wet_rain_sensor_confirms_without_asking(hass, hass_ws_client, setup) -> None:
    entry, mower = setup
    register_entity(hass, "binary_sensor", "rain_gauge", "on")
    await configure(hass, hass_ws_client, entry, sensors=["binary_sensor.rain_gauge"])
    await add_plan(hass)
    mower.emit_snapshot(WET)
    await hass.async_block_till_done()
    assert await list_prompts(hass, hass_ws_client, entry) == []


@pytest.mark.parametrize(
    ("hour", "probability", "asks"),
    [({"condition": "rainy"}, None, False),
     ({"condition": "cloudy", "precipitation_probability": 50}, None, False),
     ({"condition": "cloudy", "precipitation_probability": 20}, None, True)],
)
async def test_forecast_confirms_rain_from_current_or_next_hour(
    hass, hass_ws_client, setup, hour, probability, asks
) -> None:
    entry, mower = setup
    register_entity(hass, "weather", "home", "cloudy")
    calls = []

    async def get_forecasts(call):
        calls.append(call.data)
        return {"weather.home": {"forecast": [
            {"datetime": "2026-10-21T12:00:00+00:00", "condition": "cloudy"}, hour]}}

    hass.services.async_register(
        "weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)
    await configure(hass, hass_ws_client, entry, weather=["weather.home"])
    await add_plan(hass)
    mower.emit_snapshot(WET)
    await hass.async_block_till_done()
    assert calls and calls[0]["type"] == "hourly"
    assert bool(await list_prompts(hass, hass_ws_client, entry)) is asks


async def test_unavailable_weather_is_skipped_not_wet(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    register_entity(hass, "weather", "home", "unavailable")
    await configure(hass, hass_ws_client, entry, weather=["weather.home"])
    await add_plan(hass)
    await hass.async_block_till_done()
    assert waiting_reasons(hass) == []


async def test_done_for_today_service_blocks_until_next_work_start(
    hass, hass_ws_client, setup, freezer
) -> None:
    entry, mower = setup
    await configure(hass, hass_ws_client, entry)
    await add_plan(hass)
    mower.emit_snapshot(WET)
    await hass.async_block_till_done()
    events = async_capture_events(hass, EVENT_TYPE)
    await hass.services.async_call(DOMAIN, "wet_grass_done_today", {}, blocking=True)
    mower.emit_snapshot(DRY)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.example_mower_pending_prompts").state == "0"
    assert [e.data["action"] for e in events if e.data["type"] == "prompt_answered"] == [
        "yes_done_today"]
    assert "done_for_today" in waiting_reasons(hass)
    assert hass.states.get("sensor.example_mower_schedule").state == "waiting"


async def test_morning_dry_out_until_sunrise_plus_two_hours(
    hass, hass_ws_client, setup, freezer
) -> None:
    entry, mower = setup
    # hass default location is San Diego: 12:00 UTC is 05:00 local, before sunrise
    await configure(hass, hass_ws_client, entry, morning=True)
    freezer.move_to("2026-10-21 12:00:00+00:00")
    await add_plan(hass)
    await hass.async_block_till_done()
    assert waiting_reasons(hass) == ["morning_dry_out"]
    freezer.move_to("2026-10-21 17:00:00+00:00")
    await advance(hass, freezer, 61)
    assert waiting_reasons(hass) == []


async def test_category_toggle_off_means_no_ask(hass, hass_ws_client, setup) -> None:
    entry, mower = setup
    await configure(hass, hass_ws_client, entry)
    async_get_director(hass, entry.entry_id).data["config"]["notifications"] = {"is_raining": False}
    await add_plan(hass)
    mower.emit_snapshot(WET)
    await hass.async_block_till_done()
    assert await list_prompts(hass, hass_ws_client, entry) == []
    assert waiting_reasons(hass) == ["mower_rain"]
