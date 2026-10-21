"""Mammotion adapter: snapshots, signals, limits (issue #8, spec §3.2/§3.6).

All state is fake: entity registry entries plus `hass.states`, shaped
from the live ExampleSite inventory and the research notes. No real Mammotion
service is ever called.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director.adapter import (
    JobStarted,
    NewError,
    RainStop,
    ZoneTaskAborted,
    ZoneTaskComplete,
    ZonesChanged,
)
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


async def test_restored_unavailable_is_not_ready(hass: HomeAssistant) -> None:
    """`unavailable` + `restored: true` means Mammotion has not added the
    entity yet (spec §2.3): available False, activity None, no crash."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id("lawn_mower", "mammotion", f"{NAME}_mower")
    hass.states.async_set(entity_id, "unavailable", {"restored": True})
    adapter = make_adapter(hass, entry)

    snapshot = adapter.snapshot
    assert snapshot.available is False
    assert snapshot.activity is None


# -- signals ----------------------------------------------------------------


class _Recorder:
    """Sink that records pushes."""

    def __init__(self) -> None:
        self.signals: list = []
        self.snapshots: list[Snapshot] = []

    def on_snapshot(self, snapshot: Snapshot) -> None:
        self.snapshots.append(snapshot)

    def on_signal(self, signal) -> None:
        self.signals.append(signal)


async def _adapter_with_sink(hass: HomeAssistant) -> tuple[object, _Recorder, object, object]:
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    adapter = make_adapter(hass, entry)
    recorder = _Recorder()
    adapter.subscribe(recorder)
    return adapter, recorder, entry, mower


async def test_new_error_from_error_sensors(hass: HomeAssistant) -> None:
    """A new (code, time) pair with code != 0 emits NewError; the same
    pair again does not (works over BLE, no mammotion_event)."""
    _, recorder, entry, mower = await _adapter_with_sink(hass)

    set_entity(
        hass, entry, mower, NAME, "sensor:error_1_code", "2748", {}
    )
    set_entity(hass, entry, mower, NAME, "sensor:error_1_message", "Angle position sensor of lift motor disconnected", {})
    set_entity(hass, entry, mower, NAME, "sensor:error_1_time", "2026-10-19T18:59:58+00:00", {})
    await hass.async_block_till_done()

    assert recorder.signals == [
        NewError(
            code="2748",
            message="Angle position sensor of lift motor disconnected",
            time=datetime(2026, 10, 19, 18, 59, 58, tzinfo=UTC),
        )
    ]

    # Same pair again: no re-emit (rolling log re-reads are common).
    set_entity(hass, entry, mower, NAME, "sensor:error_1_code", "2748", {})
    set_entity(hass, entry, mower, NAME, "sensor:error_1_message", "changed wording", {})
    await hass.async_block_till_done()
    assert len(recorder.signals) == 1

    # A recurrence gets a new time: it is a new error.
    set_entity(hass, entry, mower, NAME, "sensor:error_1_time", "2026-10-20T06:30:00+00:00", {})
    await hass.async_block_till_done()
    assert len(recorder.signals) == 2
    assert recorder.signals[1].time == datetime(2026, 10, 20, 6, 30, tzinfo=UTC)


async def test_error_without_time_waits_for_the_stamp(hass: HomeAssistant) -> None:
    """`error_1_time` may stay `unknown` until the firmware stamps it;
    the error is held, then reported once when the time arrives (never
    with a made-up time, which would double-count the same fault)."""
    _, recorder, entry, mower = await _adapter_with_sink(hass)

    set_entity(hass, entry, mower, NAME, "sensor:error_1_code", "5004", {})
    set_entity(hass, entry, mower, NAME, "sensor:error_1_message", "some fault", {})
    await hass.async_block_till_done()
    assert recorder.signals == []

    set_entity(hass, entry, mower, NAME, "sensor:error_1_time", "2026-10-20T06:00:00+00:00", {})
    await hass.async_block_till_done()
    errors = [s for s in recorder.signals if isinstance(s, NewError)]
    assert errors == [
        NewError(code="5004", message="some fault", time=datetime(2026, 10, 20, 6, tzinfo=UTC))
    ]

    set_entity(hass, entry, mower, NAME, "sensor:error_1_message", "same fault", {})
    await hass.async_block_till_done()
    assert len([s for s in recorder.signals if isinstance(s, NewError)]) == 1


