"""Editing one pending Run's Spacing and speed (#111), through the panel's
websocket commands. The Mower default is 25 cm and 0.4 m/s; the fake Mower's
device limits are 20-60 cm and 0.3-1.2 m/s."""

import pytest

from tests.test_step_down_plans import director_of, plan, ws
from tests.test_tracer_bullet import director  # noqa: F401 - fixture


async def now_rows(hass, hass_ws_client, entry):
    msg = await ws(hass, hass_ws_client, entry, "get_now")
    assert msg["success"], msg
    return msg["result"]["up_next"]


# -- the Now tab's Run sheet pre-fills from the Run's stored values -----------------


async def test_up_next_rows_carry_each_runs_stored_spacing_and_speed(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    await plan(hass, settings={"spacing_cm": 30, "speed_m_s": 0.5})
    await plan(hass, 70, 70)  # a second Plan on the Mower defaults
    rows = await now_rows(hass, hass_ws_client, entry)
    assert [(r["spacing_cm"], r["speed_m_s"]) for r in rows] == [(30, 0.5)] * 3 + [(25, 0.4)]


# -- editing one Run ------------------------------------------------------------------

STEP_DOWN = {"start_h": 79, "end_h": 61}  # three Runs: 79, 69, 61 mm


async def schedule(hass, hass_ws_client, entry):
    return (await ws(hass, hass_ws_client, entry, "get_schedule"))["result"]


def stored(schedule_view):
    return [(r["settings"]["spacing_cm"], r["settings"]["speed_m_s"]) for r in schedule_view["runs"]]


async def edit(hass, hass_ws_client, entry, run_id, **settings):
    return await ws(hass, hass_ws_client, entry, "edit_run", run_id=run_id, settings=settings)


async def test_a_step_down_plan_can_end_up_with_a_different_spacing_on_every_run(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    response = await plan(hass, **STEP_DOWN)
    ids = [r["run_id"] for r in response["runs"]]
    assert len(ids) == 3
    for run_id, spacing in zip(ids, (35, 28, 22)):  # wider early, tighter last
        msg = await edit(hass, hass_ws_client, entry, run_id, spacing_cm=spacing)
        assert msg["success"], msg
    view = await schedule(hass, hass_ws_client, entry)
    assert [r["settings"]["spacing_cm"] for r in view["runs"]] == [35, 28, 22]


async def test_editing_one_run_leaves_its_siblings_and_the_plans_own_settings_alone(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    response = await plan(hass, **STEP_DOWN, settings={"spacing_cm": 30, "speed_m_s": 0.5})
    middle = response["runs"][1]["run_id"]
    msg = await edit(hass, hass_ws_client, entry, middle, spacing_cm=22, speed_m_s=0.3)
    assert msg["success"], msg
    view = await schedule(hass, hass_ws_client, entry)
    assert stored(view) == [(30, 0.5), (22, 0.3), (30, 0.5)]
    assert view["plans"][0]["settings"] == {"spacing_cm": 30, "speed_m_s": 0.5}


async def test_only_the_field_sent_changes_on_the_run(hass, hass_ws_client, director) -> None:
    entry, _, _ = director
    response = await plan(hass, 70, 70)
    run_id = response["runs"][0]["run_id"]
    await edit(hass, hass_ws_client, entry, run_id, speed_m_s=0.6)
    assert stored(await schedule(hass, hass_ws_client, entry)) == [(25, 0.6)]


# -- a value outside the device limits is refused and the Run is left unchanged ----------


@pytest.mark.parametrize(
    "bad", [{"spacing_cm": 5}, {"spacing_cm": 99}, {"speed_m_s": 0.1}, {"speed_m_s": 9}]
)
async def test_a_value_outside_the_device_limits_is_refused_and_the_run_is_unchanged(
    hass, hass_ws_client, director, bad
) -> None:
    entry, _, _ = director
    response = await plan(hass, 70, 70)
    run_id = response["runs"][0]["run_id"]
    msg = await edit(hass, hass_ws_client, entry, run_id, **bad)
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_settings"
    assert "outside the device limits" in msg["error"]["message"]
    assert stored(await schedule(hass, hass_ws_client, entry)) == [(25, 0.4)]


async def test_a_refused_value_changes_none_of_the_edit(hass, hass_ws_client, director) -> None:
    """A good spacing sent beside a bad speed is not half-applied."""
    entry, _, _ = director
    run_id = (await plan(hass, 70, 70))["runs"][0]["run_id"]
    msg = await edit(hass, hass_ws_client, entry, run_id, spacing_cm=30, speed_m_s=9)
    assert not msg["success"]
    assert stored(await schedule(hass, hass_ws_client, entry)) == [(25, 0.4)]


# -- Re-roll angles keeps them; Re-derive from defaults replaces them -----------------------


async def test_re_rolling_angles_keeps_an_edited_runs_spacing_and_speed(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    response = await plan(hass, **STEP_DOWN)
    plan_id = director_of(hass, entry).state["runs"][0]["plan_id"]
    middle = response["runs"][1]["run_id"]
    await edit(hass, hass_ws_client, entry, middle, spacing_cm=22, speed_m_s=0.3)
    msg = await ws(hass, hass_ws_client, entry, "reroll_angles", plan_id=plan_id)
    assert msg["success"], msg
    assert stored(await schedule(hass, hass_ws_client, entry)) == [(25, 0.4), (22, 0.3), (25, 0.4)]


async def test_re_deriving_replaces_an_edited_runs_spacing_and_speed_like_its_height_and_angle(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    response = await plan(hass, **STEP_DOWN, settings={"spacing_cm": 30})
    plan_id = director_of(hass, entry).state["runs"][0]["plan_id"]
    middle = response["runs"][1]["run_id"]
    await ws(hass, hass_ws_client, entry, "edit_run", run_id=middle, height_mm=65, angle_deg=12,
             settings={"spacing_cm": 22, "speed_m_s": 0.3})
    msg = await ws(hass, hass_ws_client, entry, "rederive_plan", plan_id=plan_id)
    assert msg["success"], msg
    view = await schedule(hass, hass_ws_client, entry)
    # Back to the Plan's own Spacing and the Mower's speed; height and angle go back too.
    assert stored(view) == [(30, 0.4)] * 3
    assert [r["height_mm"] for r in view["runs"]] == [79, 69, 61]
    assert all(r.get("angle_edited") is not True for r in view["runs"])
