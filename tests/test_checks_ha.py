"""HA integration for Checks: wiring, entities, websocket CRUD (spec §9, §13.5)."""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_DEVICE
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.mowing_director import adapters
from custom_components.mowing_director.const import DOMAIN, EVENT_TYPE
from tests.conftest import make_mammotion_mower
from tests.fake_mower import DEFAULT_SNAPSHOT, FakeMower


@pytest.fixture
async def setup(hass, monkeypatch, freezer):
    mower = FakeMower()
    monkeypatch.setitem(adapters.ADAPTER_REGISTRY, "mammotion", lambda h, e: mower)
    _, device, _ = await make_mammotion_mower(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DEVICE: device.id})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    entry = result["result"]
    await hass.async_block_till_done()
    # Work hours are on by default; keep the tests independent of the wall clock.
    data = await entry.runtime_data.async_data()
    data["config"]["checks"] = [{"id": "work_hours", "enabled": False}]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return entry, mower


async def advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def ws(hass, hass_ws_client, entry, type_, **payload):
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/{type_}",
                            "entry_id": entry.entry_id, **payload})
    return await client.receive_json()


async def add_plan(hass):
    await hass.services.async_call(
        DOMAIN, "add_plan",
        {"zone": "front lawn", "start_height_mm": 70, "end_height_mm": 70, "start": True},
        blocking=True, return_response=True)


def entity_for(hass, entry, check_id):
    return er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_check_{check_id}")


ENTITY_CHECK = {"name": "Gate shut", "enabled": True, "timeout_s": 600, "policy": "pause",
                "config": {"mode": "entity", "entity": "input_boolean.gate", "operator": "is",
                           "value": "on"}}


