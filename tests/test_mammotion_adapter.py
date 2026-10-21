"""Mammotion adapter: snapshots, signals, limits (issue #8, spec §3.2/§3.6).

All state is fake: entity registry entries plus `hass.states`, shaped
from the live ExampleSite inventory and the research notes. No real Mammotion
service is ever called.
"""

from __future__ import annotations

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director.adapter import Movement, Snapshot
from custom_components.mowing_director.adapters import ADAPTER_REGISTRY
from custom_components.mowing_director.const import DOMAIN
from tests.conftest import make_mammotion_mower
from tests.mammotion_states import install_baseline, set_entity

NAME = "Luba-SERIAL1"


async def make_director_entry(hass: HomeAssistant) -> tuple[ConfigEntry, object]:
    """A Mowing Director entry pointing at a fake Mammotion Mower device."""
    mammotion_entry, mower, _ = await make_mammotion_mower(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Example Mower", unique_id=mower.id, data={CONF_DEVICE: mower.id}
    )
    entry.add_to_hass(hass)
    return entry, mower


def make_adapter(hass: HomeAssistant, entry: ConfigEntry):
    """Build the registered Mammotion adapter for a director entry."""
    factory = ADAPTER_REGISTRY["mammotion"]
    return factory(hass, entry)


async def test_registry_has_mammotion() -> None:
    """The adapter registry registers `mammotion` (spec §3.1)."""
    assert "mammotion" in ADAPTER_REGISTRY


async def test_snapshot_maps_examplesite_baseline(hass: HomeAssistant) -> None:
    """Baseline ExampleSite states map to the spec §3.2 snapshot fields."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    adapter = make_adapter(hass, entry)

    assert adapter.snapshot == Snapshot(
        activity=LawnMowerActivity.IDLE,
        available=True,
        recharging=False,
        job_active=False,
        battery=80,
        progress=0,
        zone=None,
        # ExampleSite reports inches: 3.1496 in = 80.0 mm (live inventory).
        blade_height_mm=80,
        movement=Movement(progress_pct=0, area_m2=0.0, position=None),
        job_ref=None,
        # rain detection switch is on, so the flag is known: not raining.
        rain=False,
        self_check=None,
    )


async def test_entities_resolve_through_unique_id_not_entity_id(hass: HomeAssistant) -> None:
    """Resolution is by device + unique_id; entity_id history (`example_mower_*`
    vs `luba_serial1_*` prefixes) must not matter (issue #8 AC)."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id("sensor", "mammotion", f"{NAME}_blade_height")
    assert entity_id is not None
    # The live install renamed entity_ids while unique_ids stayed.
    reg.async_update_entity(entity_id, new_entity_id="sensor.garden_example_mower_blade_height")
    hass.states.async_set(
        "sensor.garden_example_mower_blade_height", "3.1496", {"unit_of_measurement": "in"}
    )
    adapter = make_adapter(hass, entry)
    assert adapter.snapshot.blade_height_mm == 80


async def test_rtk_entities_are_ignored(hass: HomeAssistant) -> None:
    """One Mammotion entry holds mower + RTK; only the configured device
    is used (issue #8 AC)."""
    mammotion_entry, mower, rtk = await make_mammotion_mower(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Example Mower", unique_id=mower.id, data={CONF_DEVICE: mower.id}
    )
    entry.add_to_hass(hass)
    install_baseline(hass, mammotion_entry, mower, NAME)
    # A battery sensor on the RTK device must not be read as the Mower's.
    set_entity(hass, mammotion_entry, rtk, f"{NAME}-rtk", "sensor:battery_percent", "7")
    adapter = make_adapter(hass, entry)
    assert adapter.snapshot.battery == 80


async def test_blade_height_unit_conversion_and_guards(hass: HomeAssistant) -> None:
    """Unit-aware conversion; None when unknown/non-numeric (reconnect blip)."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    adapter = make_adapter(hass, entry)

    # mm unit passes through.
    set_entity(hass, entry, mower, NAME, "sensor:blade_height", "65", {"unit_of_measurement": "mm"})
    assert adapter.snapshot.blade_height_mm == 65

    # unknown -> None (the reconnect blip: sensor goes unknown, then back).
    set_entity(hass, entry, mower, NAME, "sensor:blade_height", "unknown", {})
    assert adapter.snapshot.blade_height_mm is None

    # non-numeric -> None.
    set_entity(hass, entry, mower, NAME, "sensor:blade_height", "false", {})
    assert adapter.snapshot.blade_height_mm is None

    # missing entity -> None.
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id("sensor", "mammotion", f"{NAME}_blade_height")
    hass.states.async_remove(entity_id)
    assert adapter.snapshot.blade_height_mm is None
