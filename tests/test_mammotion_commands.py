"""Mammotion adapter commands (issue #10, spec §3.3/§3.4/§3.7).

Every Mammotion service is a phacc mock: no test ever commands a live
Mower. The mapping under test is upstream's error surface (research
t10-mammotion-commands.md §2/§3) translated into the closed command
union, and the never-reissue rule (ADR 0002): every failure test
asserts exactly one service call.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceNotSupported,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockModule,
    async_mock_service,
    mock_integration,
)

from custom_components.groundskeeper.adapter import (
    Accepted,
    JobStarted,
    Refused,
    RunSettings,
    Unconfirmed,
)
from custom_components.groundskeeper.adapters.mammotion import MammotionAdapter
from tests.mammotion_states import install_baseline, install_zone_switches, set_entity

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


def set_mode(hass: HomeAssistant, mode: str) -> None:
    """Drive the raw `activity_mode` sensor state."""
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id("sensor", "mammotion", f"{NAME}_activity_mode")
    assert entity_id is not None
    hass.states.async_set(entity_id, mode, {})


def mock_lawn_mower(
    hass: HomeAssistant,
    stop_raises: Exception | None = None,
    dock_raises: Exception | None = None,
) -> tuple[list[tuple[str, str]], list, list]:
    """Mock `lawn_mower.stop` and `lawn_mower.dock` with a shared call
    order log plus per-service call logs."""
    order: list[tuple[str, str]] = []
    stops: list = []
    docks: list = []

    def log(target: list, exception: Exception | None):
        async def _handler(call) -> None:
            order.append((call.domain, call.service))
            target.append(call)
            if exception is not None:
                raise exception

        return _handler

    hass.services.async_register("lawn_mower", "stop", log(stops, stop_raises))
    hass.services.async_register("lawn_mower", "dock", log(docks, dock_raises))
    return order, stops, docks


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


# -- start_run payload and pre-checks (spec §3.4/§3.7, research §4) ----------


async def test_start_run_sends_one_start_mow_with_every_route_field(
    hass: HomeAssistant,
) -> None:
    """Exact payload pin: one `mammotion.start_mow`, exactly one Zone in
    `areas`, every route field explicit (omitted fields would fall back
    to HA-side select/number defaults), and never `modify` (issue #10
    AC, spec §3.4)."""
    adapter = await make_command_adapter(hass)
    calls = async_mock_service(hass, "mammotion", "start_mow")
    result = await adapter.start_run(SIDE_YARD, settings(bearing_deg=135.0))
    assert result == Accepted()
    assert len(calls) == 1
    call = calls[0]
    assert (call.domain, call.service) == ("mammotion", "start_mow")
    assert call.data == {
        "entity_id": ["lawn_mower.luba_serial1"],
        "areas": ["switch.side_yard"],
        "blade_height": 70,
        "toward_mode": 1,
        "toward": -45,
        "channel_width": 22,
        "speed": 0.6,
        "mowing_laps": 1,
        # Explicit 0: a stale number entity must not resume mid-Zone.
        "start_progress": 0,
        "border_mode": 0,
        "obstacle_laps": 0,
        "channel_mode": 0,
        # Always sent: pymammotion zeroes it unless channel_mode == 1.
        "toward_included_angle": 90,
        # The extras default is the string "1"; the schema wants an int.
        "ultra_wave": 1,
        "auto_change_direction": 0,
        "ride_boundary_distance": 0.0,
    }
    assert "modify" not in call.data


@pytest.mark.parametrize(
    ("bearing_deg", "toward"),
    [
        (0.0, 90),
        (5.0, 85),
        (60.0, 30),
        (90.0, 0),
        (100.0, -10),
        (135.0, -45),
        (175.0, -85),
        # 180 folds to +90: a stripe has no direction, +/-90 is one line.
        (180.0, 90),
        (190.0, 80),
    ],
)
async def test_start_run_folds_bearing_into_minus_90_to_90(
    hass: HomeAssistant, bearing_deg: float, toward: int
) -> None:
    """`toward = 90 - bearing` folded into -90..90 (spec §3.7)."""
    adapter = await make_command_adapter(hass)
    calls = async_mock_service(hass, "mammotion", "start_mow")
    assert await adapter.start_run(SIDE_YARD, settings(bearing_deg)) == Accepted()
    assert len(calls) == 1
    assert calls[0].data["toward"] == toward


@pytest.mark.parametrize(
    "mode",
    [
        "MODE_LOCK",
        "MODE_CHARGING",
        "MODE_CHARGING_PAUSE",
        "MODE_SYSTEM_ERROR",
        "MODE_UPDATING",
        "MODE_MANUAL_MOWING",
        "MODE_SLEEPING",
        "MODE_NOT_A_MODE_AT_ALL",
    ],
)
async def test_start_run_pre_checks_modes_where_start_mow_silently_no_ops(
    hass: HomeAssistant, mode: str
) -> None:
    """LOCK, CHARGING and other non-route modes return HTTP 200 and do
    nothing upstream, so the adapter refuses them with ZERO service
    calls (issue #10 AC, research §2)."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, mode)
    calls = async_mock_service(hass, "mammotion", "start_mow")
    result = await adapter.start_run(SIDE_YARD, settings())
    assert isinstance(result, Refused)
    assert mode in result.reason
    assert calls == []


async def test_start_run_without_activity_mode_is_refused(hass: HomeAssistant) -> None:
    """No `activity_mode` state (device not reporting): refused, no call -
    upstream raises device_not_ready, but the pre-check keeps it local."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, "unavailable")
    calls = async_mock_service(hass, "mammotion", "start_mow")
    result = await adapter.start_run(SIDE_YARD, settings())
    assert isinstance(result, Refused)
    assert calls == []


@pytest.mark.parametrize("mode", ["MODE_READY", "MODE_WORKING", "MODE_PAUSE"])
async def test_start_run_allows_route_modes(
    hass: HomeAssistant, mode: str
) -> None:
    """The modes upstream handles (READY/WORKING/PAUSE, plus
    INITIALIZATION/RETURNING) pass the pre-check and send one call."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, mode)
    calls = async_mock_service(hass, "mammotion", "start_mow")
    assert await adapter.start_run(SIDE_YARD, settings()) == Accepted()
    assert len(calls) == 1


async def test_start_run_unknown_zone_is_refused_without_a_call(
    hass: HomeAssistant,
) -> None:
    """A zone the device does not carry is a definite no: refused, zero
    calls (a wrong `areas` entry would be dropped upstream and fall back
    to the ON switches)."""
    adapter = await make_command_adapter(hass)
    calls = async_mock_service(hass, "mammotion", "start_mow")
    result = await adapter.start_run("zone-1", settings())
    assert isinstance(result, Refused)
    assert "zone-1" in result.reason
    assert calls == []


async def test_start_run_zone_switch_without_hash_is_refused(hass: HomeAssistant) -> None:
    """An unavailable Zone switch loses its `hash` attribute; upstream
    silently drops such entity_ids and falls back to the ON switches
    (wrong-zone hazard), so the adapter refuses before calling (research
    §2, mammotion-ha-surface §5)."""
    adapter = await make_command_adapter(hass)
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id("switch", "mammotion", f"{NAME}_{SIDE_YARD}")
    assert entity_id is not None
    hass.states.async_set(entity_id, "unavailable", {})
    calls = async_mock_service(hass, "mammotion", "start_mow")
    result = await adapter.start_run(SIDE_YARD, settings())
    assert isinstance(result, Refused)
    assert SIDE_YARD in result.reason
    assert calls == []


async def test_start_run_validation_error_is_refused(hass: HomeAssistant) -> None:
    """A 400 from start_mow is a definite no: refused with the message,
    exactly one call (ADR 0002)."""
    adapter = await make_command_adapter(hass)
    calls = async_mock_service(
        hass, "mammotion", "start_mow",
        raise_exception=ServiceValidationError("blade height out of range"),
    )
    result = await adapter.start_run(SIDE_YARD, settings())
    assert result == Refused("blade height out of range")
    assert len(calls) == 1


# -- abort_run and dock sequencing (spec §3.4) --------------------------------


async def test_abort_run_stops_then_docks_in_order(hass: HomeAssistant) -> None:
    """abort_run = `lawn_mower.stop` then `lawn_mower.dock`, in that
    order, accepted (issue #10 AC)."""
    adapter = await make_command_adapter(hass)
    order, stops, docks = mock_lawn_mower(hass)
    assert await adapter.abort_run() == Accepted()
    assert len(stops) == 1
    assert len(docks) == 1
    assert order == [("lawn_mower", "stop"), ("lawn_mower", "dock")]


async def test_abort_run_does_not_dock_when_stop_is_unconfirmed(
    hass: HomeAssistant,
) -> None:
    """A `pause_failed` mode-wait timeout on stop leaves the state
    unknown: return unconfirmed and do NOT dock - docking could fight a
    command whose outcome is unknown (research §3). Exactly one call."""
    adapter = await make_command_adapter(hass)
    _, stops, docks = mock_lawn_mower(
        hass,
        stop_raises=HomeAssistantError(
            "pause wait expired", translation_domain="mammotion",
            translation_key="pause_failed",
        ),
    )
    assert await adapter.abort_run() == Unconfirmed()
    assert len(stops) == 1
    assert docks == []


async def test_abort_run_does_not_dock_when_stop_is_refused(hass: HomeAssistant) -> None:
    """A 400 on stop is a definite no: return the refusal, dock NOT
    called. Exactly one call."""
    adapter = await make_command_adapter(hass)
    _, stops, docks = mock_lawn_mower(
        hass, stop_raises=ServiceValidationError("no job running")
    )
    assert await adapter.abort_run() == Refused("no job running")
    assert len(stops) == 1
    assert docks == []


async def test_abort_run_returns_the_dock_result_when_dock_fails(
    hass: HomeAssistant,
) -> None:
    """After a clean stop, the dock's own mapping is the result: a
    `dock_failed` timeout is unconfirmed, one stop + one dock call."""
    adapter = await make_command_adapter(hass)
    order, stops, docks = mock_lawn_mower(
        hass,
        dock_raises=HomeAssistantError(
            "dock wait expired", translation_domain="mammotion",
            translation_key="dock_failed",
        ),
    )
    assert await adapter.abort_run() == Unconfirmed()
    assert len(stops) == 1
    assert len(docks) == 1
    assert order == [("lawn_mower", "stop"), ("lawn_mower", "dock")]


# -- refresh (spec §3.4, decision A8) -----------------------------------------


def mower_device(hass: HomeAssistant):
    """The fake Mammotion Mower device, via the lawn_mower registry entry."""
    reg = er.async_get(hass)
    entry = reg.async_get(reg.async_get_entity_id("lawn_mower", "mammotion", f"{NAME}_mower"))
    assert entry is not None
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device is not None
    return device


def button_id(hass: HomeAssistant, key: str) -> str:
    """The entity_id of the registry `{NAME}_{key}` button."""
    entity_id = er.async_get(hass).async_get_entity_id("button", "mammotion", f"{NAME}_{key}")
    assert entity_id is not None
    return entity_id


async def test_refresh_presses_the_refresh_status_button(hass: HomeAssistant) -> None:
    """refresh requests fresh telemetry by pressing the Mammotion
    `{name}_refresh_status` button (research §2)."""
    adapter = await make_command_adapter(hass)
    set_entity(
        hass, None, mower_device(hass), NAME, "button:refresh_status", "unknown"
    )
    presses = async_mock_service(hass, "button", "press")
    assert await adapter.refresh() == Accepted()
    assert len(presses) == 1
    assert presses[0].data == {"entity_id": [button_id(hass, "refresh_status")]}


async def test_refresh_without_the_button_is_refused_not_ready(hass: HomeAssistant) -> None:
    """No refresh button in the registry (entry not loaded): refused with
    a not-ready reason, zero calls."""
    adapter = await make_command_adapter(hass)
    presses = async_mock_service(hass, "button", "press")
    assert await adapter.refresh() == Refused("mammotion_not_ready")
    assert presses == []


async def test_refresh_maps_a_press_failure(hass: HomeAssistant) -> None:
    """A refresh press that fails with command_unconfirmed is
    unconfirmed, exactly one press (ADR 0002)."""
    adapter = await make_command_adapter(hass)
    set_entity(
        hass, None, mower_device(hass), NAME, "button:refresh_status", "unknown"
    )
    presses = async_mock_service(
        hass, "button", "press",
        raise_exception=HomeAssistantError(
            "did not confirm", translation_domain="mammotion",
            translation_key="command_unconfirmed",
        ),
    )
    assert await adapter.refresh() == Unconfirmed()
    assert len(presses) == 1


# -- resume_run version strategy (spec §3.4/§3.8, research §2) ----------------


def install_continue_button(hass: HomeAssistant) -> str:
    """Registry-install the `{NAME}_continue_last_job` button."""
    return set_entity(
        hass, None, mower_device(hass), NAME, "button:continue_last_job", "unknown"
    )


async def test_resume_from_pause_uses_start_mowing_once(hass: HomeAssistant) -> None:
    """PAUSE + breakpoint: plain `lawn_mower.start_mowing` resumes the
    job on every version (research §2). Exactly one call."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, "MODE_PAUSE")
    starts = async_mock_service(hass, "lawn_mower", "start_mowing")
    assert await adapter.resume_run() == Accepted()
    assert len(starts) == 1


async def test_resume_from_ready_with_a_live_job_uses_start_mowing(
    hass: HomeAssistant,
) -> None:
    """READY while a per-Zone task sensor still exists means a
    breakpoint is waiting (job_active): resume is allowed."""
    adapter = await make_command_adapter(hass)
    set_entity(
        hass, None, mower_device(hass), NAME, f"sensor:{SIDE_YARD}_task_area", "RUNNING"
    )
    starts = async_mock_service(hass, "lawn_mower", "start_mowing")
    assert await adapter.resume_run() == Accepted()
    assert len(starts) == 1


async def test_resume_without_a_live_job_is_refused_without_a_call(
    hass: HomeAssistant,
) -> None:
    """Plain `start_mowing` when NOT mid-job plans a fresh job from the
    ON switches (research §2): pre-check job_active and refuse with zero
    calls."""
    adapter = await make_command_adapter(hass)
    starts = async_mock_service(hass, "lawn_mower", "start_mowing")
    result = await adapter.resume_run()
    assert isinstance(result, Refused)
    assert starts == []


async def test_resume_during_a_recharge_pause_is_refused(hass: HomeAssistant) -> None:
    """CHARGING_PAUSE: the mower is mid-charge; resume is refused, no
    call (research §2 resume)."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, "MODE_CHARGING_PAUSE")
    starts = async_mock_service(hass, "lawn_mower", "start_mowing")
    assert await adapter.resume_run() == Refused("recharging")
    assert starts == []


async def test_resume_from_a_random_mode_is_refused(hass: HomeAssistant) -> None:
    """WORKING (or any other mode): nothing to resume; refused, no
    call."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, "MODE_WORKING")
    starts = async_mock_service(hass, "lawn_mower", "start_mowing")
    result = await adapter.resume_run()
    assert isinstance(result, Refused)
    assert starts == []


async def test_resume_typeerror_falls_back_to_the_continue_button(
    hass: HomeAssistant,
) -> None:
    """v0.6.19+ bug: plain `start_mowing` raises TypeError after the
    route query was sent (research §2). The adapter must NOT re-call
    start_mowing; it presses `continue_last_job` instead - one start
    attempt plus one press."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, "MODE_PAUSE")
    button = install_continue_button(hass)
    starts = async_mock_service(
        hass, "lawn_mower", "start_mowing",
        raise_exception=TypeError(
            "async_send_command() got multiple values for argument 'priority'"
        ),
    )
    presses = async_mock_service(hass, "button", "press")
    assert await adapter.resume_run() == Accepted()
    assert len(starts) == 1
    assert len(presses) == 1
    assert presses[0].data == {"entity_id": [button]}


async def test_resume_typeerror_without_the_button_is_unconfirmed(
    hass: HomeAssistant,
) -> None:
    """After the TypeError the route query was already sent upstream, so
    with no button to fall back on the outcome is unknown: unconfirmed,
    never a retry (ADR 0002)."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, "MODE_PAUSE")
    starts = async_mock_service(
        hass, "lawn_mower", "start_mowing",
        raise_exception=TypeError("got multiple values for argument 'priority'"),
    )
    presses = async_mock_service(hass, "button", "press")
    assert await adapter.resume_run() == Unconfirmed()
    assert len(starts) == 1
    assert presses == []


async def test_resume_on_v0_6_19_presses_the_button_directly(
    hass: HomeAssistant,
) -> None:
    """Installed Mammotion-HA >= 0.6.19: the plain resume path is known
    broken, so press `continue_last_job` directly and never call
    start_mowing (issue #10 AC)."""
    adapter = await make_command_adapter(hass)
    mock_integration(
        hass, MockModule("mammotion", partial_manifest={"version": "0.6.19"}), built_in=False
    )
    await adapter.async_start()
    set_mode(hass, "MODE_PAUSE")
    button = install_continue_button(hass)
    starts = async_mock_service(hass, "lawn_mower", "start_mowing")
    presses = async_mock_service(hass, "button", "press")
    assert await adapter.resume_run() == Accepted()
    assert starts == []
    assert len(presses) == 1
    assert presses[0].data == {"entity_id": [button]}


async def test_resume_on_v0_6_19_without_the_button_is_refused_not_ready(
    hass: HomeAssistant,
) -> None:
    """The 0.6.19+ path needs the button; without it, refuse not-ready
    rather than take the known-broken path. Zero calls."""
    adapter = await make_command_adapter(hass)
    mock_integration(
        hass, MockModule("mammotion", partial_manifest={"version": "0.6.19"}), built_in=False
    )
    await adapter.async_start()
    set_mode(hass, "MODE_PAUSE")
    starts = async_mock_service(hass, "lawn_mower", "start_mowing")
    presses = async_mock_service(hass, "button", "press")
    assert await adapter.resume_run() == Refused("mammotion_not_ready")
    assert starts == []
    assert presses == []


async def test_resume_on_v0_6_18_still_uses_start_mowing(hass: HomeAssistant) -> None:
    """The installed version is the live ExampleSite one (0.6.18): the plain
    resume path works there, so use it - the button is not needed."""
    adapter = await make_command_adapter(hass)
    mock_integration(
        hass, MockModule("mammotion", partial_manifest={"version": "0.6.18"}), built_in=False
    )
    await adapter.async_start()
    set_mode(hass, "MODE_PAUSE")
    starts = async_mock_service(hass, "lawn_mower", "start_mowing")
    assert await adapter.resume_run() == Accepted()
    assert len(starts) == 1


async def test_resume_maps_a_start_mowing_failure(hass: HomeAssistant) -> None:
    """A resume_failed mode-wait timeout on start_mowing is
    unconfirmed, exactly one call (ADR 0002)."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, "MODE_PAUSE")
    starts = async_mock_service(
        hass, "lawn_mower", "start_mowing",
        raise_exception=HomeAssistantError(
            "resume wait expired", translation_domain="mammotion",
            translation_key="resume_failed",
        ),
    )
    assert await adapter.resume_run() == Unconfirmed()
    assert len(starts) == 1


# -- the v0.6.18 false-500, verify-by-state (ADR 0002, trace fixture) ---------


async def test_false_500_start_is_unconfirmed_then_state_reports_the_start(
    hass: HomeAssistant,
) -> None:
    """The v0.6.18 shape (trace examplesite_2026_10_18_stepdown_run.json):
    `start_mow` raises command_unconfirmed although the job usually
    started. The adapter returns unconfirmed after EXACTLY ONE call, and
    the core's verify-by-state half works: when state moves to mowing,
    the adapter emits JobStarted."""
    adapter = await make_command_adapter(hass)
    received: list = []
    unsubscribe = adapter.subscribe(_RecordingSink(received))
    try:
        calls = async_mock_service(
            hass, "mammotion", "start_mow",
            raise_exception=HomeAssistantError(
                "start did not confirm", translation_domain="mammotion",
                translation_key="command_unconfirmed",
            ),
        )
        assert await adapter.start_run(SIDE_YARD, settings()) == Unconfirmed()
        assert len(calls) == 1  # never re-issued (ADR 0002)

        # The job started anyway: drive the states a real start produces.
        set_mode(hass, "MODE_WORKING")
        reg = er.async_get(hass)
        mower_entity = reg.async_get_entity_id("lawn_mower", "mammotion", f"{NAME}_mower")
        assert mower_entity is not None
        hass.states.async_set(mower_entity, "mowing", {})
        await hass.async_block_till_done()

        started = [s for s in received if isinstance(s, JobStarted)]
        assert len(started) == 1
    finally:
        unsubscribe()


class _RecordingSink:
    """Records adapter signals."""

    def __init__(self, signals: list) -> None:
        self._signals = signals

    def on_snapshot(self, snapshot) -> None:
        """Ignore snapshot pushes; only signals matter here."""

    def on_signal(self, signal) -> None:
        """Record a signal."""
        self._signals.append(signal)


# -- services not loaded at all (issue #10 AC) --------------------------------


async def test_all_commands_refuse_not_ready_without_any_services(
    hass: HomeAssistant,
) -> None:
    """Mammotion services not yet loaded: every command comes back
    refused with a not-ready reason, never an exception (the mode
    pre-checks pass here; the ServiceNotFound arm does the work)."""
    adapter = await make_command_adapter(hass)
    set_mode(hass, "MODE_PAUSE")  # lets resume_run reach its call path
    for result in [
        await adapter.start_run(SIDE_YARD, settings()),
        await adapter.abort_run(),
        await adapter.dock(),
        await adapter.resume_run(),
        await adapter.refresh(),
    ]:
        assert result == Refused("mammotion_not_ready")
