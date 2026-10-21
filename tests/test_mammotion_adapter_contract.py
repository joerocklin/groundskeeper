"""Mammotion adapter contract suite (issue #8 AC, spec §18.2).

Runs the reusable `_AdapterContract` against the real adapter over fake
hass states shaped from the live ExampleSite inventory. The injection hooks
drive the entities a real Mammotion would update: the rolling error
sensors for errors, the `mammotion_event` bus for rain stop, and
ordinary state changes for snapshots. `script_result` stays skipped:
commands are read-only stubs until ticket #10, and scripting a stub
would test the script, not the adapter.
"""

from __future__ import annotations

import pytest
from homeassistant.const import CONF_DEVICE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director.adapter import MowerAdapter, NewError
from custom_components.mowing_director.adapters import get_adapter_factory
from custom_components.mowing_director.adapters.mammotion import MammotionAdapter
from custom_components.mowing_director.const import DOMAIN
from tests.adapter_contract import _AdapterContract
from tests.conftest import make_mammotion_mower
from tests.mammotion_states import (
    install_baseline,
    install_zone_switches,
    register_fake_mammotion_services,
    set_entity,
)

NAME = "Luba-SERIAL1"


class TestMammotionAdapterContract(_AdapterContract):
    """The Mammotion adapter must satisfy the full adapter contract."""

    @pytest.fixture
    async def mower(self, hass: HomeAssistant) -> MowerAdapter:
        """A Mammotion adapter over fake ExampleSite-shaped state."""
        mammotion_entry, device, _ = await make_mammotion_mower(hass)
        install_baseline(hass, mammotion_entry, device, NAME)
        install_zone_switches(hass, mammotion_entry, device, NAME)
        register_fake_mammotion_services(hass)
        entry = MockConfigEntry(
            domain=DOMAIN, title="Example Mower", unique_id=device.id, data={CONF_DEVICE: device.id}
        )
        entry.add_to_hass(hass)
        self._hass = hass
        self._mammotion_entry = mammotion_entry
        self._device = device
        return get_adapter_factory("mammotion")(hass, entry)

    async def inject_error(self, mower: MowerAdapter, error: NewError) -> None:
        """Drive the rolling error sensors, as the error coordinator does."""
        assert isinstance(mower, MammotionAdapter)
        set_entity(
            self._hass,
            self._mammotion_entry,
            self._device,
            NAME,
            "sensor:error_1_code",
            error.code,
        )
        set_entity(
            self._hass,
            self._mammotion_entry,
            self._device,
            NAME,
            "sensor:error_1_message",
            error.message,
        )
        set_entity(
            self._hass,
            self._mammotion_entry,
            self._device,
            NAME,
            "sensor:error_1_time",
            error.time.isoformat(),
        )
        await self._hass.async_block_till_done()

    async def inject_rain_stop(self, mower: MowerAdapter) -> None:
        """Fire the cloud `mammotion_event` with code 1003 (research §7)."""
        assert isinstance(mower, MammotionAdapter)
        self._hass.bus.async_fire(
            "mammotion_event",
            {
                "type": "device_warning_code_event",
                "device_id": self._device.id,
                "device_name": NAME,
                "codes": [{"code": 1003, "message": "rain sensor triggered, job ended"}],
            },
        )
        await self._hass.async_block_till_done()

    async def push_snapshot(self, mower: MowerAdapter, snapshot) -> None:
        """Re-derive the pushed snapshot from a state change: battery."""
        assert isinstance(mower, MammotionAdapter)
        assert snapshot.battery is not None
        set_entity(
            self._hass,
            self._mammotion_entry,
            self._device,
            NAME,
            "sensor:battery_percent",
            str(snapshot.battery),
        )
        await self._hass.async_block_till_done()