async def test_list_checks_returns_the_built_ins(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    msg = await ws(hass, hass_ws_client, entry, "list_checks")
    by_id = {c["id"]: c for c in msg["result"]["checks"]}
    assert set(by_id) == {"mower_ready", "zone_ready", "battery", "work_hours", "wet_grass", "gate_open"}
    assert by_id["battery"]["enabled"] is False


async def test_every_check_has_a_binary_sensor_and_waiting_on_sensor_explains(
    hass, setup, freezer
) -> None:
    entry, mower = setup
    for check_id in ("mower_ready", "zone_ready", "battery", "work_hours", "wet_grass"):
        assert entity_for(hass, entry, check_id) is not None
    mower.emit_snapshot(replace(DEFAULT_SNAPSHOT, job_active=True))
    await add_plan(hass)
    await hass.async_block_till_done()
    waiting = hass.states.get("sensor.example_mower_waiting_on")
    assert waiting.state == "1"
    assert waiting.attributes["reasons"] == [
        {"check": "mower_ready", "name": "Mower ready", "reason": "job_active"}]
    assert hass.states.get(entity_for(hass, entry, "mower_ready")).state == "off"


async def test_custom_check_blocks_start_until_condition_holds(
    hass, hass_ws_client, setup, freezer
) -> None:
    entry, mower = setup
    hass.states.async_set("input_boolean.gate", "off")
    msg = await ws(hass, hass_ws_client, entry, "create_check", **ENTITY_CHECK)
    assert msg["success"], msg
    check_id = msg["result"]["id"]
    assert hass.states.get(entity_for(hass, entry, check_id)) is not None
    await add_plan(hass)
    await advance(hass, freezer, 120)
    assert mower.commands == []
    hass.states.async_set("input_boolean.gate", "on")
    await hass.async_block_till_done()
    assert mower.commands == []  # stability window runs from now
    await advance(hass, freezer, 61)
    assert [c[0] for c in mower.commands] == ["start_run"]


async def test_unavailable_entity_fails_the_check(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    hass.states.async_set("input_boolean.gate", "unavailable")
    await ws(hass, hass_ws_client, entry, "create_check", **ENTITY_CHECK)
    await add_plan(hass)
    await hass.async_block_till_done()
    reasons = hass.states.get("sensor.example_mower_waiting_on").attributes["reasons"]
    assert [r["reason"] for r in reasons] == ["unavailable"]


async def test_picked_entity_is_stored_as_registry_id(hass, hass_ws_client, setup, hass_storage) -> None:
    entry, mower = setup
    reg = er.async_get(hass).async_get_or_create("sensor", "test", "soil", suggested_object_id="soil")
    hass.states.async_set("sensor.soil", "30")
    msg = await ws(hass, hass_ws_client, entry, "create_check", name="Dry",
                   enabled=True, timeout_s=None, policy="notify",
                   config={"mode": "entity", "entity": "sensor.soil", "operator": "above",
                           "value": "50"})
    assert msg["result"]["config"]["entity"] == reg.id
    await add_plan(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.example_mower_waiting_on").state == "1"
    # a rename keeps the Check working because it follows the registry id
    er.async_get(hass).async_update_entity("sensor.soil", new_entity_id="sensor.soil_moisture")
    hass.states.async_set("sensor.soil_moisture", "60")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.example_mower_waiting_on").state == "0"


async def test_template_check_is_validated_on_save_and_evaluated(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    bad = await ws(hass, hass_ws_client, entry, "create_check", name="Bad",
                   enabled=True, timeout_s=None, policy="notify",
                   config={"mode": "template", "template": "{{ states('x') | nope }}"})
    assert not bad["success"]
    hass.states.async_set("input_boolean.flag", "off")
    ok = await ws(hass, hass_ws_client, entry, "create_check", name="Flag",
                  enabled=True, timeout_s=None, policy="notify",
                  config={"mode": "template",
                          "template": "{{ is_state('input_boolean.flag', 'on') }}"})
    assert ok["success"], ok
    await add_plan(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.example_mower_waiting_on").state == "1"
    hass.states.async_set("input_boolean.flag", "on")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.example_mower_waiting_on").state == "0"


async def test_template_render_error_fails_the_check(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    await ws(hass, hass_ws_client, entry, "create_check", name="Boom",
             enabled=True, timeout_s=None, policy="notify",
             config={"mode": "template", "template": "{{ 1 / (states('input_number.z') | int(0)) }}"})
    await add_plan(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.example_mower_waiting_on").state == "1"


async def test_pause_policy_timeout_pauses_schedule_with_reason_and_event(
    hass, hass_ws_client, setup, freezer
) -> None:
    entry, mower = setup
    events = async_capture_events(hass, EVENT_TYPE)
    hass.states.async_set("input_boolean.gate", "off")
    await ws(hass, hass_ws_client, entry, "create_check", **ENTITY_CHECK)
    await add_plan(hass)
    await advance(hass, freezer, 601)
    assert hass.states.get("sensor.example_mower_schedule").state == "paused"
    assert hass.states.get("sensor.example_mower_schedule").attributes["paused_reason"] == (
        "Check Gate shut timed out")
    assert [e for e in events if e.data["type"] == "check_timeout"]


async def test_delete_check_removes_its_entity_and_edit_applies(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    hass.states.async_set("input_boolean.gate", "off")
    msg = await ws(hass, hass_ws_client, entry, "create_check", **ENTITY_CHECK)
    check_id = msg["result"]["id"]
    entity_id = entity_for(hass, entry, check_id)
    upd = await ws(hass, hass_ws_client, entry, "update_check", check_id=check_id,
                   changes={"enabled": False})
    assert upd["success"] and upd["result"]["enabled"] is False
    gone = await ws(hass, hass_ws_client, entry, "delete_check", check_id=check_id)
    assert gone["success"]
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None
    assert entity_for(hass, entry, check_id) is None


async def test_built_in_checks_cannot_be_deleted_or_always_on_disabled(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    assert not (await ws(hass, hass_ws_client, entry, "delete_check", check_id="zone_ready"))["success"]
    assert not (await ws(hass, hass_ws_client, entry, "update_check", check_id="mower_ready",
                         changes={"enabled": False}))["success"]
    ok = await ws(hass, hass_ws_client, entry, "update_check", check_id="battery",
                  changes={"enabled": True, "config": {"threshold_pct": 60}})
    assert ok["success"]


async def test_checks_persist_in_the_store(hass, hass_ws_client, setup, hass_storage) -> None:
    entry, _ = setup
    await ws(hass, hass_ws_client, entry, "create_check", **ENTITY_CHECK)
    stored = hass_storage[f"mowing_director.{entry.entry_id}"]["data"]["config"]["checks"]
    assert [c["name"] for c in stored if c["type"] == "custom"] == ["Gate shut"]


def test_logbook_describes_check_timeout() -> None:
    from custom_components.mowing_director.logbook import describe

    text = describe({"type": "check_timeout", "name": "Gate shut", "policy": "pause"})
    assert text == "Check Gate shut timed out (pause)"
