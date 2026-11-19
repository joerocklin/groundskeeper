"""Zone import and sync (spec §4.4, §4.6, §4.8) against FakeMower."""

import json
from dataclasses import replace

from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.groundskeeper.adapter import Channel, Zone, ZonesChanged
from custom_components.groundskeeper.const import DOMAIN
from custom_components.groundskeeper.zone_sync import (
    async_attach_zone_sync,
    reconcile_channels,
    reconcile_zones,
)
from tests.fake_mower import FakeMower

POLY = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
PATH = {"type": "LineString", "coordinates": [[0, 0], [0.5, 0.2], [1, 1]]}
HOLE = {"type": "Polygon", "coordinates": [[[0.2, 0.2], [0.4, 0.2], [0.4, 0.4], [0.2, 0.2]]]}
HOLE_2 = {"type": "Polygon", "coordinates": [[[0.5, 0.5], [0.6, 0.5], [0.6, 0.6], [0.5, 0.5]]]}


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


def test_a_new_zone_stores_its_nogo_areas() -> None:
    out = reconcile_zones([], [Zone("a", "Front", POLY, [HOLE])], _ids())
    assert out[0]["nogo_areas"] == [HOLE]


def test_a_new_zone_with_unknown_nogo_areas_stores_unknown_not_none() -> None:
    out = reconcile_zones([], [Zone("a", "Front")], _ids())
    assert out[0]["nogo_areas"] is None


def test_nogo_areas_follow_the_map_when_one_is_added_or_removed() -> None:
    known = [_zone("z1", "a", "Front", nogo_areas=[HOLE])]
    added = reconcile_zones(known, [Zone("a", "Front", POLY, [HOLE, HOLE_2])], _ids())
    assert added[0]["nogo_areas"] == [HOLE, HOLE_2]
    removed = reconcile_zones(added, [Zone("a", "Front", POLY, [HOLE_2])], _ids())
    assert removed[0]["nogo_areas"] == [HOLE_2]


def test_a_zone_whose_last_nogo_area_is_removed_reads_none_not_stale() -> None:
    known = [_zone("z1", "a", "Front", nogo_areas=[HOLE])]
    out = reconcile_zones(known, [Zone("a", "Front", POLY, [])], _ids())
    assert out[0]["nogo_areas"] == []


def test_a_refresh_without_geometry_keeps_the_last_known_nogo_areas() -> None:
    known = [_zone("z1", "a", "Front", nogo_areas=[HOLE])]
    out = reconcile_zones(known, [Zone("a", "Front")], _ids())  # fetch failed
    assert out[0]["nogo_areas"] == [HOLE]


def test_rebinding_to_a_new_ref_refetches_nogo_areas_as_the_polygon_is() -> None:
    known = [_zone("z1", "a", "Front", nogo_areas=[HOLE])]
    # The new ref's geometry is not loaded yet: the old ref's areas must not
    # be kept as if they were the new Zone's.
    pending = reconcile_zones(known, [Zone("b", "Front")], _ids())
    assert pending[0]["polygon"] is None and pending[0]["nogo_areas"] is None
    loaded = reconcile_zones(known, [Zone("b", "Front", POLY, [HOLE_2])], _ids())
    assert loaded[0]["nogo_areas"] == [HOLE_2]


def test_a_record_written_before_nogo_areas_read_unknown_until_the_next_sync() -> None:
    old = _zone("z1", "a", "Front")
    assert "nogo_areas" not in old
    out = reconcile_zones([old], [Zone("a", "Front")], _ids())
    assert out[0]["nogo_areas"] is None
    out = reconcile_zones([old], [Zone("a", "Front", POLY, [HOLE])], _ids())
    assert out[0]["nogo_areas"] == [HOLE]


def test_a_missing_zone_keeps_its_nogo_areas() -> None:
    known = [_zone("z1", "a", "Front", nogo_areas=[HOLE])]
    out = reconcile_zones(known, [], _ids())
    assert out[0]["missing"] is True and out[0]["nogo_areas"] == [HOLE]


