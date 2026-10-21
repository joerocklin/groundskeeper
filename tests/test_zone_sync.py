"""Zone import and sync (spec §4.4, §4.6, §4.8) against FakeMower."""

from dataclasses import replace

from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director.adapter import Channel, Zone, ZonesChanged
from custom_components.mowing_director.const import DOMAIN
from custom_components.mowing_director.zone_sync import (
    async_attach_zone_sync,
    reconcile_channels,
    reconcile_zones,
)
from tests.fake_mower import FakeMower

POLY = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}


def _ids():
    n = iter(range(1000))
    return lambda: f"id{next(n)}"


def _zone(id_, ref, name, **kw):
    return {
        "id": id_, "native_ref": ref, "name": name, "hidden": False,
        "missing": False, "polygon": POLY, **kw,
    }


def test_new_zones_are_visible_with_stable_ids() -> None:
    out = reconcile_zones([], [Zone("a", "Front"), Zone("b", "Back")], _ids())
    assert [(z["id"], z["name"], z["hidden"], z["missing"]) for z in out] == [
        ("id0", "Front", False, False),
        ("id1", "Back", False, False),
    ]


def test_same_ref_new_name_renames() -> None:
    out = reconcile_zones([_zone("z1", "a", "Front")], [Zone("a", "Lawn")], _ids())
    assert out[0]["id"] == "z1" and out[0]["name"] == "Lawn"
    assert out[0]["polygon"] == POLY  # no refetch on rename


def test_new_ref_known_name_rebinds_keeping_id_and_overrides() -> None:
    known = [_zone("z1", "a", "Front", hidden=True,
                   bearing={"source": "manual", "value": 42.0})]
    out = reconcile_zones(known, [Zone("b", "Front")], _ids())
    assert len(out) == 1
    z = out[0]
    assert (z["id"], z["native_ref"], z["hidden"], z["bearing"]["value"]) == (
        "z1", "b", True, 42.0,
    )
    assert z["polygon"] is None  # changed ref refetches the polygon


def test_ref_match_wins_over_name_match() -> None:
    known = [_zone("z1", "a", "Front"), _zone("z2", "b", "Back")]
    # Mower swapped names: refs still decide.
    out = reconcile_zones(known, [Zone("a", "Back"), Zone("b", "Front")], _ids())
    assert [(z["id"], z["name"]) for z in out] == [("z1", "Back"), ("z2", "Front")]


def test_unmatched_known_zone_is_marked_missing_never_deleted() -> None:
    out = reconcile_zones([_zone("z1", "a", "Front")], [], _ids())
    assert out[0]["missing"] is True


def test_missing_zone_returning_by_name_is_unmissed() -> None:
    known = [_zone("z1", "a", "Front", missing=True)]
    out = reconcile_zones(known, [Zone("c", "Front", POLY)], _ids())
    assert out[0]["id"] == "z1" and out[0]["missing"] is False


def test_polygon_arrives_later_without_blocking() -> None:
    known = [_zone("z1", "a", "Front", polygon=None)]
    out = reconcile_zones(known, [Zone("a", "Front", POLY)], _ids())
    assert out[0]["polygon"] == POLY


def test_channels_reconcile_by_end_zones_across_ref_change() -> None:
    zones = [_zone("z1", "a", "Front"), _zone("z2", "b", "Back")]
    known = [{"id": "c1", "native_ref": "old", "end_a": "z1", "end_b": "dock"}]
    out = reconcile_channels(
        known,
        [Channel("new", "dock", "a"), Channel("p2", "a", "b")],
        zones,
        _ids(),
    )
    assert out[0] == {"id": "c1", "native_ref": "new", "end_a": "dock", "end_b": "z1"}
    assert out[1]["id"] == "id0" and {out[1]["end_a"], out[1]["end_b"]} == {"z1", "z2"}


def _entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"device_id": "d"})
    entry.add_to_hass(hass)
    return entry


async def _attached(hass, mower):
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sync = async_attach_zone_sync(hass, entry.entry_id, mower, entry.runtime_data)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry, sync


async def test_first_load_imports_zones_and_channels(hass) -> None:
    _, sync = await _attached(hass, FakeMower())
    zones = await sync.async_zones()
    assert [z["name"] for z in zones] == ["Front Lawn", "Backyard"]
    assert zones[1]["polygon"] is None  # pending, not blocking
    assert len({z["id"] for z in zones}) == 2
    channels = await sync.async_channels()
    assert {frozenset((c["end_a"], c["end_b"])) for c in channels} == {
        frozenset((zones[0]["id"], "dock")),
        frozenset((zones[0]["id"], zones[1]["id"])),
    }


async def test_zones_persist_in_store_and_survive_resync(hass) -> None:
    mower = FakeMower()
    entry, sync = await _attached(hass, mower)
    ids = [z["id"] for z in await sync.async_zones()]
    # Map rebuild: new hashes, same names.
    mower.zones = tuple(replace(z, native_ref=z.native_ref + "x") for z in mower.zones)
    await sync.async_sync()
    assert [z["id"] for z in await sync.async_zones()] == ids
    stored = await entry.runtime_data.async_load()
    assert [z["id"] for z in stored["config"]["zones"]] == ids


