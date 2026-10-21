"""Fake hass state plumbing for Mammotion adapter tests (issue #8).

`make_mammotion_mower` (conftest) creates the entry, devices and registry
entries but NO states, mirroring what a not-yet-loaded Mammotion looks
like. This module installs ExampleSite-shaped states on top: every entity is
created in the entity registry with the real `mammotion` unique_id
pattern (`{device_name}_{key}`) and given a state.

Entity keys are written `domain:key` here; the full unique_id is
`{name}_{key}`, exactly what the adapter must resolve through the
registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import entity_registry as er

ZONE_JSON = Path(__file__).parents[1] / "tests/fixtures/zone.json"

MAMMOTION = "mammotion"


def zone_geojson() -> dict[str, Any]:
    """The live ExampleSite map (7 areas, 8 paths) used by the catalogue tests."""
    return json.loads(ZONE_JSON.read_text())


def baseline_states(name: str = "Luba-SERIAL1") -> dict[str, tuple[str, dict[str, Any]]]:
    """ExampleSite-shaped baseline for one Mower, from research/live-examplesite-inventory.

    ExampleSite reports blade height in inches (`3.1496 in` = 80 mm); the blade
    number entity carries the H-variant device limits 55-100 mm step 1.
    """
    del name  # keys only; friendly names are not asserted
    return {
        "lawn_mower:mower": ("idle", {}),
        "sensor:battery_percent": ("80", {}),
        "sensor:progress": ("0", {}),
        "sensor:area": ("0", {}),
        "sensor:work_area": ("Not working", {}),
        "sensor:blade_height": ("3.1496", {"unit_of_measurement": "in"}),
        "sensor:activity_mode": ("MODE_READY", {}),
        "sensor:self_check": ("normal", {}),
        "sensor:error_1_code": ("0", {}),
        "sensor:error_1_message": ("No Error", {}),
        "sensor:error_1_time": ("unknown", {}),
        "binary_sensor:charging": ("off", {}),
        "binary_sensor:rain_detected": ("off", {}),
        "switch:rain_detection_mower_on_off": ("on", {}),
        "number:blade_height": (
            "55",
            {"min_value": 55, "max_value": 100, "step": 1, "unit_of_measurement": "mm"},
        ),
        "number:path_spacing": (
            "20",
            {"min_value": 20, "max_value": 35, "step": 1, "unit_of_measurement": "cm"},
        ),
        "number:working_speed": (
            "0.2",
            {"min_value": 0.2, "max_value": 0.6, "step": 0.1, "unit_of_measurement": "m/s"},
        ),
        "number:recharge_level": ("20", {"min_value": 15, "max_value": 30, "step": 1}),
        "number:resume_level": ("50", {"min_value": 40, "max_value": 100, "step": 1}),
        "switch:smart_recharge_level": ("off", {}),
        "switch:smart_resume_level": ("off", {}),
    }


def set_entity(
    hass: HomeAssistant,
    entry: Any,
    device: Any,
    name: str,
    key: str,
    state: str,
    attributes: dict[str, Any] | None = None,
) -> str:
    """Registry-create (if needed) and state-set one `mammotion` entity.

    `key` is the unique_id suffix: `blade_height`, `{hash}` for a Zone
    switch, `{hash}_task_area` for an ephemeral task sensor.
    """
    domain, _, suffix = key.partition(":")
    unique_id = f"{name}_{suffix}"
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id(domain, MAMMOTION, unique_id)
    if entity_id is None:
        created = reg.async_get_or_create(
            domain,
            MAMMOTION,
            unique_id,
            config_entry=entry,
            device_id=device.id,
        )
        entity_id = created.entity_id
    hass.states.async_set(entity_id, state, attributes or {})
    return entity_id


def install_states(
    hass: HomeAssistant,
    entry: Any,
    device: Any,
    name: str,
    states: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    """Apply a `{domain:key: (state, attributes)}` map as hass states."""
    for key, (state, attributes) in states.items():
        set_entity(hass, entry, device, name, key, state, attributes)


def install_baseline(
    hass: HomeAssistant, entry: Any, device: Any, name: str = "Luba-SERIAL1"
) -> None:
    """Install the ExampleSite-shaped baseline states for one Mower."""
    install_states(hass, entry, device, name, baseline_states(name))


def install_zone_switches(
    hass: HomeAssistant, entry: Any, device: Any, name: str, geojson: dict[str, Any] | None = None
) -> None:
    """Create the Zone switches for the map, as hass-mammotion does.

    unique_id `{name}_{hash}`, `hash` in the state attributes.
    """
    for feat in (geojson or zone_geojson())["features"]:
        props = feat["properties"]
        if props.get("type_name") != "area":
            continue
        reg = er.async_get(hass)
        entity = reg.async_get_or_create(
            "switch",
            MAMMOTION,
            f"{name}_{props['hash']}",
            suggested_object_id=str(props["Name"]).lower().replace(" ", "_"),
            config_entry=entry,
            device_id=device.id,
            original_name=props["Name"],
        )
        hass.states.async_set(
            entity.entity_id,
            "on",
            {"hash": props["hash"], "friendly_name": props["Name"]},
        )


def register_fake_mammotion_services(
    hass: HomeAssistant,
    geojson: dict[str, Any] | None = None,
    tasks: dict[str, Any] | None = None,
) -> None:
    """Fake the read-only `mammotion` response services (never the real ones).

    `get_geojson` starts a 5-minute report stream on the real Mower, so
    tests must never call it for real; these fakes answer in-process.
    """
    geojson = geojson if geojson is not None else zone_geojson()
    tasks = tasks or {
        "tasks": [
            {
                "task_id": "plan-1",
                "entity_id": "button.example_mower_backyard",
                "name": "Backyard",
                "enabled": False,
                "running": False,
            }
        ]
    }

    async def get_geojson(call: Any) -> dict[str, Any]:
        return {call.data["entity_id"][0]: geojson}

    async def get_tasks(call: Any) -> dict[str, Any]:
        return {call.data["entity_id"][0]: tasks}

    hass.services.async_register(
        MAMMOTION, "get_geojson", get_geojson, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        MAMMOTION, "get_tasks", get_tasks, supports_response=SupportsResponse.ONLY
    )
