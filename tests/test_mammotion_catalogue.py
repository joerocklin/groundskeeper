"""Mammotion catalogue (Zones, Channels, native tasks) - issue #9, spec §3.4/§4.6.

Tests drive the public `MammotionCatalogue` seam against ExampleSite-shaped data
(`zone.json`, 8 Channels, dock joins only Backyard) with the Mammotion
services faked. No real service is ever called.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.groundskeeper.adapter import (
    Accepted,
    Channel,
    NativeTask,
    Refused,
    Unconfirmed,
    Zone,
    ZonesChanged,
)
from custom_components.groundskeeper.adapters.mammotion_catalogue import (
    MammotionCatalogue,
)

from .conftest import make_mammotion_mower

ZONE_JSON = (
    Path(__file__).parents[1]
    / "tests/fixtures/zone.json"
)

BACKYARD = "1000000000000000001"
FRONT = "1000000000000000002"
FENCE = "1000000000000000006"
SIDE = "1000000000000000007"

TASKS = {
    "tasks": [
        {
            "task_id": "plan-1",
            "entity_id": "button.example_mower_backyard",
            "name": "Backyard",
            "enabled": False,
            "running": False,
        },
        {
            "task_id": "plan-2",
            "entity_id": "button.example_mower_task_2",
            "name": "Front yard",
            "enabled": True,
            "running": False,
        },
    ]
}


def _geojson() -> dict:
    return json.loads(ZONE_JSON.read_text())


class FakeServices:
    """Registers fake mammotion response services and records calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.geojson = _geojson()
        self.tasks = TASKS
        self.calls: list[tuple[str, ServiceCall]] = []
        self.toggle_error: Exception | None = None
        self.entity_wrapped = True

        async def get_geojson(call: ServiceCall):
            self.calls.append(("get_geojson", call))
            return self._wrap(call, self.geojson)

        async def get_tasks(call: ServiceCall):
            self.calls.append(("get_tasks", call))
            return self._wrap(call, self.tasks)

        async def set_task_enabled(call: ServiceCall):
            self.calls.append(("set_task_enabled", call))
            if self.toggle_error:
                raise self.toggle_error

        only = SupportsResponse.ONLY
        hass.services.async_register(
            "mammotion", "get_geojson", get_geojson, supports_response=only
        )
        hass.services.async_register(
            "mammotion", "get_tasks", get_tasks, supports_response=only
        )
        hass.services.async_register("mammotion", "set_task_enabled", set_task_enabled)

    def _wrap(self, call: ServiceCall, payload: dict) -> dict:
        # Entity services return {entity_id: response}.
        if not self.entity_wrapped:
            return payload
        return {call.data["entity_id"][0]: payload}

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


def _add_zone_switches(hass, entry, device, geojson: dict) -> None:
    ent_reg = er.async_get(hass)
    for feat in geojson["features"]:
        p = feat["properties"]
        if p.get("type_name") != "area":
            continue
        entity = ent_reg.async_get_or_create(
            "switch",
            "mammotion",
            str(p["hash"]),
            suggested_object_id=p["Name"].lower().replace(" ", "_"),
            config_entry=entry,
            device_id=device.id,
            original_name=f"Area {p['Name']}",  # Mammotion's "Area {name}" switch
        )
        hass.states.async_set(
            entity.entity_id,
            "on",
            {"hash": p["hash"], "friendly_name": f"Area {p['Name']}"},
        )


@pytest.fixture
async def setup(hass: HomeAssistant):
    entry, device, _ = await make_mammotion_mower(hass)
    services = FakeServices(hass)
    _add_zone_switches(hass, entry, device, services.geojson)
    signals: list = []
    catalogue = MammotionCatalogue(hass, device.id, signals.append)
    return catalogue, services, signals, entry, device


async def test_list_zones_returns_ref_name_polygon(setup) -> None:
    catalogue, *_ = setup
    zones = await catalogue.list_zones()
    by_ref = {z.native_ref: z for z in zones}
    assert len(zones) == 7
    backyard = by_ref[BACKYARD]
    assert isinstance(backyard, Zone)
    assert backyard.name == "Backyard"
    assert backyard.polygon["type"] == "Polygon"
    assert by_ref[SIDE].name == "Side yard"


async def test_zones_without_geometry_when_fetch_fails(setup) -> None:
    catalogue, services, *_ = setup
    services.geojson = None  # makes the fake raise below

    async def boom(call):
        raise HomeAssistantError("offline")

    catalogue._hass.services.async_register(
        "mammotion", "get_geojson", boom, supports_response=SupportsResponse.ONLY
    )
    zones = await catalogue.list_zones()
    assert len(zones) == 7
    assert all(z.polygon is None for z in zones)
    # No map to read the plain name from: fall back to the switch's own name.
    assert {z.native_ref: z.name for z in zones}[BACKYARD] == "Area Backyard"


async def test_zone_without_matching_map_feature_uses_switch_name(setup) -> None:
    catalogue, _, _, entry, device = setup
    # A switch for a Zone the map does not (yet) carry, e.g. a rebuilt map.
    # A genuine "Area 51" Zone has no extra prefix (Mammotion's `area_plain`).
    ent = er.async_get(catalogue._hass).async_get_or_create(
        "switch",
        "mammotion",
        "1000000000000000099",
        suggested_object_id="area_51",
        config_entry=entry,
        device_id=device.id,
        original_name="Area 51",
    )
    catalogue._hass.states.async_set(
        ent.entity_id, "on", {"hash": 1000000000000000099, "friendly_name": "Area 51"}
    )
    by_ref = {z.native_ref: z for z in await catalogue.list_zones()}
    assert by_ref["1000000000000000099"].name == "Area 51"
    assert by_ref[BACKYARD].name == "Backyard"