async def test_zone_list_changed_signal_triggers_sync(hass) -> None:
    mower = FakeMower()
    _, sync = await _attached(hass, mower)
    mower.zones = mower.zones + (Zone("33333333", "Side"),)
    mower.emit_signal(ZonesChanged())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "Side" in [z["name"] for z in await sync.async_zones()]


async def test_hidden_flag_toggles_and_survives_sync(hass) -> None:
    _, sync = await _attached(hass, FakeMower())
    zid = (await sync.async_zones())[0]["id"]
    await sync.async_set_hidden(zid, True)
    await sync.async_sync()
    assert (await sync.async_zones())[0]["hidden"] is True


async def test_missing_zones_raise_one_repairs_issue_and_clear_on_return(hass) -> None:
    mower = FakeMower()
    entry, sync = await _attached(hass, mower)
    full = mower.zones
    mower.zones = full[:1]
    await sync.async_sync()
    zones = await sync.async_zones()
    assert [z["missing"] for z in zones] == [False, True]
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"missing_zones_{entry.entry_id}"
    )
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders == {"zones": "Backyard"}
    mower.zones = full
    await sync.async_sync()
    assert not any(z["missing"] for z in await sync.async_zones())
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"missing_zones_{entry.entry_id}"
    ) is None


async def test_fix_flow_delete_removes_zone_and_its_plans(hass, hass_client) -> None:
    from custom_components.mowing_director.repairs import async_create_fix_flow

    mower = FakeMower()
    entry, sync = await _attached(hass, mower)
    zones = await sync.async_zones()
    sync._data["config"]["plans"] = [
        {"id": "p1", "zone_id": zones[0]["id"]},
        {"id": "p2", "zone_id": zones[1]["id"]},
    ]
    mower.zones = mower.zones[:1]
    await sync.async_sync()

    flow = await async_create_fix_flow(
        hass, f"missing_zones_{entry.entry_id}", {"entry_id": entry.entry_id}
    )
    flow.hass = hass
    result = await flow.async_step_delete()
    assert result["type"].value == "create_entry"
    assert [z["name"] for z in await sync.async_zones()] == ["Front Lawn"]
    assert [p["id"] for p in sync._data["config"]["plans"]] == ["p1"]
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"missing_zones_{entry.entry_id}"
    ) is None


async def test_fix_flow_wait_changes_nothing(hass) -> None:
    from custom_components.mowing_director.repairs import async_create_fix_flow

    mower = FakeMower()
    entry, sync = await _attached(hass, mower)
    mower.zones = mower.zones[:1]
    await sync.async_sync()
    flow = await async_create_fix_flow(
        hass, f"missing_zones_{entry.entry_id}", {"entry_id": entry.entry_id}
    )
    flow.hass = hass
    result = await flow.async_step_wait()
    assert result["type"].value == "abort"
    assert [z["missing"] for z in await sync.async_zones()] == [False, True]


async def test_refresh_zones_service(hass) -> None:
    mower = FakeMower()
    entry, sync = await _attached(hass, mower)
    mower.zones = mower.zones + (Zone("33333333", "Side"),)
    await hass.services.async_call(DOMAIN, "refresh_zones", {}, blocking=True)
    assert "Side" in [z["name"] for z in await sync.async_zones()]


async def test_refresh_zones_ambiguous_without_entry_id(hass) -> None:
    import pytest
    from homeassistant.exceptions import ServiceValidationError

    for _ in range(2):
        e = _entry(hass)
        await hass.config_entries.async_setup(e.entry_id)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "refresh_zones", {}, blocking=True)


async def test_ws_refresh_and_hide(hass, hass_ws_client) -> None:
    mower = FakeMower()
    entry, sync = await _attached(hass, mower)
    zid = (await sync.async_zones())[0]["id"]
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "mowing_director/set_zone_hidden",
                        "entry_id": entry.entry_id, "zone_id": zid, "hidden": True})
    assert (await ws.receive_json())["success"]
    await ws.send_json({"id": 2, "type": "mowing_director/refresh_zones",
                        "entry_id": entry.entry_id})
    assert (await ws.receive_json())["success"]
    assert (await sync.async_zones())[0]["hidden"] is True


# -- bearing and Allowed angle range (issue #12) ------------------------------

from tests.test_zone_bearing import rect  # noqa: E402


def _long(bearing):
    return {"type": "Polygon", "coordinates": [rect(40, 8, bearing)]}


async def _with_polygon(hass, bearing=30):
    mower = FakeMower()
    mower.zones = (Zone("11111111", "Front Lawn", _long(bearing)),
                   Zone("22222222", "Backyard", None))
    _, sync = await _attached(hass, mower)
    return mower, sync, (await sync.async_zones())


