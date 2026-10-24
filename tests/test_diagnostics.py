"""Diagnostics download: snapshot + limits, serial redacted (issue #8 AC)."""

from __future__ import annotations

import json

from homeassistant.const import CONF_DEVICE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.groundskeeper.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.mammotion_states import install_baseline

from .test_mammotion_adapter import NAME, make_director_entry


async def test_diagnostics_snapshot_limits_and_redaction(hass: HomeAssistant) -> None:
    """The download carries the snapshot and limits with the serial gone."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)

    data = await async_get_config_entry_diagnostics(hass, entry)

    assert data["snapshot"]["activity"] == "idle"
    assert data["snapshot"]["blade_height_mm"] == 80
    assert data["limits"]["height_min_mm"] == 55
    assert data["limits"]["height_max_mm"] == 100
    assert data["metadata"]["source_domain"] == "mammotion"
    assert data["metadata"]["device_name"] == "**REDACTED**"

    # The payload is JSON-serialisable end to end (it is a download).
    text = json.dumps(data)
    assert "SERIAL1" not in text
    assert "Luba-SERIAL1" not in text
    assert "**REDACTED**" in text


async def test_diagnostics_without_mower_device(hass: HomeAssistant) -> None:
    """A stale entry whose device is gone still answers without crashing."""
    entry = MockConfigEntry(
        domain="groundskeeper", title="Gone", unique_id="nope", data={CONF_DEVICE: "nope"}
    )
    entry.add_to_hass(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data == {"error": "no adapter for this Mower"}
