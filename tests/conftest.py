"""Shared fixtures for Mowing Director tests."""

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield


async def make_mammotion_mower(
    hass,
    name: str = "Luba-SERIAL1",
    nick: str = "Example Mower",
    *,
    with_rtk: bool = True,
):
    """Create a fake mammotion entry owning a Mower device and an RTK station.

    Mirrors the live hass-mammotion shape: one config entry (account) owns
    several devices; only the Mower has a lawn_mower entity.
    Returns (mammotion_entry, mower_device, rtk_device_or_None).
    """
    mammotion_entry = MockConfigEntry(
        domain="mammotion", title="Mammotion", unique_id="acct-1"
    )
    mammotion_entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    mower = dev_reg.async_get_or_create(
        config_entry_id=mammotion_entry.entry_id,
        identifiers={("mammotion", name)},
        name=name,
    )
    dev_reg.async_update_device(mower.id, name_by_user=nick)
    ent_reg.async_get_or_create(
        "lawn_mower",
        "mammotion",
        f"{name}_mower",
        suggested_object_id=name.lower(),
        config_entry=mammotion_entry,
        device_id=mower.id,
    )

    rtk = None
    if with_rtk:
        rtk = dev_reg.async_get_or_create(
            config_entry_id=mammotion_entry.entry_id,
            identifiers={("mammotion", f"{name}-rtk")},
            name="RTK",
        )
        ent_reg.async_get_or_create(
            "switch",
            "mammotion",
            f"{name}-rtk_power",
            config_entry=mammotion_entry,
            device_id=rtk.id,
        )
    return mammotion_entry, mower, rtk


@pytest.fixture(autouse=True)
def _work_hours_always_open(request, monkeypatch):
    """Work hours default to 08:00-00:00 (spec §9.2); other suites must not
    depend on the wall clock, so their default window is the whole day."""
    if "test_checks" not in request.module.__name__:
        monkeypatch.setattr(
            "custom_components.mowing_director.checks.DEFAULT_WINDOW", ("00:00", "00:00")
        )
