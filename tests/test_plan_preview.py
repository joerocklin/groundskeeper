"""Plan preview for the New Plan form (spec §16.3, §5.5; ticket #37)."""

from tests.test_step_down_plans import director_of, front_lawn_id, ws
from tests.test_tracer_bullet import director  # noqa: F401 - fixture


async def test_preview_derives_runs_without_touching_the_schedule(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    msg = await ws(hass, hass_ws_client, entry, "preview_plan", zone="front lawn",
                   start_height_mm=100, end_height_mm=58)
    assert msg["success"], msg
    result = msg["result"]
    runs = result["runs"]
    assert len(runs) >= 2
    assert [r["n"] for r in runs] == list(range(1, len(runs) + 1))
    assert runs[0]["height_mm"] <= 100 and runs[-1]["height_mm"] == 58
    assert all(isinstance(r["estimated_time_s"], (int, float)) and r["estimated_time_s"] > 0
               for r in runs)
    assert all(r["provisional"] is False for r in runs)
    assert result["provisional"] is False
    assert director_of(hass, entry).state["runs"] == []
    assert director_of(hass, entry).state["plans"] == {}


async def test_preview_warns_of_provisional_angles_while_range_unset(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    zone = next(z for z in director_of(hass, entry).data["config"]["zones"]
                if z["id"] == front_lawn_id(hass, entry))
    zone["angle_range"] = {**(zone.get("angle_range") or {}), "angle_range": "unset"}
    msg = await ws(hass, hass_ws_client, entry, "preview_plan", zone="front lawn",
                   start_height_mm=100, end_height_mm=58)
    assert msg["success"], msg
    assert msg["result"]["provisional"] is True
    assert all(r["provisional"] for r in msg["result"]["runs"])
    assert any("provisional" in w.lower() for w in msg["result"]["warnings"])


async def test_preview_reports_invalid_input_as_an_error(
    hass, hass_ws_client, director
) -> None:
    entry, _, _ = director
    msg = await ws(hass, hass_ws_client, entry, "preview_plan", zone="nowhere",
                   start_height_mm=100, end_height_mm=58)
    assert not msg["success"] and msg["error"]["code"] == "unknown_zone"
