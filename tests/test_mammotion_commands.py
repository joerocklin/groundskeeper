"""Mammotion adapter commands (issue #10, spec §3.3/§3.4/§3.7).

Every Mammotion service is a phacc mock: no test ever commands a live
Mower. The mapping under test is upstream's error surface (research
t10-mammotion-commands.md §2/§3) translated into the closed command
union, and the never-reissue rule (ADR 0002): every failure test
asserts exactly one service call.
"""

from __future__ import annotations

import pytest
from homeassistant.const import CONF_DEVICE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceNotSupported,
    ServiceValidationError,
)
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.mowing_director.adapter import (
    Accepted,
    Refused,
    RunSettings,
    Unconfirmed,
)
from custom_components.mowing_director.adapters.mammotion import MammotionAdapter
from tests.mammotion_states import install_baseline, install_zone_switches

from .test_mammotion_adapter import NAME, make_adapter, make_director_entry

# Side yard from the live ExampleSite map (tests/mammotion_states.py).
SIDE_YARD = "1000000000000000007"

# Missing feature on an older Mammotion-HA (v0.6.18 declares STOP
# unconditionally; main gates on HA >= 2026.10, research §2).
_NOT_SUPPORTED = ServiceNotSupported("lawn_mower", "dock", "lawn_mower.example_mower")


async def make_command_adapter(hass: HomeAssistant) -> MammotionAdapter:
    """Adapter over the ExampleSite-shaped baseline plus Zone switches."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    install_zone_switches(hass, entry, mower, NAME)
    return make_adapter(hass, entry)


def settings(bearing_deg: float = 135.0) -> RunSettings:
    """Run settings with every extras field at its schema default."""
    return RunSettings(
        blade_height_mm=70,
        bearing_deg=bearing_deg,
        spacing_cm=22.0,
        speed_m_s=0.6,
        perimeter_laps=1,
        extras={
            "border_mode": 0,
            "obstacle_laps": 0,
            "channel_mode": 0,
            "toward_included_angle": 90,
            "ultra_wave": "1",
            "auto_change_direction": 0,
            "ride_boundary_distance": 0.0,
        },
    )


# -- result mapping (spec §3.3, research §3) ---------------------------------


async def test_dock_without_any_services_is_refused_not_ready(hass: HomeAssistant) -> None:
    """Mammotion services not yet loaded -> refused with a not-ready
    reason, never an exception (issue #10 AC)."""
    adapter = await make_command_adapter(hass)
    result = await adapter.dock()
    assert result == Refused("mammotion_not_ready")


@pytest.mark.parametrize(
    ("raise_exception", "expected"),
    [
        # 400s are definite nos and carry the message (spec §3.3).
        (
            ServiceValidationError("blade height out of range"),
            Refused("blade height out of range"),
        ),
        # (ServiceNotSupported asserted in its own test: its message is
        # translated at raise time and needs the hass context)
        # Mode-wait timeouts and the false-500 are outcome-unknown.
        (
            HomeAssistantError(
                "did not confirm", translation_domain="mammotion",
                translation_key="command_unconfirmed",
            ),
            Unconfirmed(),
        ),
        (
            HomeAssistantError(
                "pause wait expired", translation_domain="mammotion",
                translation_key="pause_failed",
            ),
            Unconfirmed(),
        ),
        (
            HomeAssistantError(
                "start wait expired", translation_domain="mammotion",
                translation_key="start_failed",
            ),
            Unconfirmed(),
        ),
        (
            HomeAssistantError(
                "resume wait expired", translation_domain="mammotion",
                translation_key="resume_failed",
            ),
            Unconfirmed(),
        ),
        (
            HomeAssistantError(
                "dock wait expired", translation_domain="mammotion",
                translation_key="dock_failed",
            ),
            Unconfirmed(),
        ),
        (
            HomeAssistantError(
                "dock cancel wait expired", translation_domain="mammotion",
                translation_key="dock_cancel_failed",
            ),
            Unconfirmed(),
        ),
        # 500s that are definite nos.
        (
            HomeAssistantError(
                "device not ready", translation_domain="mammotion",
                translation_key="device_not_ready",
            ),
            Refused("device not ready"),
        ),
        (
            HomeAssistantError(
                "account in use", translation_domain="mammotion",
                translation_key="account_in_use",
            ),
            Refused("account in use"),
        ),
        (
            HomeAssistantError(
                "api limit", translation_domain="mammotion",
                translation_key="api_limit_exceeded",
            ),
            Refused("api limit"),
        ),
        (
            HomeAssistantError(
                "not in account", translation_domain="mammotion",
                translation_key="device_not_in_account",
            ),
            Refused("not in account"),
        ),
        (
            HomeAssistantError(
                "command failed", translation_domain="mammotion",
                translation_key="command_failed",
            ),
            Refused("command failed"),
        ),
        (
            HomeAssistantError(
                "no job to continue", translation_domain="mammotion",
                translation_key="no_job_to_continue",
            ),
            Refused("no job to continue"),
        ),
        # Unknown 500: outcome unknown, never retry (ADR 0002).
        (
            HomeAssistantError(
                "surprise", translation_domain="mammotion",
                translation_key="something_new",
            ),
            Unconfirmed(),
        ),
        (HomeAssistantError("bare error"), Unconfirmed()),
    ],
    ids=[
        "validation", "command_unconfirmed", "pause_failed",
        "start_failed", "resume_failed", "dock_failed", "dock_cancel_failed",
        "device_not_ready", "account_in_use", "api_limit_exceeded",
        "device_not_in_account", "command_failed", "no_job_to_continue",
        "unknown_key", "bare",
    ],
)
async def test_dock_maps_upstream_errors_to_the_closed_union(
    hass: HomeAssistant, raise_exception: Exception, expected: object
) -> None:
    """One dock call maps every upstream error shape to its CommandResult;
    exactly one call even on failure (ADR 0002)."""
    adapter = await make_command_adapter(hass)
    calls = async_mock_service(
        hass, "lawn_mower", "dock", raise_exception=raise_exception
    )
    result = await adapter.dock()
    assert result == expected
    assert len(calls) == 1


async def test_service_not_supported_is_a_plain_refused(hass: HomeAssistant) -> None:
    """ServiceNotSupported (feature gated on an older Mammotion-HA)
    subclasses ServiceValidationError, so it lands as refused with the
    translated message (research §2 stop/dock)."""
    adapter = await make_command_adapter(hass)
    calls = async_mock_service(
        hass, "lawn_mower", "dock", raise_exception=_NOT_SUPPORTED
    )
    result = await adapter.dock()
    assert isinstance(result, Refused)
    assert result.reason == str(_NOT_SUPPORTED)
    assert len(calls) == 1


async def test_service_not_found_is_caught_before_validation_error(
    hass: HomeAssistant,
) -> None:
    """ServiceNotFound subclasses ServiceValidationError: the not-ready
    reason must win over the bare message (research §3 catch order)."""
    adapter = await make_command_adapter(hass)
    calls = async_mock_service(
        hass, "lawn_mower", "dock", raise_exception=ServiceNotFound("lawn_mower", "dock")
    )
    result = await adapter.dock()
    assert result == Refused("mammotion_not_ready")
    assert len(calls) == 1


async def test_dock_accepted(hass: HomeAssistant) -> None:
    """A clean dock is accepted with exactly one call."""
    adapter = await make_command_adapter(hass)
    calls = async_mock_service(hass, "lawn_mower", "dock")
    assert await adapter.dock() == Accepted()
    assert len(calls) == 1
