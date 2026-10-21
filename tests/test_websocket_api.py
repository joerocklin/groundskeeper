"""Tests for the panel's per-entry websocket API (spec §2.3, §16.1)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director.const import DOMAIN
from custom_components.mowing_director.websocket_api import (
    async_notify_state_changed,
)


async def _setup(hass: HomeAssistant, *titles: str) -> list[MockConfigEntry]:
    entries = []
    for t in titles:
        e = MockConfigEntry(domain=DOMAIN, title=t, data={"device_id": f"dev-{t}"})
        e.add_to_hass(hass)
        entries.append(e)
    await hass.config_entries.async_setup(entries[0].entry_id)
    await hass.async_block_till_done()
    return entries


async def test_list_mowers_feeds_the_switcher(hass, hass_ws_client) -> None:
    """One row per loaded Mower entry."""
    await _setup(hass, "Luba", "Yuka")
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "mowing_director/list_mowers"})
    msg = await ws.receive_json()
    assert msg["success"]
    assert sorted(m["title"] for m in msg["result"]["mowers"]) == ["Luba", "Yuka"]


async def test_get_state_targets_one_entry(hass, hass_ws_client) -> None:
    """get_state returns that Mower's snapshot, including the checklist area."""
    luba, _ = await _setup(hass, "Luba", "Yuka")
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {"id": 1, "type": "mowing_director/get_state", "entry_id": luba.entry_id}
    )
    msg = await ws.receive_json()
    assert msg["success"]
    assert msg["result"]["entry_id"] == luba.entry_id
    assert msg["result"]["title"] == "Luba"
    assert msg["result"]["setup_checklist"] == {"complete": True, "items": []}


async def test_unknown_entry_is_not_found(hass, hass_ws_client) -> None:
    """A stale entry_id yields a clean not_found error."""
    await _setup(hass, "Luba")
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {"id": 1, "type": "mowing_director/get_state", "entry_id": "nope"}
    )
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_commands_require_admin(
    hass, hass_ws_client, hass_read_only_access_token
) -> None:
    """A non-admin user is refused."""
    luba, = await _setup(hass, "Luba")
    ws = await hass_ws_client(hass, hass_read_only_access_token)
    await ws.send_json({"id": 1, "type": "mowing_director/list_mowers"})
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "unauthorized"


async def test_subscribe_pushes_state_changes(hass, hass_ws_client) -> None:
    """Subscribing yields the current snapshot, then one per change."""
    luba, yuka = await _setup(hass, "Luba", "Yuka")
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {"id": 1, "type": "mowing_director/subscribe", "entry_id": luba.entry_id}
    )
    assert (await ws.receive_json())["success"]
    first = await ws.receive_json()
    assert first["type"] == "event"
    assert first["event"]["entry_id"] == luba.entry_id

    hass.config_entries.async_update_entry(luba, title="Luba 2")
    async_notify_state_changed(hass, luba.entry_id)
    pushed = await ws.receive_json()
    assert pushed["event"]["title"] == "Luba 2"

    # Another Mower's change is not delivered to this subscription.
    async_notify_state_changed(hass, yuka.entry_id)
    await hass.async_block_till_done()
    await ws.send_json({"id": 2, "type": "ping"})
    assert (await ws.receive_json())["type"] == "pong"


async def test_unsubscribe_stops_pushes(hass, hass_ws_client) -> None:
    """After unsubscribe_events, changes are no longer pushed."""
    luba, = await _setup(hass, "Luba")
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {"id": 1, "type": "mowing_director/subscribe", "entry_id": luba.entry_id}
    )
    await ws.receive_json()
    await ws.receive_json()
    await ws.send_json({"id": 2, "type": "unsubscribe_events", "subscription": 1})
    assert (await ws.receive_json())["success"]
    async_notify_state_changed(hass, luba.entry_id)
    await hass.async_block_till_done()
    await ws.send_json({"id": 3, "type": "ping"})
    assert (await ws.receive_json())["type"] == "pong"


# -- Zone bearing and range commands (issue #12) --------------------------------


async def _zone_ws(hass, hass_ws_client):
    from custom_components.mowing_director.adapter import Zone
    from custom_components.mowing_director.zone_sync import async_attach_zone_sync
    from tests.fake_mower import FakeMower
    from tests.test_zone_bearing import rect

    (entry,) = await _setup(hass, "Luba")
    mower = FakeMower()
    mower.zones = (
        Zone("11111111", "Front Lawn",
             {"type": "Polygon", "coordinates": [rect(40, 8, 30)]}),
    )
    sync = async_attach_zone_sync(hass, entry.entry_id, mower, entry.runtime_data)
    await hass.async_block_till_done(wait_background_tasks=True)
    zone = (await sync.async_zones())[0]
    return entry, zone, await hass_ws_client(hass)


async def test_zone_angle_commands_read_set_reset(hass, hass_ws_client) -> None:
    entry, zone, ws = await _zone_ws(hass, hass_ws_client)
    base = {"entry_id": entry.entry_id, "zone_id": zone["id"]}

    await ws.send_json_auto_id({"type": "mowing_director/get_zone_angles", **base})
    got = (await ws.receive_json())["result"]["zones"][0]
    assert got["bearing"]["source"] == "derived"

    await ws.send_json_auto_id(
        {"type": "mowing_director/set_zone_bearing", "value": 100, **base})
    r = (await ws.receive_json())["result"]
    assert (r["bearing"]["source"], r["bearing"]["value"]) == ("manual", 100.0)

    await ws.send_json_auto_id(
        {"type": "mowing_director/set_zone_range", "min": -10, "max": 12, **base})
    r = (await ws.receive_json())["result"]
    assert r["angle_range"]["angle_range"] == {"min": -10.0, "max": 12.0}
    assert r["angle_range"]["source"] == "manual"

    await ws.send_json_auto_id(
        {"type": "mowing_director/set_zone_range_tolerance", "tolerance": 0.1, **base})
    r = (await ws.receive_json())["result"]
    assert r["angle_range"]["basis"]["tolerance"] == 0.1

    await ws.send_json_auto_id({"type": "mowing_director/reset_zone_range", **base})
    r = (await ws.receive_json())["result"]
    assert r["angle_range"]["source"] == "derived"

    await ws.send_json_auto_id({"type": "mowing_director/reset_zone_bearing", **base})
    r = (await ws.receive_json())["result"]
    assert r["bearing"]["source"] == "derived"
    assert abs(r["bearing"]["value"] - 30) < 0.5


async def test_zone_range_rejects_range_excluding_zero(hass, hass_ws_client) -> None:
    entry, zone, ws = await _zone_ws(hass, hass_ws_client)
    await ws.send_json_auto_id(
        {"type": "mowing_director/set_zone_range", "min": 5, "max": 12,
         "entry_id": entry.entry_id, "zone_id": zone["id"]})
    assert (await ws.receive_json())["success"] is False


async def test_zone_angle_commands_unknown_zone(hass, hass_ws_client) -> None:
    entry, _zone, ws = await _zone_ws(hass, hass_ws_client)
    await ws.send_json_auto_id(
        {"type": "mowing_director/reset_zone_bearing",
         "entry_id": entry.entry_id, "zone_id": "nope"})
    assert (await ws.receive_json())["error"]["code"] == "not_found"
