"""HA integration for Gates: CRUD, entities, Prompt and automation (spec §4.7, §10, §13.5)."""

from dataclasses import replace

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.core import ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.mowing_director.const import DOMAIN, EVENT_TYPE
from tests.fake_mower import DEFAULT_SNAPSHOT
from custom_components.mowing_director.runtime import async_get_director
from tests.test_checks_ha import add_plan, advance, setup, ws  # noqa: F401
from tests.test_prompts import list_prompts

MOWING = replace(DEFAULT_SNAPSHOT, activity=LawnMowerActivity.MOWING, job_active=True,
                 job_ref="j1")


async def channel_to_dock(hass, hass_ws_client, entry):
    msg = await ws(hass, hass_ws_client, entry, "get_zones")
    [ch] = [c for c in msg["result"]["channels"] if "dock" in (c["end_a"], c["end_b"])]
    return ch["id"]


async def make_gate(hass, hass_ws_client, entry, **fields):
    channel = await channel_to_dock(hass, hass_ws_client, entry)
    msg = await ws(hass, hass_ws_client, entry, "create_gate",
                   name="Fence gate", channels=[channel], **fields)
    assert msg["success"], msg
    await hass.async_block_till_done()
    return msg["result"]


def eid(hass, domain, entry, gate, key):
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{entry.entry_id}_gate_{gate['id']}_{key}")


