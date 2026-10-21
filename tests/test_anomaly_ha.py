"""HA integration for Anomaly detectors (spec §11, §12.3, §13.2, §13.4, §13.5)."""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.mowing_director.const import DOMAIN, EVENT_TYPE
from tests.fake_mower import DEFAULT_SNAPSHOT
from tests.test_checks_ha import advance, setup  # noqa: F401

SENSOR = "binary_sensor.example_mower_anomaly"
H = 3600


class Panel:
    """Websocket calls with a day-long token minted before the clock jumps."""

    def __init__(self, hass, hass_ws_client, token, entry):
        self.hass, self._factory, self.token, self.entry = hass, hass_ws_client, token, entry

    async def call(self, type_, **payload):
        client = await self._factory(self.hass, self.token)
        await client.send_json({"id": 1, "type": f"{DOMAIN}/{type_}",
                                "entry_id": self.entry.entry_id, **payload})
        return await client.receive_json()

    async def prompts(self):
        return (await self.call("list_prompts"))["result"]["prompts"]


async def panel(hass, hass_ws_client, hass_admin_user, entry):
    refresh = await hass.auth.async_create_refresh_token(
        hass_admin_user, client_id="https://example.com/", access_token_expiration=timedelta(days=2))
    return Panel(hass, hass_ws_client, hass.auth.async_create_access_token(refresh), entry)


def types(events, type_):
    return [e.data for e in events if e.data["type"] == type_]


async def test_anomaly_binary_sensor_is_a_problem_sensor_per_mower(hass, setup) -> None:
    entry, _ = setup
    state = hass.states.get(SENSOR)
    assert state.state == "off"
    assert state.attributes["device_class"] == "problem"
    assert state.attributes["active"] == []
    assert er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_anomaly") == SENSOR


async def test_charge_not_rising_emits_prompt_and_turns_the_sensor_on(
    hass, hass_ws_client, hass_admin_user, setup, freezer
) -> None:
    entry, mower = setup
    p = await panel(hass, hass_ws_client, hass_admin_user, entry)
    events = async_capture_events(hass, EVENT_TYPE)
    await advance(hass, freezer, 2 * H - 60)
    assert hass.states.get(SENSOR).state == "off"
    await advance(hass, freezer, 60)
    [detected] = types(events, "anomaly_detected")
    assert detected["detector"] == "charge_not_rising"
    assert hass.states.get(SENSOR).state == "on"
    [active] = hass.states.get(SENSOR).attributes["active"]
    assert active["detector"] == "charge_not_rising" and active["dismissed"] is False
    [prompt] = await p.prompts()
    assert prompt["kind"] == "anomaly" and prompt["actions"] == ["dismiss"]
    assert prompt["title"] == "Example Mower: Anomaly detected"
    assert prompt["body"] == "The battery has been at 80 % for 120 min while docked."
    assert mower.commands == []  # never touches the Mower
    # a rising battery clears it with `anomaly_cleared` only
    mower.emit_snapshot(replace(DEFAULT_SNAPSHOT, battery=81))
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "off"
    assert [e["detector"] for e in types(events, "anomaly_cleared")] == ["charge_not_rising"]
    assert len(types(events, "anomaly_detected")) == 1


async def test_dismiss_anomaly_service_and_its_errors(hass, hass_ws_client, hass_admin_user, setup, freezer) -> None:
    entry, _ = setup
    p = await panel(hass, hass_ws_client, hass_admin_user, entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "dismiss_anomaly", {"detector": "charge_not_rising"}, blocking=True)
    await advance(hass, freezer, 2 * H + 1)
    await hass.services.async_call(
        DOMAIN, "dismiss_anomaly", {"detector": "charge_not_rising"}, blocking=True)
    assert await p.prompts() == []
    assert hass.states.get(SENSOR).attributes["active"][0]["dismissed"] is True
    await advance(hass, freezer, 5 * H)
    assert len(hass.states.get(SENSOR).attributes["active"]) == 1


async def test_dismiss_through_the_panel_command(hass, hass_ws_client, hass_admin_user, setup, freezer) -> None:
    entry, _ = setup
    p = await panel(hass, hass_ws_client, hass_admin_user, entry)
    await advance(hass, freezer, 2 * H + 1)
    msg = await p.call("dismiss_anomaly", detector="charge_not_rising")
    assert msg["success"], msg
    assert hass.states.get(SENSOR).attributes["active"][0]["dismissed"] is True


async def test_detector_action_is_configurable_and_off_detects_nothing(
    hass, hass_ws_client, hass_admin_user, setup, freezer
) -> None:
    entry, _ = setup
    p = await panel(hass, hass_ws_client, hass_admin_user, entry)
    msg = await p.call("set_anomaly_action", detector="charge_not_rising", action="off")
    assert msg["success"], msg
    events = async_capture_events(hass, EVENT_TYPE)
    await advance(hass, freezer, 3 * H)
    assert types(events, "anomaly_detected") == []
    assert hass.states.get(SENSOR).state == "off"
    bad = await p.call("set_anomaly_action", detector="charge_not_rising", action="explode")
    assert not bad["success"]


async def test_restart_keeps_active_anomalies_without_notifying_again(
    hass, setup, freezer
) -> None:
    entry, _ = setup
    await advance(hass, freezer, 2 * H + 1)
    await hass.services.async_call(
        DOMAIN, "dismiss_anomaly", {"detector": "charge_not_rising"}, blocking=True)
    events = async_capture_events(hass, EVENT_TYPE)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert types(events, "anomaly_detected") == []
    assert types(events, "prompt_sent") == []
    [active] = hass.states.get(SENSOR).attributes["active"]
    assert active["dismissed"] is True
