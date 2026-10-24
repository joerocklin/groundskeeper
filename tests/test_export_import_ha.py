"""HA integration: export, import with undo, Broken references (#33, spec §14.8, §4.2)."""

import pytest
from homeassistant.core import ServiceCall  # noqa: F401
from homeassistant.exceptions import HomeAssistantError  # noqa: F401
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.groundskeeper.const import DOMAIN, EVENT_TYPE
from custom_components.groundskeeper.runtime import async_get_director
from tests.test_checks_ha import add_plan, setup, ws  # noqa: F401
from tests.test_gates_ha import make_gate


def broken_issue(hass, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"broken_references_{entry.entry_id}")


async def export(hass, entry, **kw):
    return await hass.services.async_call(
        DOMAIN, "export_config", {"config_entry_id": entry.entry_id, **kw},
        blocking=True, return_response=True)


async def gate_with_entity(hass, hass_ws_client, entry):
    reg = er.async_get(hass).async_get_or_create(
        "binary_sensor", "t", "gate_contact", suggested_object_id="gate_contact")
    hass.states.async_set(reg.entity_id, "off")
    gate = await make_gate(hass, hass_ws_client, entry, state_entity=reg.entity_id,
                           open_action=[{"action": "script.open"}])
    return reg, gate


async def test_export_service_writes_entity_ids_never_state_or_learned(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    reg, gate = await gate_with_entity(hass, hass_ws_client, entry)
    stored = async_get_director(hass, entry.entry_id).data["config"]["gates"][0]
    assert stored["state_entity"] == reg.id  # stored as the registry id
    out = await export(hass, entry)
    assert out["config"]["gates"][0]["state_entity"] == reg.entity_id
    assert out["config"]["gates"][0]["open_action"] == [{"action": "script.open"}]
    assert set(out) == {"format", "version", "config"}
    out = await export(hass, entry, include_history=True)
    assert "history" in out and "state" not in out and "learned" not in out


async def test_websocket_download_matches_the_service(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    await gate_with_entity(hass, hass_ws_client, entry)
    msg = await ws(hass, hass_ws_client, entry, "export_config", include_history=False)
    assert msg["success"] and msg["result"]["export"] == await export(hass, entry)


async def test_import_replaces_config_fires_event_and_undo_restores(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    reg, gate = await gate_with_entity(hass, hass_ws_client, entry)
    out = await export(hass, entry)
    await ws(hass, hass_ws_client, entry, "delete_gate", gate_id=gate["id"])
    director = async_get_director(hass, entry.entry_id)
    assert director.data["config"]["gates"] == []
    events = async_capture_events(hass, EVENT_TYPE)

    msg = await ws(hass, hass_ws_client, entry, "import_config", export=out)
    await hass.async_block_till_done()
    assert msg["success"], msg
    assert msg["result"]["report"]["dropped_zones"] == []
    [gate_rec] = director.data["config"]["gates"]
    assert gate_rec["state_entity"] == reg.id  # re-resolved to the registry id
    [event] = [e.data for e in events if e.data["type"] == "config_imported"]
    assert event["report"] == msg["result"]["report"]

    msg = await ws(hass, hass_ws_client, entry, "undo_import")
    assert msg["success"], msg
    assert director.data["config"]["gates"] == []
    msg = await ws(hass, hass_ws_client, entry, "undo_import")
    assert not msg["success"]  # nothing left to undo


async def test_import_is_refused_unless_the_schedule_is_idle(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    out = await export(hass, entry)
    await add_plan(hass)
    director = async_get_director(hass, entry.entry_id)
    before = repr(director.data["config"])
    msg = await ws(hass, hass_ws_client, entry, "import_config", export=out)
    assert not msg["success"]
    assert repr(director.data["config"]) == before


async def test_import_refuses_a_file_that_is_not_an_export(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    msg = await ws(hass, hass_ws_client, entry, "import_config", export={"config": {}})
    assert not msg["success"]


async def test_import_drops_unmatched_zones_and_reports_them(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    out = await export(hass, entry)
    out["config"]["zones"].append(
        {"id": "ghost", "native_ref": "nope", "name": "Nowhere", "hidden": False, "missing": False})
    msg = await ws(hass, hass_ws_client, entry, "import_config", export=out)
    assert msg["result"]["report"]["dropped_zones"] == ["Nowhere"]
    names = [z["name"] for z in async_get_director(hass, entry.entry_id).data["config"]["zones"]]
    assert "Nowhere" not in names and "Front Lawn" in names


async def test_unresolved_entity_is_kept_reported_and_raises_one_repairs_issue(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    out = await export(hass, entry)
    out["config"]["gates"] = [{
        "id": "g1", "name": "Far gate", "channels": [out["config"]["channels"][0]["id"]], "state_entity": "binary_sensor.elsewhere",
    }]
    msg = await ws(hass, hass_ws_client, entry, "import_config", export=out)
    await hass.async_block_till_done()
    assert msg["result"]["report"]["unresolved_entities"] == [
        {"ref": "binary_sensor.elsewhere", "where": 'Gate "Far gate" state entity'}]
    director = async_get_director(hass, entry.entry_id)
    assert director.data["config"]["gates"][0]["state_entity"] == "binary_sensor.elsewhere"
    issue = broken_issue(hass, entry)
    assert issue is not None and not issue.is_fixable
    assert "binary_sensor.elsewhere" in issue.translation_placeholders["references"]
    assert 'Gate "Far gate" state entity' in issue.translation_placeholders["references"]

    # The Gate believes it closed while its entity is missing (reads unavailable).
    [gate] = director.gates
    assert director.gate_entity_state(gate) == "unavailable"

    # It starts working when the entity appears, and the issue clears.
    hass.states.async_set("binary_sensor.elsewhere", "on")
    await hass.async_block_till_done()
    assert broken_issue(hass, entry) is None


async def test_editing_the_reference_away_clears_the_issue(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    out = await export(hass, entry)
    out["config"]["gates"] = [{
        "id": "g1", "name": "Far gate", "channels": [out["config"]["channels"][0]["id"]], "state_entity": "binary_sensor.elsewhere",
    }]
    await ws(hass, hass_ws_client, entry, "import_config", export=out)
    await hass.async_block_till_done()
    assert broken_issue(hass, entry) is not None
    msg = await ws(hass, hass_ws_client, entry, "update_gate", gate_id="g1",
                   changes={"state_entity": None})
    assert msg["success"], msg
    await hass.async_block_till_done()
    assert broken_issue(hass, entry) is None


async def test_missing_notify_service_is_listed_and_clears_when_it_appears(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    msg = await ws(hass, hass_ws_client, entry, "set_notify_settings",
                   targets=["mobile_app_pixel"])
    assert msg["success"], msg
    await hass.async_block_till_done()
    issue = broken_issue(hass, entry)
    assert "mobile_app_pixel" in issue.translation_placeholders["references"]

    async def noop(call):
        return None

    hass.services.async_register("notify", "mobile_app_pixel", noop)
    await hass.async_block_till_done()
    assert broken_issue(hass, entry) is None


async def test_custom_check_on_a_missing_entity_fails(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    director = async_get_director(hass, entry.entry_id)
    cfg = {"mode": "entity", "entity": "sensor.gone", "operator": "is", "value": "on"}
    assert director._custom_result(cfg) is not None  # noqa: SLF001
    assert director._picked_states(["sensor.gone"]) == ("unavailable",)  # noqa: SLF001


async def test_restore_from_export_in_safe_idle(hass, hass_storage, hass_ws_client, setup) -> None:
    entry, _ = setup
    reg, gate = await gate_with_entity(hass, hass_ws_client, entry)
    out = await export(hass, entry)
    key = f"groundskeeper.{entry.entry_id}"
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass_storage[key]["data"]["config"]["zones"] = "garbage"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"store_safe_idle_{entry.entry_id}")

    msg = await ws(hass, hass_ws_client, entry, "import_config", export=out)
    await hass.async_block_till_done()
    assert msg["success"], msg
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"store_safe_idle_{entry.entry_id}") is None
    director = async_get_director(hass, entry.entry_id)
    assert director.data["config"]["gates"][0]["state_entity"] == reg.id
    assert hass_storage[key]["data"]["config"]["gates"][0]["state_entity"] == reg.id
