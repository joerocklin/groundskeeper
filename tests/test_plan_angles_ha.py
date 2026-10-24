"""HA integration tests for angles on Plans (spec §5.5; ticket #22)."""

from tests.test_step_down_plans import director_of, front_lawn_id, plan, ws
from tests.test_tracer_bullet import advance, director  # noqa: F401 - fixture


def _zone(hass, entry):
    return next(z for z in director_of(hass, entry).data["config"]["zones"]
                if z["name"] == "Front Lawn")


def _angles(hass, entry):
    return [r["angle_deg"] for r in director_of(hass, entry).state["runs"]]


async def test_add_plan_response_includes_the_drawn_angles(hass, director) -> None:
    entry, _, _ = director
    response = await plan(hass, 100, 58)
    stored = director_of(hass, entry).state["runs"]
    assert [r["angle_deg"] for r in response["runs"]] == [r["angle_deg"] for r in stored]
    assert all(r["angle_draw"]["angles"] == _angles(hass, entry) for r in stored)


async def test_reroll_redraws_a_not_started_plan_and_is_refused_once_started(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, _, _ = director
    response = await plan(hass, 100, 58)
    before = _angles(hass, entry)
    for _ in range(5):  # a fresh draw may by chance repeat; five will not all
        msg = await ws(hass, hass_ws_client, entry, "reroll_angles", plan_id=response["plan_id"])
        assert msg["success"], msg
        after = _angles(hass, entry)
        if after != before:
            break
    assert after != before
    assert [r["angle_deg"] for r in msg["result"]["runs"]] == after
    runs = director_of(hass, entry).state["runs"]
    assert all(r["angle_draw"]["angles"] == after for r in runs)

    await hass.services.async_call("groundskeeper", "start", {}, blocking=True)
    await advance(hass, freezer, 61)
    msg = await ws(hass, hass_ws_client, entry, "reroll_angles", plan_id=response["plan_id"])
    assert not msg["success"] and msg["error"]["code"] == "plan_started"


async def test_provisional_angles_are_redrawn_when_the_range_arrives(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    zone = _zone(hass, entry)
    zone["angle_range"] = {**(zone.get("angle_range") or {}), "angle_range": "unset"}
    response = await plan(hass, 100, 58)
    assert all(r["provisional"] for r in response["runs"])
    assert any("provisional" in w.lower() for w in response["warnings"])
    edited_id = response["runs"][1]["run_id"]
    msg = await ws(hass, hass_ws_client, entry, "edit_run", run_id=edited_id, angle_deg=80)
    assert msg["success"], msg

    msg = await ws(hass, hass_ws_client, entry, "set_zone_range", zone_id=front_lawn_id(hass, entry),
                   min=-10, max=10)
    assert msg["success"], msg
    await hass.async_block_till_done(wait_background_tasks=True)

    state = director_of(hass, entry).state
    runs = state["runs"]
    assert runs[1]["angle_deg"] == 80 and runs[1]["angle_edited"]
    for i, r in enumerate(runs):
        if i != 1:
            assert -10 <= r["angle_deg"] <= 10
            assert r["angle_draw"]["provisional"] is False
    saved = state["plans"][response["plan_id"]]
    assert saved["note"] == "angles_redrawn_for_range"
    assert any("Run 2" in w and "outside" in w for w in saved["angle_warnings"])
