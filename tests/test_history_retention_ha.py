"""Run history and retention limits through the panel API (spec §14.3, §14.6)."""

from custom_components.groundskeeper.runtime import async_get_director
from tests.test_checks_ha import setup, ws  # noqa: F401


def run(i, plan="p1", zone="z1", name="Front Lawn", finished=None):
    return {
        "id": f"r{i}", "plan_id": plan, "zone_id": zone, "zone_name": name,
        "status": "done", "index": 1, "of": 1, "angle_deg": 10.0,
        "anomaly_ids": [f"a{i}"], "prompt_outcomes": [],
        "finished_at": finished or f"2026-03-{i:02d}T10:00:00+00:00",
    }


async def seed(entry, count):
    data = await entry.runtime_data.async_data()
    data["history"] = {
        "runs": [run(i, plan=f"p{i}") for i in range(1, count + 1)],
        "plans": {f"p{i}": {"id": f"p{i}", "origin": "panel", "inputs": {"n": i}}
                  for i in range(1, count + 1)},
    }
    return data


async def test_history_pages_newest_first_with_plan_inputs(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    await seed(entry, 5)
    msg = await ws(hass, hass_ws_client, entry, "history", offset=1, limit=2)
    result = msg["result"]
    assert result["total"] == 5
    assert [r["id"] for r in result["runs"]] == ["r4", "r3"]
    first = result["runs"][0]
    assert first["zone_name"] == "Front Lawn" and first["anomaly_ids"] == ["a4"]
    assert first["plan"]["inputs"] == {"n": 4}


async def test_retention_defaults_come_from_the_spec(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    result = (await ws(hass, hass_ws_client, entry, "get_retention"))["result"]
    assert result["limits"] == {
        "history_runs_max": 500, "duration_samples_max": 20, "battery_samples_max": 5,
    }
    assert result["fixed"] == {"command_log": 20}
    assert result["warnings"] == []
    assert async_get_director(hass, entry.entry_id).env().history_runs_max == 500


async def test_capability_warnings_are_allowed_not_blocked(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    msg = await ws(hass, hass_ws_client, entry, "set_retention_limits",
                   limits={"history_runs_max": 0, "duration_samples_max": 2,
                           "battery_samples_max": 0}, confirm=True)
    assert msg["success"]
    codes = {w["code"] for w in msg["result"]["warnings"]}
    assert codes == {"history_empty", "run_long_never", "recharge_warnings_off"}
    assert async_get_director(hass, entry.entry_id).env().duration_samples_max == 2


async def test_out_of_range_limits_are_rejected(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    msg = await ws(hass, hass_ws_client, entry, "set_retention_limits",
                   limits={"history_runs_max": 2001}, confirm=True)
    assert not msg["success"] and msg["error"]["code"] == "invalid_format"


async def test_lowering_previews_then_prunes_only_on_confirm(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    data = await seed(entry, 5)
    before_state = repr(data["state"])
    preview = (await ws(hass, hass_ws_client, entry, "set_retention_limits",
                        limits={"history_runs_max": 3}))["result"]
    assert preview["applied"] is False
    assert preview["preview"] == {"count": 2, "oldest": "2026-03-01T10:00:00+00:00", "samples": 0}
    assert len(data["history"]["runs"]) == 5
    assert (await ws(hass, hass_ws_client, entry, "get_retention"))["result"]["limits"][
        "history_runs_max"] == 500

    done = (await ws(hass, hass_ws_client, entry, "set_retention_limits",
                     limits={"history_runs_max": 3}, confirm=True))["result"]
    assert done["applied"] is True
    assert [r["id"] for r in data["history"]["runs"]] == ["r3", "r4", "r5"]
    assert sorted(data["history"]["plans"]) == ["p3", "p4", "p5"]
    assert repr(data["state"]) == before_state  # limits never touch state

    await ws(hass, hass_ws_client, entry, "set_retention_limits",
             limits={"history_runs_max": 100})
    assert len(data["history"]["runs"]) == 3  # raising restores nothing
    assert async_get_director(hass, entry.entry_id).env().history_runs_max == 100


async def test_lowering_duration_samples_trims_each_zone(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    data = await entry.runtime_data.async_data()
    data["learned"]["durations"] = {"z1": [{"value": i} for i in range(6)],
                                    "z2": [{"value": 1}]}
    msg = await ws(hass, hass_ws_client, entry, "set_retention_limits",
                   limits={"duration_samples_max": 4})
    assert msg["result"]["applied"] is False
    assert msg["result"]["preview"]["samples"] == 2
    await ws(hass, hass_ws_client, entry, "set_retention_limits",
             limits={"duration_samples_max": 4}, confirm=True)
    assert [s["value"] for s in data["learned"]["durations"]["z1"]] == [2, 3, 4, 5]
    assert len(data["learned"]["durations"]["z2"]) == 1


async def test_deleting_a_missing_zone_drops_its_samples_but_history_keeps_the_name(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    data = await seed(entry, 2)
    data["config"]["zones"].append(
        {"id": "zgone", "native_ref": "gone", "name": "Old Lawn", "hidden": False,
         "missing": True, "polygon": None,
         "angle_range": {"tolerance_override": 0.2}})
    data["history"]["runs"][0].update(zone_id="zgone", zone_name="Old Lawn")
    data["learned"]["durations"] = {"zgone": [{"value": 1}], "z1": [{"value": 2}]}
    msg = await ws(hass, hass_ws_client, entry, "delete_zone", zone_id="zgone")
    assert msg["success"]
    assert all(z["id"] != "zgone" for z in data["config"]["zones"])
    assert "zgone" not in data["learned"]["durations"] and "z1" in data["learned"]["durations"]
    assert data["history"]["runs"][0]["zone_name"] == "Old Lawn"


async def test_only_a_missing_zone_can_be_deleted(hass, hass_ws_client, setup) -> None:
    entry, _ = setup
    zone = (await entry.runtime_data.async_data())["config"]["zones"][0]
    msg = await ws(hass, hass_ws_client, entry, "delete_zone", zone_id=zone["id"])
    assert not msg["success"] and msg["error"]["code"] == "not_allowed"


async def test_lowering_battery_samples_trims_return_and_resume(
    hass, hass_ws_client, setup
) -> None:
    entry, _ = setup
    data = await entry.runtime_data.async_data()
    data["learned"]["battery_levels"] = {"return": [30, 28, 26, 24], "resume": [90, 92]}
    await ws(hass, hass_ws_client, entry, "set_retention_limits",
             limits={"battery_samples_max": 2}, confirm=True)
    assert data["learned"]["battery_levels"] == {"return": [26, 24], "resume": [90, 92]}