async def test_sync_fills_bearing_and_range_records(hass) -> None:
    _, sync, zones = await _with_polygon(hass)
    assert zones[0]["bearing"]["status"] == "ready"
    assert abs(zones[0]["bearing"]["value"] - 30) < 0.5
    assert zones[1]["bearing"]["status"] == "pending"
    assert zones[1]["angle_range"]["angle_range"] == "unset"


async def test_polygon_arriving_later_sets_bearing_and_notifies_core(hass) -> None:
    mower, sync, zones = await _with_polygon(hass)
    seen = []
    sync.subscribe_range_changes(lambda zid, r: seen.append((zid, r)))
    mower.zones = (mower.zones[0], Zone("22222222", "Backyard", _long(80)))
    await sync.async_sync()
    zones = await sync.async_zones()
    assert zones[1]["bearing"]["status"] == "ready"
    assert [zid for zid, _ in seen] == [zones[1]["id"]]
    assert seen[0][1] != "unset"


async def test_manual_bearing_survives_refresh_and_recomputes_range(hass) -> None:
    mower, sync, zones = await _with_polygon(hass)
    zid = zones[0]["id"]
    before = zones[0]["angle_range"]["derived"]
    seen = []
    sync.subscribe_range_changes(lambda z, r: seen.append(r))
    await sync.async_set_bearing(zid, 100.0)
    await sync.async_sync()
    z = (await sync.async_zones())[0]
    assert (z["bearing"]["source"], z["bearing"]["value"]) == ("manual", 100.0)
    assert abs(z["bearing"]["derived_value"] - 30) < 0.5
    assert z["angle_range"]["derived"] != before
    res = await sync.async_reset_bearing(zid)
    assert abs(res["bearing"]["value"] - 30) < 0.5
    assert res["bearing"]["source"] == "derived"


async def test_manual_range_survives_and_reset_restores_derived(hass) -> None:
    _, sync, zones = await _with_polygon(hass)
    zid = zones[0]["id"]
    derived = zones[0]["angle_range"]["derived"]
    seen = []
    sync.subscribe_range_changes(lambda z, r: seen.append(r))
    await sync.async_set_range(zid, {"min": -7, "max": 9})
    await sync.async_sync()
    r = (await sync.async_zones())[0]["angle_range"]
    assert (r["source"], r["angle_range"], r["derived"]) == (
        "manual", {"min": -7, "max": 9}, derived)
    res = await sync.async_reset_range(zid)
    assert res["angle_range"]["angle_range"] == derived
    assert seen == [{"min": -7, "max": 9}, derived]


async def test_settings_change_recomputes_every_zone(hass) -> None:
    _, sync, zones = await _with_polygon(hass)
    await sync.async_update_settings(range_tolerance=0.0, spacing=0.3)
    r = (await sync.async_zones())[0]["angle_range"]
    assert r["basis"]["tolerance"] == 0.0 and r["basis"]["spacing"] == 0.3
    assert r["derived"] != zones[0]["angle_range"]["derived"]


async def test_zone_tolerance_override(hass) -> None:
    _, sync, zones = await _with_polygon(hass)
    res = await sync.async_set_range_tolerance(zones[0]["id"], 0.0)
    assert res["angle_range"]["basis"]["tolerance"] == 0.0
    await sync.async_update_settings(range_tolerance=0.5)
    r = (await sync.async_zones())[0]["angle_range"]
    assert r["basis"]["tolerance"] == 0.0


async def test_unknown_zone_raises(hass) -> None:
    import pytest

    _, sync, _z = await _with_polygon(hass)
    with pytest.raises(KeyError):
        await sync.async_set_bearing("nope", 10)


# -- Zones tab data (issue #28) ----------------------------------------------


async def test_ws_get_zones_gives_the_zones_tab_everything(hass, hass_ws_client) -> None:
    mower, sync, zones = await _with_polygon(hass)
    entry_id = hass.config_entries.async_entries(DOMAIN)[0].entry_id
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "mowing_director/get_zones", "entry_id": entry_id})
    msg = await ws.receive_json()
    assert msg["success"]
    front, back = msg["result"]["zones"]
    assert (front["name"], front["hidden"], front["missing"]) == ("Front Lawn", False, False)
    assert front["polygon"]["type"] == "Polygon"
    assert front["bearing"]["status"] == "ready"
    assert front["estimates"]["base"]["time_s"] > 0
    assert back["bearing"]["status"] == "pending" and back["estimates"] is None
    ends = {frozenset((c["end_a"], c["end_b"])) for c in msg["result"]["channels"]}
    assert frozenset((front["id"], "dock")) in ends


async def test_ws_get_zones_unknown_mower(hass, hass_ws_client) -> None:
    await _with_polygon(hass)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "mowing_director/get_zones", "entry_id": "nope"})
    assert (await ws.receive_json())["error"]["code"] == "not_found"
