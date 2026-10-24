"""Now tab backend: get_now view, run_control and answer_prompt (issue #36, spec §16.2)."""

import asyncio
from dataclasses import replace

from homeassistant.components.lawn_mower import LawnMowerActivity

from custom_components.groundskeeper.adapter import Refused
from tests.test_step_down_plans import director_of, plan, ws  # noqa: F401
from tests.test_tracer_bullet import MOWING, add_plan, advance, director, state_of  # noqa: F401


async def now(hass, hass_ws_client, entry):
    msg = await ws(hass, hass_ws_client, entry, "get_now")
    assert msg["success"], msg
    return msg["result"]


async def test_idle_view_has_status_enabled_check_pills_and_no_controls(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    view = await now(hass, hass_ws_client, entry)
    assert view["status"] == "idle"
    assert view["current_run"] is None and view["up_next"] == []
    names = [c["name"] for c in view["checks"]]
    assert names and "Battery" not in names  # Battery is off by default
    assert all(c["state"] in ("ok", "failing", "idle") for c in view["checks"])
    assert view["controls"] == ["done_today"]


async def test_up_next_flags_reorder_moves_that_would_jump_an_earlier_run_of_the_same_plan(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    a = await plan(hass)  # three step-down Runs
    b = await plan(hass, 70, 70)
    view = await now(hass, hass_ws_client, entry)
    rows = view["up_next"]
    assert [r["run_id"] for r in rows][:3] == [r["run_id"] for r in a["runs"]]
    assert rows[-1]["run_id"] == b["runs"][0]["run_id"]
    assert (rows[0]["can_up"], rows[0]["can_down"]) == (False, False)  # same-Plan neighbours
    assert rows[2]["can_down"] is True and rows[3]["can_up"] is True
    assert rows[1]["can_up"] is False
    first = rows[0]
    assert first["zone_name"] == "Front Lawn" and first["zone_id"]
    assert first["n"] == 1 and first["of"] == 3 and first["height_mm"] > 0
    assert "angle_deg" in first


async def test_running_view_shows_current_run_battery_and_height_check(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(replace(MOWING, battery=64))
    await hass.async_block_till_done()
    view = await now(hass, hass_ws_client, entry)
    cur = view["current_run"]
    assert view["status"] == "running"
    assert cur["zone_name"] == "Front Lawn" and cur["height_mm"] == 70
    assert (cur["n"], cur["of"]) == (1, 1)
    assert cur["status"] == "mowing" and cur["battery"] == 64
    assert "height_check" in cur and "angle_deg" in cur
    assert cur["zone_id"] == view["current_zone_id"]
    assert set(view["controls"]) == {"pause", "stop_now", "done_today"}


async def test_waiting_view_lists_failing_checks_with_reasons(
    hass, hass_ws_client, director
) -> None:
    entry, mower, _ = director
    mower.emit_snapshot(replace(mower.snapshot, battery=1))
    await add_plan(hass, start=True)
    await hass.async_block_till_done()
    view = await now(hass, hass_ws_client, entry)
    assert view["status"] == "waiting"
    assert {"check": "mower_ready", "name": "Mower ready", "reason": "battery_low"} in view["waiting_on"]
    pill = next(c for c in view["checks"] if c["id"] == "mower_ready")
    assert pill["state"] == "failing"
    assert "pause" in view["controls"]


async def test_failed_run_offers_retry_skip_plan_and_abort(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.script_result("start_run", Refused("no"))
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    view = await now(hass, hass_ws_client, entry)
    assert view["status"] == "paused"
    assert view["failure"]["reason"]
    assert set(view["controls"]) >= {"retry", "skip_plan", "abort"}
    assert "resume" not in view["controls"]


async def test_run_control_applies_the_same_semantics_as_the_services(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.script_result("start_run", Refused("no"))
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    msg = await ws(hass, hass_ws_client, entry, "run_control", action="abort")
    assert msg["success"], msg
    assert state_of(hass, "sensor.example_mower_schedule").state == "idle"


async def test_run_control_refuses_what_does_not_apply(hass, hass_ws_client, director) -> None:
    entry, _, _ = director
    msg = await ws(hass, hass_ws_client, entry, "run_control", action="retry")
    assert not msg["success"] and msg["error"]["code"] == "no_failure_to_answer"


async def test_pause_after_run_is_reported_as_requested(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    msg = await ws(hass, hass_ws_client, entry, "run_control", action="pause")
    assert msg["success"], msg
    assert (await now(hass, hass_ws_client, entry))["pause_requested"] is True


async def test_answer_prompt_gives_the_panel_the_phone_actions(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    mower.script_result("start_run", Refused("no"))
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    [prompt] = director_of(hass, entry).state["prompts"]
    bad = await ws(hass, hass_ws_client, entry, "answer_prompt",
                   prompt_id=prompt["prompt_id"], action="resume")
    assert not bad["success"] and bad["error"]["code"] == "prompt_action_invalid"
    stale = await ws(hass, hass_ws_client, entry, "answer_prompt",
                     prompt_id="nope", action="retry")
    assert not stale["success"] and stale["error"]["code"] == "prompt_not_pending"
    ok = await ws(hass, hass_ws_client, entry, "answer_prompt",
                  prompt_id=prompt["prompt_id"], action="skip_plan")
    assert ok["success"], ok
    assert director_of(hass, entry).state["prompts"] == []


async def test_subscribe_now_pushes_the_view_whenever_the_director_changes(
    hass, hass_ws_client, director
) -> None:
    entry, mower, _ = director
    client = await hass_ws_client(hass)
    await client.send_json({"id": 7, "type": "groundskeeper/subscribe_now",
                            "entry_id": entry.entry_id})
    assert (await client.receive_json())["success"]
    first = await client.receive_json()
    assert first["type"] == "event" and first["event"]["status"] == "idle"
    await add_plan(hass, start=True)
    mower.emit_snapshot(replace(mower.snapshot, battery=1))
    await hass.async_block_till_done()
    # events queued before this request's result are the pushes
    await client.send_json({"id": 99, "type": "groundskeeper/get_now",
                            "entry_id": entry.entry_id})
    seen = []
    while (msg := await client.receive_json())["id"] != 99:
        seen.append(msg["event"])
    assert seen[-1]["status"] == "waiting" and len(seen[-1]["up_next"]) == 1
    assert seen[-1]["waiting_on"][0]["reason"] == "battery_low"
