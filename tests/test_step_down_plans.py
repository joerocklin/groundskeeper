"""HA integration tests for step-down Plans, layering, editing and removal
(spec §5, §13.2, §13.3; ticket #21)."""

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.mowing_director.const import DOMAIN, EVENT_TYPE
from tests.test_tracer_bullet import (  # noqa: F401 - `director` is a fixture
    MOWING,
    advance,
    director,
    state_of,
)


async def plan(hass, start_h=79, end_h=61, zone="front lawn", **extra):
    return await hass.services.async_call(
        DOMAIN,
        "add_plan",
        {"zone": zone, "start_height_mm": start_h, "end_height_mm": end_h, **extra},
        blocking=True,
        return_response=True,
    )


async def ws(hass, hass_ws_client, entry, type_, **data):
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"mowing_director/{type_}",
                            "entry_id": entry.entry_id, **data})
    return await client.receive_json()


def director_of(hass, entry):
    return hass.data[DOMAIN]["director"][entry.entry_id]


def front_lawn_id(hass, entry):
    zones = director_of(hass, entry).data["config"]["zones"]
    return next(z["id"] for z in zones if z["name"] == "Front Lawn")


# -- derivation through the service ---------------------------------------------------


async def test_add_plan_derives_a_step_down_plan_with_n_of_m(hass, director) -> None:
    entry, _, _ = director
    response = await plan(hass)
    assert [(r["height_mm"], r["n"], r["m"]) for r in response["runs"]] == [
        (79, 1, 3), (69, 2, 3), (61, 3, 3)
    ]
    assert response["start_height_mm"] == 79 and response["end_height_mm"] == 61
    runs = director_of(hass, entry).state["runs"]
    assert [r["settings"]["blade_height_mm"] for r in runs] == [79, 69, 61]


async def test_add_plan_echoes_rounded_values_and_max_cut_override(hass, director) -> None:
    response = await plan(hass, start_h=79.4, end_h=61, max_cut_pct=33)
    assert response["start_height_mm"] == 79 and response["rounded"] is True
    assert response["max_cut_pct"] == 33
    assert len(response["runs"]) == 2


@pytest.mark.parametrize(
    "data",
    [
        {"start_h": 60, "end_h": 90},  # start below end
        {"start_h": 500, "end_h": 60},  # outside the limits descriptor
        {"max_cut_pct": 2},  # outside 5-33
        {"settings": {"spacing_cm": 9999}},
        {"settings": {"bogus": 1}},
    ],
)
async def test_add_plan_rejects_invalid_input_with_a_message(hass, director, data) -> None:
    with pytest.raises(ServiceValidationError):
        await plan(hass, **{"start_h": 90, "end_h": 60, **data})


async def test_the_limits_error_names_the_limits(hass, director) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await plan(hass, start_h=500, end_h=60)
    assert "55-100" in str(err.value)


async def test_plan_stores_inputs_and_origin(hass, director) -> None:
    entry, _, _ = director
    response = await plan(hass, base_angle=10, finishing=False, settings={"perimeter_laps": 2})
    stored = director_of(hass, entry).state["plans"][response["plan_id"]]
    assert stored["origin"] == "service"
    assert (stored["start_height_mm"], stored["end_height_mm"]) == (79, 61)
    assert stored["base_angle"] == 10 and stored["finishing"] is False
    assert stored["settings"] == {"perimeter_laps": 2}
    assert all(r["settings"]["perimeter_laps"] == 2
               for r in director_of(hass, entry).state["runs"])


# -- placement ------------------------------------------------------------------------


async def test_service_appends_at_end_by_default_and_never_interleaves(hass, director) -> None:
    entry, _, _ = director
    a = await plan(hass)
    b = await plan(hass)
    runs = director_of(hass, entry).state["runs"]
    assert [r["plan_id"] for r in runs] == [a["plan_id"]] * 3 + [b["plan_id"]] * 3


async def test_service_position_front_goes_before_pending_runs(hass, director) -> None:
    entry, _, _ = director
    a = await plan(hass)
    b = await plan(hass, position="front")
    runs = director_of(hass, entry).state["runs"]
    assert [r["plan_id"] for r in runs] == [b["plan_id"]] * 3 + [a["plan_id"]] * 3


async def test_panel_add_merges_by_step_index(hass, hass_ws_client, director) -> None:
    entry, _, _ = director
    a = await plan(hass)
    msg = await ws(hass, hass_ws_client, entry, "add_plan", zone="front lawn",
                   start_height_mm=79, end_height_mm=61)
    assert msg["success"], msg
    b = msg["result"]["plan_id"]
    runs = director_of(hass, entry).state["runs"]
    assert [(r["plan_id"], r["index"]) for r in runs] == [
        (a["plan_id"], 1), (b, 1), (a["plan_id"], 2), (b, 2), (a["plan_id"], 3), (b, 3)
    ]
    assert director_of(hass, entry).state["plans"][b]["origin"] == "panel"


