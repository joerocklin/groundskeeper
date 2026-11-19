"""Spacing and speed on a Plan (#108): preview, add and the views, through the
panel's websocket commands. The Mower default is 25 cm and 0.4 m/s; the fake
Mower's device limits are 20-60 cm and 0.3-1.2 m/s."""

import copy

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.groundskeeper.const import DOMAIN
from custom_components.groundskeeper.zone_angles import run_estimate_s
from tests.test_step_down_plans import director_of, front_lawn_id, ws
from tests.test_tracer_bullet import director  # noqa: F401 - fixture

STEP_DOWN = {"zone": "front lawn", "start_height_mm": 100, "end_height_mm": 58}
ONE_RUN = {"zone": "front lawn", "start_height_mm": 70, "end_height_mm": 70, "base_angle": 15}


def zone_record(hass, entry):
    zid = front_lawn_id(hass, entry)
    return next(z for z in director_of(hass, entry).data["config"]["zones"] if z["id"] == zid)


async def preview(hass, hass_ws_client, entry, **data):
    msg = await ws(hass, hass_ws_client, entry, "preview_plan", **data)
    assert msg["success"], msg
    return msg["result"]


# -- preview: each Run's resolved Spacing and speed ----------------------------


async def test_preview_shows_each_runs_resolved_spacing_and_speed(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    result = await preview(hass, hass_ws_client, entry, **STEP_DOWN)
    assert len(result["runs"]) >= 2
    assert {(r["spacing_cm"], r["speed_m_s"]) for r in result["runs"]} == {(25, 0.4)}


async def test_a_plan_override_shows_on_every_run_including_the_finishing_run(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings", finishing={"speed_m_s": 0.3})
    result = await preview(hass, hass_ws_client, entry, **STEP_DOWN,
                           settings={"spacing_cm": 30, "speed_m_s": 0.5})
    assert {(r["spacing_cm"], r["speed_m_s"]) for r in result["runs"]} == {(30, 0.5)}


async def test_a_finishing_set_with_a_lower_speed_shows_on_the_last_row_only(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings", finishing={"speed_m_s": 0.3})
    runs = (await preview(hass, hass_ws_client, entry, **STEP_DOWN))["runs"]
    assert len(runs) >= 2
    assert [r["speed_m_s"] for r in runs] == [0.4] * (len(runs) - 1) + [0.3]


# -- preview: each Run's estimate comes from its own Spacing and speed -----------


async def test_the_estimate_is_unchanged_without_an_override(hass, hass_ws_client, director) -> None:
    entry, _, _ = director
    (run,) = (await preview(hass, hass_ws_client, entry, **ONE_RUN))["runs"]
    assert run["estimated_time_s"] == run_estimate_s(zone_record(hass, entry), run["angle_deg"])


@pytest.mark.parametrize(
    "override", [{"speed_m_s": 0.3}, {"spacing_cm": 20}, {"spacing_cm": 20, "speed_m_s": 0.3}]
)
async def test_a_slower_speed_or_closer_spacing_takes_longer_on_the_same_zone_and_angle(
    hass, hass_ws_client, director, override
) -> None:
    entry, _, _ = director
    (run,) = (await preview(hass, hass_ws_client, entry, **ONE_RUN, settings=override))["runs"]
    # The default estimate at this very angle (drawn angles vary between previews).
    default = run_estimate_s(zone_record(hass, entry), run["angle_deg"])
    assert run["estimated_time_s"] > default


async def test_a_mower_default_shows_in_the_estimate_like_any_other_resolved_value(
    hass, hass_ws_client, director
) -> None:
    """The estimate follows the Run's resolved Spacing and speed, wherever they
    came from, so a slower Mower default is in the time with no Plan override."""
    entry, _, _ = director
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings",
             run_defaults={"speed_m_s": 0.3})
    (run,) = (await preview(hass, hass_ws_client, entry, **ONE_RUN))["runs"]
    assert run["speed_m_s"] == 0.3
    assert run["estimated_time_s"] > run_estimate_s(zone_record(hass, entry), run["angle_deg"])


async def test_the_finishing_run_is_estimated_from_its_own_slower_speed(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings", finishing={"speed_m_s": 0.3})
    last = (await preview(hass, hass_ws_client, entry, **STEP_DOWN))["runs"][-1]
    assert last["estimated_time_s"] > run_estimate_s(zone_record(hass, entry), last["angle_deg"])


# -- the Zone's stored range record stays as it was ------------------------------


async def test_previewing_with_overrides_leaves_the_zones_range_record_unchanged(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    before = copy.deepcopy(zone_record(hass, entry)["angle_range"])
    await preview(hass, hass_ws_client, entry, **STEP_DOWN,
                  settings={"spacing_cm": 20, "speed_m_s": 0.3})
    assert zone_record(hass, entry)["angle_range"] == before


# -- a value outside the device limits is rejected, never clamped ----------------


@pytest.mark.parametrize("type_", ["preview_plan", "add_plan"])
@pytest.mark.parametrize(
    "bad", [{"spacing_cm": 5}, {"spacing_cm": 99}, {"speed_m_s": 0.1}, {"speed_m_s": 9}]
)
async def test_a_value_outside_the_device_limits_fails_and_creates_no_plan(
    hass, hass_ws_client, director, type_, bad
) -> None:
    entry, _, _ = director
    msg = await ws(hass, hass_ws_client, entry, type_, **STEP_DOWN, settings=bad)
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_settings"
    assert "outside the device limits" in msg["error"]["message"]
    assert director_of(hass, entry).state["plans"] == {}
    assert director_of(hass, entry).state["runs"] == []


# -- add: every Run stores the values --------------------------------------------


async def add(hass, hass_ws_client, entry, **data):
    msg = await ws(hass, hass_ws_client, entry, "add_plan", **STEP_DOWN, **data)
    assert msg["success"], msg
    return (await ws(hass, hass_ws_client, entry, "get_schedule"))["result"]


async def test_added_plan_stores_the_values_on_every_run_including_the_finishing_run(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings", finishing={"speed_m_s": 0.3})
    schedule = await add(hass, hass_ws_client, entry, finishing=True,
                         settings={"spacing_cm": 30, "speed_m_s": 0.5})
    assert len(schedule["runs"]) >= 2
    assert {(r["settings"]["spacing_cm"], r["settings"]["speed_m_s"])
            for r in schedule["runs"]} == {(30, 0.5)}
    assert schedule["plans"][0]["settings"] == {"spacing_cm": 30, "speed_m_s": 0.5}


async def test_blank_values_leave_the_plan_as_it_is_today(hass, hass_ws_client, director) -> None:
    entry, _, _ = director
    schedule = await add(hass, hass_ws_client, entry)
    assert schedule["plans"][0]["settings"] == {}
    assert {(r["settings"]["spacing_cm"], r["settings"]["speed_m_s"])
            for r in schedule["runs"]} == {(25, 0.4)}


async def test_adding_with_overrides_leaves_the_zones_range_record_unchanged(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    before = copy.deepcopy(zone_record(hass, entry)["angle_range"])
    await add(hass, hass_ws_client, entry, settings={"spacing_cm": 20, "speed_m_s": 0.3})
    assert zone_record(hass, entry)["angle_range"] == before


async def test_the_add_plan_service_rejects_a_value_outside_the_limits_too(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "add_plan",
            {"zone": "front lawn", "start_height_mm": 100, "end_height_mm": 58,
             "settings": {"speed_m_s": 9}},
            blocking=True, return_response=True)
    assert director_of(hass, entry).state["plans"] == {}


# -- the Zones view: what the New Plan form shows beside the inputs -----------------


async def zones_view(hass, hass_ws_client, entry):
    return (await ws(hass, hass_ws_client, entry, "get_zones"))["result"]


async def test_the_zones_view_carries_the_device_limits_and_the_mower_defaults(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    view = await zones_view(hass, hass_ws_client, entry)
    assert view["run_limits"] == {
        "spacing_cm": {"min": 20, "max": 60, "default": 25},
        "speed_m_s": {"min": 0.3, "max": 1.2, "default": 0.4},
    }
    front = next(z for z in view["zones"] if z["id"] == front_lawn_id(hass, entry))
    assert (front["spacing_cm_default"], front["speed_m_s_default"]) == (25, 0.4)


async def test_a_mower_default_shows_as_the_new_default_everywhere(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings",
             run_defaults={"spacing_cm": 30, "speed_m_s": 0.5})
    view = await zones_view(hass, hass_ws_client, entry)
    assert view["run_limits"]["spacing_cm"]["default"] == 30
    assert view["run_limits"]["speed_m_s"]["default"] == 0.5
    assert {(z["spacing_cm_default"], z["speed_m_s_default"]) for z in view["zones"]} == {(30, 0.5)}


async def test_a_zone_default_is_that_zones_resolved_default_and_not_the_finishing_set(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    zid = front_lawn_id(hass, entry)
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings", finishing={"speed_m_s": 0.3})
    msg = await ws(hass, hass_ws_client, entry, "set_zone_run_settings", zone_id=zid,
                   run_defaults={"spacing_cm": 35})
    assert msg["success"], msg
    view = await zones_view(hass, hass_ws_client, entry)
    front = next(z for z in view["zones"] if z["id"] == zid)
    assert (front["spacing_cm_default"], front["speed_m_s_default"]) == (35, 0.4)
    assert view["run_limits"]["spacing_cm"]["default"] == 25  # the Mower's, for no Zone picked