async def test_zone_discovery_uses_unique_id_when_switch_unavailable(setup) -> None:
    catalogue, _, _, entry, device = setup
    # An unavailable switch has no `hash` attribute (research §5).
    reg = er.async_get(catalogue._hass)
    ent = reg.async_get_entity_id("switch", "mammotion", BACKYARD)
    catalogue._hass.states.async_set(ent, "unavailable", {})
    zones = await catalogue.list_zones()
    assert BACKYARD in {z.native_ref for z in zones}


async def test_geometry_fetched_once_until_asked(setup) -> None:
    catalogue, services, *_ = setup
    await catalogue.list_zones()
    await catalogue.list_channels()
    await catalogue.list_zones()
    assert services.count("get_geojson") == 1  # 5-minute report stream
    await catalogue.async_refresh_geometry()
    assert services.count("get_geojson") == 2


async def test_geojson_targets_the_device_lawn_mower_entity(setup) -> None:
    catalogue, services, *_ = setup
    await catalogue.list_zones()
    reg = er.async_get(catalogue._hass)
    mower_entity = next(
        e.entity_id for e in reg.entities.values() if e.domain == "lawn_mower"
    )
    assert services.calls[0][1].data["entity_id"] == [mower_entity]


async def test_list_channels_eight_dock_joins_only_backyard(setup) -> None:
    catalogue, *_ = setup
    channels = await catalogue.list_channels()
    assert len(channels) == 8
    assert all(isinstance(c, Channel) for c in channels)
    dock_links = [c for c in channels if Channel.DOCK in (c.end_a, c.end_b)]
    assert len(dock_links) == 1
    other = {dock_links[0].end_a, dock_links[0].end_b} - {Channel.DOCK}
    assert other == {BACKYARD}
    by_ref = {c.native_ref: c for c in channels}
    path1 = by_ref["1000000000000000008"]
    assert (path1.end_a, path1.end_b) == (FRONT, BACKYARD)


async def test_zone_list_change_signals_and_refetches_geometry(setup) -> None:
    catalogue, services, signals, entry, device = setup
    catalogue.async_start()
    await catalogue.list_zones()
    hass = catalogue._hass
    reg = er.async_get(hass)
    ent = reg.async_get_entity_id("switch", "mammotion", BACKYARD)

    # Map rebuild re-keys the same-named Zone to a new hash (research §5).
    hass.states.async_set(ent, "on", {"hash": 999, "friendly_name": "Backyard"})
    await hass.async_block_till_done()
    assert signals == [ZonesChanged()]

    services.geojson = _geojson()
    await catalogue.list_zones()
    assert services.count("get_geojson") == 2  # refetched after map update

    # No change in the hash set -> no further signal.
    hass.states.async_set(ent, "off", {"hash": 999, "friendly_name": "Backyard"})
    await hass.async_block_till_done()
    assert signals == [ZonesChanged()]
    catalogue.async_stop()


async def test_list_native_tasks_read_only(setup) -> None:
    catalogue, services, *_ = setup
    tasks = await catalogue.list_native_tasks()
    assert tasks == [
        NativeTask("plan-1", "Backyard", False),
        NativeTask("plan-2", "Front yard", True),
    ]
    assert services.count("set_task_enabled") == 0


async def test_set_task_enabled_targets_task_button(setup) -> None:
    catalogue, services, *_ = setup
    await catalogue.list_native_tasks()
    result = await catalogue.set_task_enabled("plan-2", False)
    assert result == Accepted()
    call = services.calls[-1][1]
    assert call.data["entity_id"] == ["button.example_mower_task_2"]
    assert call.data["enabled"] is False


async def test_set_task_enabled_unknown_task_is_refused(setup) -> None:
    catalogue, services, *_ = setup
    result = await catalogue.set_task_enabled("nope", True)
    assert isinstance(result, Refused)
    assert services.count("set_task_enabled") == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ServiceValidationError("no"), Refused),
        (HomeAssistantError("unknown"), Unconfirmed),
    ],
)
async def test_set_task_enabled_maps_failures(setup, error, expected) -> None:
    catalogue, services, *_ = setup
    services.toggle_error = error
    result = await catalogue.set_task_enabled("plan-1", True)
    assert isinstance(result, expected)


async def test_list_channels_carry_the_path_geometry(setup) -> None:
    catalogue, *_ = setup
    by_ref = {c.native_ref: c for c in await catalogue.list_channels()}
    path1 = by_ref["1000000000000000008"]
    assert path1.path["type"] == "LineString"
    assert len(path1.path["coordinates"]) == 8
    assert len(by_ref["1000000000000000009"].path["coordinates"]) == 2


async def test_list_channels_only_keep_line_string_geometry(setup) -> None:
    catalogue, services, *_ = setup
    for feat in services.geojson["features"]:
        if feat["properties"].get("hash") == 1000000000000000008:
            feat["geometry"] = {"type": "MultiLineString", "coordinates": []}
        if feat["properties"].get("hash") == 1000000000000000009:
            feat["geometry"] = {"type": "LineString", "coordinates": [[0, 0]]}
    by_ref = {c.native_ref: c for c in await catalogue.list_channels()}
    assert by_ref["1000000000000000008"].path is None
    assert by_ref["1000000000000000009"].path is None  # a line needs two points
    assert by_ref["1000000000000000010"].path is not None