def test_channels_reconcile_by_end_zones_across_ref_change() -> None:
    zones = [_zone("z1", "a", "Front"), _zone("z2", "b", "Back")]
    known = [{"id": "c1", "native_ref": "old", "end_a": "z1", "end_b": "dock"}]
    out = reconcile_channels(
        known,
        [Channel("new", "dock", "a"), Channel("p2", "a", "b")],
        zones,
        _ids(),
    )
    assert out[0] == {
        "id": "c1", "native_ref": "new", "end_a": "dock", "end_b": "z1", "path": None,
    }
    assert out[1]["id"] == "id0" and {out[1]["end_a"], out[1]["end_b"]} == {"z1", "z2"}


def test_channel_path_is_stored_and_survives_a_refresh_without_geometry() -> None:
    zones = [_zone("z1", "a", "Front")]
    out = reconcile_channels([], [Channel("p1", "dock", "a", PATH)], zones, _ids())
    assert out[0]["path"] == PATH
    again = reconcile_channels(out, [Channel("p1", "dock", "a")], zones, _ids())
    assert again[0]["path"] == PATH  # geometry not refetched yet: keep the last one


def test_channel_path_is_replaced_when_the_map_changes() -> None:
    zones = [_zone("z1", "a", "Front")]
    known = [{"id": "c1", "native_ref": "p1", "end_a": "dock", "end_b": "z1",
              "path": PATH}]
    newer = {"type": "LineString", "coordinates": [[0, 0], [2, 2]]}
    out = reconcile_channels(known, [Channel("p1", "dock", "a", newer)], zones, _ids())
    assert out[0]["path"] == newer


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
    from custom_components.groundskeeper.repairs import async_create_fix_flow

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
    from custom_components.groundskeeper.repairs import async_create_fix_flow

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
    await ws.send_json({"id": 1, "type": "groundskeeper/set_zone_hidden",
                        "entry_id": entry.entry_id, "zone_id": zid, "hidden": True})
    assert (await ws.receive_json())["success"]
    await ws.send_json({"id": 2, "type": "groundskeeper/refresh_zones",
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
    await ws.send_json({"id": 1, "type": "groundskeeper/get_zones", "entry_id": entry_id})
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
    await ws.send_json({"id": 1, "type": "groundskeeper/get_zones", "entry_id": "nope"})
    assert (await ws.receive_json())["error"]["code"] == "not_found"


async def test_zone_view_serves_channel_paths_and_they_persist(hass) -> None:
    mower = FakeMower()
    mower.channels = tuple(replace(c, path=PATH) for c in mower.channels)
    entry, sync = await _attached(hass, mower)
    view = await sync.async_zone_view()
    assert [c["path"] for c in view["channels"]] == [PATH, PATH]
    stored = await entry.runtime_data.async_load()
    assert all(c["path"] == PATH for c in stored["config"]["channels"])


# -- No-go areas (issue #117) --------------------------------------------------


async def test_sync_stores_each_zones_nogo_areas_and_unknown_without_geometry(hass) -> None:
    entry, sync = await _attached(hass, FakeMower())
    front, back = await sync.async_zones()
    assert len(front["nogo_areas"]) == 1 and front["nogo_areas"][0]["type"] == "Polygon"
    assert back["nogo_areas"] is None  # geometry not loaded: unknown, not none
    stored = (await entry.runtime_data.async_load())["config"]["zones"]
    assert [z["nogo_areas"] is None for z in stored] == [False, True]


async def test_a_map_update_adding_or_removing_a_nogo_area_changes_the_stored_zone(
    hass,
) -> None:
    mower = FakeMower()
    _, sync = await _attached(hass, mower)
    front, back = mower.zones

    mower.zones = (replace(front, nogo_areas=[HOLE, HOLE_2]), back)
    mower.emit_signal(ZonesChanged())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (await sync.async_zones())[0]["nogo_areas"] == [HOLE, HOLE_2]

    mower.zones = (replace(front, nogo_areas=[HOLE_2]), back)
    mower.emit_signal(ZonesChanged())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (await sync.async_zones())[0]["nogo_areas"] == [HOLE_2]

    mower.zones = (replace(front, nogo_areas=[]), back)
    mower.emit_signal(ZonesChanged())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (await sync.async_zones())[0]["nogo_areas"] == []  # none, not stale


async def test_a_rebound_zone_refetches_its_nogo_areas_from_the_new_ref(hass) -> None:
    mower = FakeMower()
    _, sync = await _attached(hass, mower)
    front, back = mower.zones
    zid = (await sync.async_zones())[0]["id"]

    # Map rebuild: new hash, same name. The areas follow the new ref...
    mower.zones = (replace(front, native_ref="11111111x", nogo_areas=[HOLE_2]), back)
    await sync.async_sync()
    rebound = (await sync.async_zones())[0]
    assert (rebound["id"], rebound["native_ref"]) == (zid, "11111111x")
    assert rebound["nogo_areas"] == [HOLE_2]

    # ...and while the next ref's geometry is not loaded, the old ones are not
    # kept as if they were its own.
    mower.zones = (
        replace(front, native_ref="11111111y", polygon=None, nogo_areas=None),
        back,
    )
    await sync.async_sync()
    pending = (await sync.async_zones())[0]
    assert pending["native_ref"] == "11111111y" and pending["nogo_areas"] is None


async def test_nogo_areas_leave_the_stored_bearing_and_angle_range_untouched(hass) -> None:
    mower, sync, _ = await _with_polygon(hass)

    def angles() -> str:
        return json.dumps(
            [{k: z[k] for k in ("bearing", "angle_range")} for z in sync._data["config"]["zones"]],
            sort_keys=True,
        )

    before = angles()
    centre_hole = {"type": "Polygon", "coordinates": [rect(4, 4, 0)]}  # inside the lawn
    mower.zones = (replace(mower.zones[0], nogo_areas=[centre_hole]), mower.zones[1])
    await sync.async_sync()
    assert (await sync.async_zones())[0]["nogo_areas"] == [centre_hole]
    assert angles() == before  # byte for byte, basis included


async def test_zone_view_carries_the_nogo_areas_unknown_as_none(hass, hass_ws_client) -> None:
    await _attached(hass, FakeMower())
    entry_id = hass.config_entries.async_entries(DOMAIN)[0].entry_id
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "groundskeeper/get_zones", "entry_id": entry_id})
    msg = await ws.receive_json()
    front, back = msg["result"]["zones"]
    assert len(front["nogo_areas"]) == 1
    assert back["nogo_areas"] is None


async def test_a_store_written_before_nogo_areas_loads_and_reads_unknown(
    hass, hass_storage
) -> None:
    from custom_components.groundskeeper.store import (
        STORAGE_VERSION,
        STORAGE_VERSION_MINOR,
        empty_store_data,
    )
    from custom_components.groundskeeper.zone_sync import ZoneSync

    entry = _entry(hass)
    key = f"groundskeeper.{entry.entry_id}"
    data = empty_store_data()
    data["config"]["zones"] = [_zone("z1", "11111111", "Front Lawn")]  # no nogo_areas
    hass_storage[key] = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_VERSION_MINOR,
        "key": key,
        "data": data,
    }
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    store = entry.runtime_data
    sync = ZoneSync(hass, entry.entry_id, FakeMower(), store)

    view = await sync.async_zone_view()
    assert not store.safe_idle
    assert [(z["id"], z["nogo_areas"]) for z in view["zones"]] == [("z1", None)]

    await sync.async_sync()  # the next sync fills it in
    zone = next(z for z in await sync.async_zones() if z["id"] == "z1")
    assert len(zone["nogo_areas"]) == 1