async def test_gate_crud_and_derived_gated_zones(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    gate = await make_gate(hass, hass_ws_client, entry)
    assert gate["name"] == "Fence gate" and gate["notify"] is True
    msg = await ws(hass, hass_ws_client, entry, "list_gates")
    assert [g["id"] for g in msg["result"]["gates"]] == [gate["id"]]
    # The dock opens straight onto the Front Lawn through the gate; the Backyard
    # lies beyond the Front Lawn, so both are behind it.
    assert sorted(z["name"] for z in msg["result"]["gated_zones"]) == ["Backyard", "Front Lawn"]
    assert msg["result"]["unrouted_zones"] == []
    msg = await ws(hass, hass_ws_client, entry, "update_gate", gate_id=gate["id"],
                   changes={"name": "Side gate", "notify": False})
    assert msg["result"]["name"] == "Side gate" and msg["result"]["notify"] is False
    msg = await ws(hass, hass_ws_client, entry, "delete_gate", gate_id=gate["id"])
    assert msg["success"]
    msg = await ws(hass, hass_ws_client, entry, "list_gates")
    assert msg["result"]["gates"] == [] and msg["result"]["gated_zones"] == []


async def test_invalid_gates_are_rejected(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    msg = await ws(hass, hass_ws_client, entry, "create_gate", name="", channels=["nope"])
    assert not msg["success"]
    msg = await ws(hass, hass_ws_client, entry, "update_gate", gate_id="nope", changes={})
    assert not msg["success"]


async def test_state_entity_is_stored_as_a_registry_id(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    reg = er.async_get(hass).async_get_or_create("binary_sensor", "t", "gate_contact",
                                                 suggested_object_id="gate_contact")
    gate = await make_gate(hass, hass_ws_client, entry, state_entity=reg.entity_id)
    assert gate["state_entity"] == reg.id


async def test_each_gate_is_a_device_with_switch_and_two_binary_sensors(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    gate = await make_gate(hass, hass_ws_client, entry)
    devices = dr.async_get(hass)
    mower_device = devices.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    device = devices.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_gate_{gate['id']}"), entry.entry_id)
    assert device.name == "Fence gate" and device.via_device_id == mower_device.id
    switch = eid(hass, "switch", entry, gate, "open_flag")
    believed = eid(hass, "binary_sensor", entry, gate, "open")
    crossing = eid(hass, "binary_sensor", entry, gate, "crossing_expected")
    assert switch and believed and crossing
    assert hass.states.get(believed).state == "off"
    assert hass.states.get(crossing).state == "off"
    # The flag is the believed state when nothing else is linked.
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    assert hass.states.get(switch).state == "on"
    assert hass.states.get(believed).state == "on"
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert hass.states.get(believed).state == "off"


async def test_no_switch_with_a_state_entity_and_believed_follows_it(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    er.async_get(hass).async_get_or_create("binary_sensor", "t", "contact",
                                           suggested_object_id="contact")
    hass.states.async_set("binary_sensor.contact", "off")
    gate = await make_gate(hass, hass_ws_client, entry, state_entity="binary_sensor.contact")
    assert eid(hass, "switch", entry, gate, "open_flag") is None
    believed = eid(hass, "binary_sensor", entry, gate, "open")
    assert hass.states.get(believed).state == "off"
    hass.states.async_set("binary_sensor.contact", "on")
    await hass.async_block_till_done()
    assert hass.states.get(believed).state == "on"


async def test_deleting_a_gate_removes_its_device_and_entities(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    gate = await make_gate(hass, hass_ws_client, entry)
    await ws(hass, hass_ws_client, entry, "delete_gate", gate_id=gate["id"])
    await hass.async_block_till_done()
    assert eid(hass, "switch", entry, gate, "open_flag") is None
    assert eid(hass, "binary_sensor", entry, gate, "open") is None
    assert dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_gate_{gate['id']}"), entry.entry_id) is None


async def test_run_start_holds_for_the_gate_until_the_user_opens_it(
    hass, hass_ws_client, setup, freezer
) -> None:
    entry, mower = setup
    events = async_capture_events(hass, EVENT_TYPE)
    gate = await make_gate(hass, hass_ws_client, entry)
    await add_plan(hass)
    await hass.async_block_till_done()
    crossing = eid(hass, "binary_sensor", entry, gate, "crossing_expected")
    assert hass.states.get(crossing).state == "on"
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    assert prompt["kind"] == "gate" and prompt["subject"] == gate["id"]
    assert prompt["title"] == "Example Mower: Gate closed"
    assert prompt["actions"] == ["opened", "closed", "skip_plan"]
    assert "Gate: Fence gate" in prompt["body"]
    await advance(hass, freezer, 300)
    assert mower.commands == []  # unanswered waits
    await hass.services.async_call(
        DOMAIN, "answer_prompt", {"prompt_id": prompt["prompt_id"], "action": "opened"},
        blocking=True)
    await advance(hass, freezer, 61)
    await advance(hass, freezer, 61)
    assert [c[0] for c in mower.commands] == ["start_run"]
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert hass.states.get(crossing).state == "off"
    assert hass.states.get(eid(hass, "switch", entry, gate, "open_flag")).state == "off"
    kinds = [e.data["type"] for e in events]
    assert "gate_crossing_expected" in kinds and "gate_crossing_ended" in kinds
    [close] = await list_prompts(hass, hass_ws_client, entry)
    assert close["kind"] == "gate_close" and close["title"] == "Example Mower: Through, close Fence gate"
    assert close["actions"] == ["closed"]


async def test_skip_plan_answer_empties_the_schedule(hass, hass_ws_client, setup) -> None:
    entry, mower = setup
    await make_gate(hass, hass_ws_client, entry)
    await add_plan(hass)
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    await hass.services.async_call(
        DOMAIN, "answer_prompt", {"prompt_id": prompt["prompt_id"], "action": "skip_plan"},
        blocking=True)
    assert hass.states.get("sensor.example_mower_schedule").state == "idle"
    assert await list_prompts(hass, hass_ws_client, entry) == []


async def test_open_and_close_actions_run_instead_of_notifying(
    hass, hass_ws_client, setup, freezer
) -> None:
    entry, mower = setup
    calls: list[ServiceCall] = []
    hass.services.async_register("test", "open_gate", lambda c: calls.append(c))
    hass.services.async_register("test", "close_gate", lambda c: calls.append(c))
    await make_gate(hass, hass_ws_client, entry,
                    open_action=[{"action": "test.open_gate", "data": {"which": "fence"}}],
                    close_action=[{"action": "test.close_gate"}])
    await add_plan(hass)
    await hass.async_block_till_done()
    assert [(c.service, dict(c.data)) for c in calls] == [("open_gate", {"which": "fence"})]
    assert await list_prompts(hass, hass_ws_client, entry) == []
    await advance(hass, freezer, 61)
    await advance(hass, freezer, 61)
    assert [c[0] for c in mower.commands] == ["start_run"]
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert [c.service for c in calls] == ["open_gate", "close_gate"]
    assert await list_prompts(hass, hass_ws_client, entry) == []


async def test_open_action_that_does_not_open_the_gate_falls_back_to_notifying(
    hass, hass_ws_client, setup, freezer
) -> None:
    entry, mower = setup
    events = async_capture_events(hass, EVENT_TYPE)
    hass.services.async_register("test", "open_gate", lambda c: None)
    er.async_get(hass).async_get_or_create("binary_sensor", "t", "contact",
                                           suggested_object_id="contact")
    hass.states.async_set("binary_sensor.contact", "off")
    gate = await make_gate(hass, hass_ws_client, entry, state_entity="binary_sensor.contact",
                           open_action=[{"action": "test.open_gate"}])
    await add_plan(hass)
    await hass.async_block_till_done()
    assert await list_prompts(hass, hass_ws_client, entry) == []
    await advance(hass, freezer, 125)
    [failed] = [e.data for e in events if e.data["type"] == "gate_open_action_failed"]
    assert failed["gate_id"] == gate["id"]
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    assert prompt["kind"] == "gate"
    # The contact opening settles the Prompt and releases the Run.
    hass.states.async_set("binary_sensor.contact", "on")
    await hass.async_block_till_done()
    assert await list_prompts(hass, hass_ws_client, entry) == []


async def test_gate_restart_keeps_gates_and_the_waiting_crossing(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    gate = await make_gate(hass, hass_ws_client, entry)
    await add_plan(hass)
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    msg = await ws(hass, hass_ws_client, entry, "list_gates")
    assert [g["id"] for g in msg["result"]["gates"]] == [gate["id"]]
    assert hass.states.get(eid(hass, "binary_sensor", entry, gate, "crossing_expected")).state == "on"


async def test_gate_views_show_the_state_entity_id_for_the_panel(
    hass, hass_ws_client, setup
) -> None:
    """The Store keeps a registry id; the panel needs the entity_id to display it."""
    entry, _ = setup
    reg = er.async_get(hass).async_get_or_create("binary_sensor", "t", "gate_contact2",
                                                 suggested_object_id="gate_contact2")
    await make_gate(hass, hass_ws_client, entry, state_entity=reg.entity_id)
    msg = await ws(hass, hass_ws_client, entry, "get_zones")
    [gate] = msg["result"]["gates"]
    assert gate["state_entity"] == reg.id
    assert gate["state_entity_id"] == reg.entity_id


async def test_get_zones_shows_the_learned_return_and_resume_thresholds(
    hass, hass_ws_client, setup
) -> None:
    """Zones & Gates tab: level, where it came from and how many samples (spec §10)."""
    entry, _ = setup
    director = async_get_director(hass, entry.entry_id)
    msg = await ws(hass, hass_ws_client, entry, "get_zones")
    assert msg["result"]["thresholds"] == {
        "return": {"level": 25, "source": "default", "samples": 0},
        "resume": {"level": 95, "source": "default", "samples": 0},
    }
    director._data.setdefault("learned", {})["battery_levels"] = {
        "return": [30, 20, 26], "resume": [90],
    }
    msg = await ws(hass, hass_ws_client, entry, "get_zones")
    assert msg["result"]["thresholds"]["return"] == {
        "level": 26, "source": "learned", "samples": 3}
    assert msg["result"]["thresholds"]["resume"]["source"] == "default"
    assert msg["result"]["thresholds"]["resume"]["samples"] == 1