async def test_rain_stop_from_mammotion_event(hass: HomeAssistant) -> None:
    """mammotion_event code 1003 for this device emits RainStop once."""
    adapter, recorder, _, _ = await _adapter_with_sink(hass)

    hass.bus.async_fire(
        "mammotion_event",
        {
            "type": "device_warning_code_event",
            "device_id": adapter._device_id,
            "device_name": NAME,
            "codes": [{"code": 1003, "time": "2026-10-19T18:10:00+00:00", "message": "rain sensor"}],
        },
    )
    await hass.async_block_till_done()
    assert RainStop() in recorder.signals

    # A different device's event is ignored.
    recorder.signals.clear()
    hass.bus.async_fire(
        "mammotion_event",
        {"type": "device_warning_code_event", "device_id": "other", "codes": [{"code": 1003}]},
    )
    await hass.async_block_till_done()
    assert recorder.signals == []


async def test_zone_task_complete_and_aborted(hass: HomeAssistant) -> None:
    """Ephemeral `{hash}_task_area` sensors reaching COMPLETE/ABORTED emit
    zone signals; the sensors vanishing at job end emits nothing."""
    _, recorder, entry, mower = await _adapter_with_sink(hass)
    side = "1000000000000000007"
    back = "1000000000000000001"

    set_entity(hass, entry, mower, NAME, f"sensor:{side}_task_area", "MOWING", {})
    await hass.async_block_till_done()
    assert recorder.signals == []

    set_entity(hass, entry, mower, NAME, f"sensor:{side}_task_area", "COMPLETE", {})
    await hass.async_block_till_done()
    assert recorder.signals == [ZoneTaskComplete(side)]

    set_entity(hass, entry, mower, NAME, f"sensor:{back}_task_area", "ABORTED", {})
    await hass.async_block_till_done()
    assert recorder.signals == [ZoneTaskComplete(side), ZoneTaskAborted(back)]

    # Job ends: the sensors are removed from the registry (research §4.2).
    reg = er.async_get(hass)
    reg.async_remove(
        reg.async_get_entity_id("sensor", "mammotion", f"{NAME}_{side}_task_area")
    )
    await hass.async_block_till_done()
    assert recorder.signals == [ZoneTaskComplete(side), ZoneTaskAborted(back)]


async def test_job_started_on_mowing_transition(hass: HomeAssistant) -> None:
    """A transition into `mowing` emits JobStarted (job_ref None; the core
    decides externality from its own command log)."""
    _, recorder, entry, mower = await _adapter_with_sink(hass)

    set_entity(hass, entry, mower, NAME, "lawn_mower:mower", "mowing", {})
    set_entity(hass, entry, mower, NAME, "sensor:activity_mode", "MODE_WORKING", {})
    await hass.async_block_till_done()

    started = [s for s in recorder.signals if isinstance(s, JobStarted)]
    assert len(started) == 1
    assert started[0].job_ref is None
    assert started[0].started_at.tzinfo is not None

    # Staying mowing does not re-emit.
    set_entity(hass, entry, mower, NAME, "sensor:progress", "5", {})
    await hass.async_block_till_done()
    assert len([s for s in recorder.signals if isinstance(s, JobStarted)]) == 1


