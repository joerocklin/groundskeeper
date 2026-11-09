"""The setup checklist's marks, through the websocket API (spec §4.3, issue #42)."""

import asyncio

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_DEVICE

from custom_components.groundskeeper import adapters
from custom_components.groundskeeper.const import DOMAIN
from tests.conftest import make_mammotion_mower
from tests.fake_mower import FakeMower


@pytest.fixture
def add_mower(hass, monkeypatch):
    """Add a Groundskeeper Mower through the config flow; returns its entry."""

    async def add(name: str = "Luba-SERIAL1", nick: str = "Luba", *, zones=None):
        mower = FakeMower()
        if zones is not None:
            mower.zones = zones
        monkeypatch.setitem(adapters.ADAPTER_REGISTRY, "mammotion", lambda h, e: mower)
        _, device, _ = await make_mammotion_mower(hass, name, nick)
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: device.id}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
        entry = result["result"]
        add.mowers[entry.entry_id] = mower
        return entry

    add.mowers = {}  # entry_id -> its FakeMower
    return add


@pytest.fixture
async def entry(add_mower):
    return await add_mower()


async def set_step(ws, msg_id, entry, step, **extra) -> dict:
    await ws.send_json(
        {"id": msg_id, "type": f"{DOMAIN}/set_setup_step",
         "entry_id": entry.entry_id, "step": step, **extra}
    )
    return await ws.receive_json()


async def steps_done(ws, msg_id, entry) -> dict[str, bool]:
    await ws.send_json({"id": msg_id, "type": f"{DOMAIN}/get_state", "entry_id": entry.entry_id})
    items = (await ws.receive_json())["result"]["setup_checklist"]["items"]
    return {i["id"]: i["done"] for i in items}


async def test_a_marked_step_stays_done_across_a_reload(hass, hass_ws_client, entry) -> None:
    ws = await hass_ws_client(hass)
    assert (await steps_done(ws, 1, entry))["bearings"] is False
    assert (await set_step(ws, 2, entry, "bearings"))["success"]
    assert (await steps_done(ws, 3, entry))["bearings"] is True

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (await steps_done(ws, 4, entry))["bearings"] is True


async def test_panel_only_and_recording_review_are_marked_the_same_way(
    hass, hass_ws_client, entry
) -> None:
    ws = await hass_ws_client(hass)
    before = await steps_done(ws, 1, entry)
    assert before["notify"] is False and before["recording"] is False

    assert (await set_step(ws, 2, entry, "notify"))["success"]
    assert (await set_step(ws, 3, entry, "recording"))["success"]
    after = await steps_done(ws, 4, entry)
    assert after["notify"] is True and after["recording"] is True


async def test_the_gates_step_takes_a_yes_or_no_answer(hass, hass_ws_client, entry) -> None:
    ws = await hass_ws_client(hass)
    assert (await set_step(ws, 1, entry, "gates", answer="yes"))["success"]
    assert (await steps_done(ws, 2, entry))["gates"] is False  # still needs a Gate
    assert (await set_step(ws, 3, entry, "gates", answer="no"))["success"]
    assert (await steps_done(ws, 4, entry))["gates"] is True


async def test_gates_without_an_answer_and_unknown_steps_are_refused(
    hass, hass_ws_client, entry
) -> None:
    ws = await hass_ws_client(hass)
    missing = await set_step(ws, 1, entry, "gates")
    assert missing["success"] is False and missing["error"]["code"] == "answer_required"
    assert (await set_step(ws, 2, entry, "bogus"))["success"] is False
    assert (await steps_done(ws, 3, entry))["gates"] is False


async def test_one_mowers_marks_do_not_reach_another(hass, hass_ws_client, add_mower) -> None:
    luba = await add_mower("Luba-SERIAL1", "Luba")
    yuka = await add_mower("Yuka-SERIAL2", "Yuka")
    ws = await hass_ws_client(hass)
    assert (await set_step(ws, 1, luba, "bearings"))["success"]
    assert (await steps_done(ws, 2, luba))["bearings"] is True
    assert (await steps_done(ws, 3, yuka))["bearings"] is False


async def test_a_mark_pushes_a_fresh_snapshot_to_the_panel(hass, hass_ws_client, entry) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/subscribe", "entry_id": entry.entry_id})
    assert (await ws.receive_json())["success"]
    first = await ws.receive_json()
    assert {i["id"]: i["done"] for i in first["event"]["setup_checklist"]["items"]}["bearings"] is False

    await ws.send_json({"id": 2, "type": f"{DOMAIN}/set_setup_step",
                        "entry_id": entry.entry_id, "step": "bearings"})
    messages = [await ws.receive_json(), await ws.receive_json()]  # the result and the push
    pushed = next(m for m in messages if m["type"] == "event")
    assert {i["id"]: i["done"] for i in pushed["event"]["setup_checklist"]["items"]}["bearings"] is True


async def test_an_export_does_not_carry_the_marks(hass, hass_ws_client, entry) -> None:
    ws = await hass_ws_client(hass)
    assert (await set_step(ws, 1, entry, "bearings"))["success"]
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/export_config", "entry_id": entry.entry_id})
    exported = (await ws.receive_json())["result"]["export"]
    assert "setup" not in exported["config"]


async def _export(ws, msg_id, entry) -> dict:
    await ws.send_json({"id": msg_id, "type": f"{DOMAIN}/export_config", "entry_id": entry.entry_id})
    return (await ws.receive_json())["result"]["export"]


async def _import(ws, msg_id, entry, export) -> dict:
    await ws.send_json({"id": msg_id, "type": f"{DOMAIN}/import_config",
                        "entry_id": entry.entry_id, "export": export})
    return await ws.receive_json()


async def test_an_import_keeps_this_mowers_marks(hass, hass_ws_client, entry) -> None:
    ws = await hass_ws_client(hass)
    assert (await set_step(ws, 1, entry, "bearings"))["success"]
    export = await _export(ws, 2, entry)
    assert (await _import(ws, 3, entry, export))["success"]
    assert (await steps_done(ws, 4, entry))["bearings"] is True


async def test_an_import_ignores_marks_in_the_file(hass, hass_ws_client, entry) -> None:
    ws = await hass_ws_client(hass)
    export = await _export(ws, 1, entry)
    export["config"]["setup"] = {"recording_reviewed": True, "bearings_reviewed": True}
    assert (await _import(ws, 2, entry, export))["success"]
    done = await steps_done(ws, 3, entry)
    assert done["recording"] is False and done["bearings"] is False


async def test_zones_imported_in_the_background_reach_an_open_panel(
    hass, hass_ws_client, add_mower
) -> None:
    from custom_components.groundskeeper.adapter import Zone, ZonesChanged
    from tests.test_zone_bearing import rect

    entry = await add_mower(zones=())  # nothing to import yet
    mower = add_mower.mowers[entry.entry_id]
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/subscribe", "entry_id": entry.entry_id})
    assert (await ws.receive_json())["success"]
    first = await ws.receive_json()
    assert {i["id"]: i["done"] for i in first["event"]["setup_checklist"]["items"]}["zones"] is False

    mower.zones = (
        Zone("11111111", "Front Lawn", {"type": "Polygon", "coordinates": [rect(40, 8, 30)]}),
    )
    mower.emit_signal(ZonesChanged())
    await hass.async_block_till_done(wait_background_tasks=True)
    pushed = await asyncio.wait_for(ws.receive_json(), timeout=2)  # no push: fail, don't hang
    assert {i["id"]: i["done"] for i in pushed["event"]["setup_checklist"]["items"]}["zones"] is True