# -- layering through the service -----------------------------------------------------


async def test_layers_mower_zone_plan_through_the_panel_and_service(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    zone_id = front_lawn_id(hass, entry)
    msg = await ws(hass, hass_ws_client, entry, "set_mower_run_settings",
                   run_defaults={"spacing_cm": 26})
    assert msg["success"], msg
    first = await plan(hass, 70, 70)
    assert director_of(hass, entry).state["runs"][0]["settings"]["spacing_cm"] == 26
    msg = await ws(hass, hass_ws_client, entry, "set_zone_run_settings",
                   zone_id=zone_id, run_defaults={"spacing_cm": 27})
    assert msg["success"], msg
    second = await plan(hass, 70, 70)
    third = await plan(hass, 70, 70, settings={"spacing_cm": 28})
    by_plan = {r["plan_id"]: r["settings"]["spacing_cm"]
               for r in director_of(hass, entry).state["runs"]}
    assert by_plan == {first["plan_id"]: 26, second["plan_id"]: 27, third["plan_id"]: 28}


async def test_default_edits_do_not_touch_plans_on_the_schedule(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    await plan(hass, 70, 70)
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings",
             run_defaults={"spacing_cm": 30})
    assert director_of(hass, entry).state["runs"][0]["settings"]["spacing_cm"] != 30


async def test_rederive_picks_up_current_defaults_until_it_starts(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, _, _ = director
    response = await plan(hass)
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings",
             run_defaults={"spacing_cm": 30})
    msg = await ws(hass, hass_ws_client, entry, "rederive_plan", plan_id=response["plan_id"])
    assert msg["success"], msg
    runs = director_of(hass, entry).state["runs"]
    assert [r["settings"]["spacing_cm"] for r in runs] == [30, 30, 30]
    assert [r["height_mm"] for r in runs] == [79, 69, 61]
    # once started, it is refused
    await hass.services.async_call(DOMAIN, "start", {}, blocking=True)
    await advance(hass, freezer, 61)
    msg = await ws(hass, hass_ws_client, entry, "rederive_plan", plan_id=response["plan_id"])
    assert not msg["success"] and msg["error"]["code"] == "plan_started"


# -- Finishing Run --------------------------------------------------------------------


async def test_finishing_override_hits_only_the_last_run_of_a_multi_run_plan(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    msg = await ws(hass, hass_ws_client, entry, "set_mower_run_settings",
                   finishing={"speed_m_s": 0.3})
    assert msg["success"], msg
    await plan(hass)
    speeds = [r["settings"]["speed_m_s"] for r in director_of(hass, entry).state["runs"]]
    assert speeds[-1] == 0.3 and speeds[0] != 0.3
    await plan(hass, 70, 70)
    assert director_of(hass, entry).state["runs"][-1]["settings"]["speed_m_s"] != 0.3
    await plan(hass, finishing=False)
    assert director_of(hass, entry).state["runs"][-1]["settings"]["speed_m_s"] != 0.3


async def test_zone_finishing_falls_back_to_mower(hass, hass_ws_client, director) -> None:
    entry, _, _ = director
    zone_id = front_lawn_id(hass, entry)
    await ws(hass, hass_ws_client, entry, "set_mower_run_settings", finishing={"speed_m_s": 0.5})
    msg = await ws(hass, hass_ws_client, entry, "set_zone_run_settings", zone_id=zone_id,
                   finishing={"speed_m_s": 0.35})
    assert msg["success"], msg
    await plan(hass)
    assert director_of(hass, entry).state["runs"][-1]["settings"]["speed_m_s"] == 0.35


# -- editing, deleting, reordering ----------------------------------------------------


async def test_edit_run_warns_on_height_increase_and_big_step_but_applies(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    response = await plan(hass)
    middle = response["runs"][1]["run_id"]
    msg = await ws(hass, hass_ws_client, entry, "edit_run", run_id=middle, height_mm=85)
    assert msg["success"], msg
    assert "height_increases" in msg["result"]["warnings"]
    assert director_of(hass, entry).state["runs"][1]["height_mm"] == 85
    msg = await ws(hass, hass_ws_client, entry, "edit_run", run_id=middle, height_mm=56)
    assert "step_above_max_cut" in msg["result"]["warnings"]


async def test_edit_run_rejects_values_outside_device_limits(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    response = await plan(hass)
    rid = response["runs"][0]["run_id"]
    for data in ({"height_mm": 500}, {"settings": {"speed_m_s": 99}}):
        msg = await ws(hass, hass_ws_client, entry, "edit_run", run_id=rid, **data)
        assert not msg["success"]


async def test_edit_delete_and_reorder_work_while_the_schedule_is_running(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    a = await plan(hass, 70, 70, start=True)
    b = await plan(hass, 70, 70)
    c = await plan(hass, 70, 70)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.example_mower_schedule").state == "running"
    active = a["runs"][0]["run_id"]
    # the active Run is frozen
    for type_, extra in (("edit_run", {"height_mm": 60}), ("delete_run", {}),
                         ("move_run", {"position": 0})):
        msg = await ws(hass, hass_ws_client, entry, type_, run_id=active, **extra)
        assert not msg["success"]
        assert msg["error"]["code"] in ("run_not_pending", "move_not_pending")
    msg = await ws(hass, hass_ws_client, entry, "move_run",
                   run_id=c["runs"][0]["run_id"], position=0)
    assert msg["success"], msg
    ids = [r["id"] for r in director_of(hass, entry).state["runs"]]
    assert ids == [active, c["runs"][0]["run_id"], b["runs"][0]["run_id"]]
    msg = await ws(hass, hass_ws_client, entry, "delete_run", run_id=b["runs"][0]["run_id"])
    assert msg["success"], msg
    assert len(director_of(hass, entry).state["runs"]) == 2


async def test_manual_reorder_never_moves_a_run_ahead_of_an_earlier_run_of_its_plan(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    a = await plan(hass)
    await plan(hass, 70, 70)
    before = [r["id"] for r in director_of(hass, entry).state["runs"]]
    msg = await ws(hass, hass_ws_client, entry, "move_run",
                   run_id=a["runs"][2]["run_id"], position=0)
    assert not msg["success"] and msg["error"]["code"] == "move_before_earlier_run"
    assert [r["id"] for r in director_of(hass, entry).state["runs"]] == before


# -- remove_plan ------------------------------------------------------------------------


async def test_remove_plan_cancels_pending_runs_and_fires_plan_removed(hass, director) -> None:
    entry, mower, _ = director
    events = async_capture_events(hass, EVENT_TYPE)
    a = await plan(hass)
    b = await plan(hass, 70, 70)
    await hass.services.async_call(
        DOMAIN, "remove_plan", {"plan_id": a["plan_id"]}, blocking=True
    )
    d = director_of(hass, entry)
    assert [r["plan_id"] for r in d.state["runs"]] == [b["plan_id"]]
    assert [r["status"] for r in d.data["history"]["runs"]] == ["cancelled"] * 3
    removed = [e.data for e in events if e.data["type"] == "plan_removed"]
    assert [e["plan_id"] for e in removed] == [a["plan_id"]]
    assert mower.commands == []


async def test_remove_plan_is_refused_while_one_of_its_runs_is_active(
    hass, director, freezer
) -> None:
    entry, mower, _ = director
    a = await plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    commands = list(mower.commands)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "remove_plan", {"plan_id": a["plan_id"]}, blocking=True
        )
    assert mower.commands == commands
    assert len(director_of(hass, entry).state["runs"]) == 3


async def test_remove_unknown_plan_raises(hass, director) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "remove_plan", {"plan_id": "nope"}, blocking=True)


# -- default settings over the websocket ------------------------------------------------


async def test_run_settings_can_be_read_and_edited(hass, hass_ws_client, director) -> None:
    entry, _, _ = director
    msg = await ws(hass, hass_ws_client, entry, "get_run_settings")
    assert msg["result"]["mower"]["max_cut_pct"] == 13
    assert msg["result"]["mower"]["finishing"] is None
    msg = await ws(hass, hass_ws_client, entry, "set_mower_run_settings", max_cut_pct=20)
    assert msg["result"]["mower"]["max_cut_pct"] == 20
    assert len((await plan(hass, 100, 58))["runs"]) < 5
    bad = await ws(hass, hass_ws_client, entry, "set_mower_run_settings", max_cut_pct=50)
    assert not bad["success"]
    bad = await ws(hass, hass_ws_client, entry, "set_mower_run_settings",
                   run_defaults={"spacing_cm": 9999})
    assert not bad["success"] and bad["error"]["code"] == "invalid_settings"
    cleared = await ws(hass, hass_ws_client, entry, "set_mower_run_settings", finishing=None)
    assert cleared["success"]


async def test_zone_settings_for_an_unknown_zone_are_refused(hass, hass_ws_client, director) -> None:
    entry, _, _ = director
    msg = await ws(hass, hass_ws_client, entry, "set_zone_run_settings",
                   zone_id="nope", run_defaults={"spacing_cm": 26})
    assert not msg["success"]