async def test_zones_changed_on_registry_and_rekey(hass: HomeAssistant) -> None:
    """A Zone switch appearing, and a map rebuild re-keying the hash set,
    both emit ZonesChanged."""
    _, recorder, entry, mower = await _adapter_with_sink(hass)

    set_entity(hass, entry, mower, NAME, "switch:9999999999999999", "on", {"hash": 9999999999999999})
    await hass.async_block_till_done()
    assert recorder.signals == [ZonesChanged()]

    # Re-key without a registry change: the hash attribute moved.
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id("switch", "mammotion", f"{NAME}_9999999999999999")
    hass.states.async_set(entity_id, "on", {"hash": 8888888888888888})
    await hass.async_block_till_done()
    assert recorder.signals.count(ZonesChanged()) == 2


# -- limits (spec §3.6) ------------------------------------------------------


async def test_limits_come_from_the_entities(hass: HomeAssistant) -> None:
    """Height/spacing/speed ranges are read from the number entities'
    attributes, not hard-coded (ExampleSite H variant: 55-100 mm step 1)."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    adapter = make_adapter(hass, entry)

    limits = adapter.limits
    assert (limits.height_min_mm, limits.height_max_mm, limits.height_step_mm) == (55, 100, 1)
    assert limits.has_height_readback is True
    assert limits.height_readback_resolution_mm == 5
    assert (limits.spacing_min_cm, limits.spacing_max_cm) == (20.0, 35.0)
    assert (limits.speed_min_m_s, limits.speed_max_m_s) == (0.2, 0.6)
    assert limits.min_start_battery_pct == 15
    assert (limits.recharge_battery_pct, limits.resume_battery_pct) == (20, 50)


async def test_limits_follow_a_different_device(hass: HomeAssistant) -> None:
    """A standard (non-H) variant's number entity gives 25-70 mm; the
    adapter must report that instead."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    set_entity(
        hass,
        entry,
        mower,
        NAME,
        "number:blade_height",
        "30",
        {"min_value": 25, "max_value": 70, "step": 1, "unit_of_measurement": "mm"},
    )
    adapter = make_adapter(hass, entry)
    assert (adapter.limits.height_min_mm, adapter.limits.height_max_mm) == (25, 70)


async def test_smart_recharge_and_resume_are_none(hass: HomeAssistant) -> None:
    """The smart switches are authoritative: on means the number entity
    shows a stale 30/100 and the level is None (spec §3.6)."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    set_entity(hass, entry, mower, NAME, "switch:smart_recharge_level", "on", {})
    set_entity(hass, entry, mower, NAME, "switch:smart_resume_level", "on", {})
    set_entity(hass, entry, mower, NAME, "number:recharge_level", "30", {})
    set_entity(hass, entry, mower, NAME, "number:resume_level", "100", {})
    adapter = make_adapter(hass, entry)
    assert adapter.limits.recharge_battery_pct is None
    assert adapter.limits.resume_battery_pct is None


async def test_extras_schema_declares_the_mammotion_settings(hass: HomeAssistant) -> None:
    """The seven spec §3.5 extras are declared with HA selectors and
    defaults that are valid `start_mow` inputs."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    adapter = make_adapter(hass, entry)

    schema = {field.key: field for field in adapter.limits.extras_schema}
    assert set(schema) == {
        "border_mode",
        "obstacle_laps",
        "channel_mode",
        "toward_included_angle",
        "ultra_wave",
        "auto_change_direction",
        "ride_boundary_distance",
    }
    for field in adapter.limits.extras_schema:
        assert field.label
        # Panel renders and serialises the selector generically.
        serialised = field.selector.serialize()
        assert json.loads(json.dumps(serialised)) == serialised

    # Defaults are native ints/floats, valid for start_mow (contract test
    # feeds them straight into RunSettings.extras).
    assert schema["border_mode"].default == 0
    assert schema["obstacle_laps"].default == 0
    assert schema["channel_mode"].default == 0
    assert schema["toward_included_angle"].default == 90
    # Select options are strings; the adapter converts to the native int.
    assert schema["ultra_wave"].default == "1"
    assert schema["auto_change_direction"].default == 0
    assert schema["ride_boundary_distance"].default == 0.0

