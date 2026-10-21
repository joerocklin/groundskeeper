"""Zone bearing and Allowed angle range records (spec §4.5, §5.4, §14.3)."""

from custom_components.mowing_director.zone_angles import (
    DEFAULT_SETTINGS,
    refresh_zone_angles,
)
from tests.test_zone_bearing import rect


def _poly(ring):
    return {"type": "Polygon", "coordinates": [ring]}


def _zone(polygon=None, **kw):
    return {"id": "z1", "polygon": polygon, **kw}


LONG = _poly(rect(40, 8, 30))  # long axis at 30 degrees


def test_no_polygon_yet_is_pending() -> None:
    z = refresh_zone_angles(_zone(None), DEFAULT_SETTINGS)
    assert z["bearing"]["status"] == "pending"
    assert z["bearing"]["value"] is None
    assert z["angle_range"]["angle_range"] == "unset"


def test_unusable_polygon_is_unset() -> None:
    z = refresh_zone_angles(_zone(_poly([[0, 0], [1, 1]])), DEFAULT_SETTINGS)
    assert z["bearing"]["status"] == "unset"
    assert z["bearing"]["derived_value"] is None


def test_derived_bearing_record() -> None:
    b = refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS)["bearing"]
    assert b["source"] == "derived"
    assert abs(b["value"] - 30) < 0.5 and b["value"] == b["derived_value"]
    assert (b["ambiguous"], b["irregular"], b["status"]) == (False, False, "ready")


def test_ambiguous_flag() -> None:
    z = refresh_zone_angles(_zone(_poly(rect(10, 9, 0))), DEFAULT_SETTINGS)
    assert z["bearing"]["ambiguous"] is True


def test_manual_bearing_survives_refresh_and_derived_keeps_updating() -> None:
    z = refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS)
    z["bearing"].update(source="manual", value=100.0)
    z["polygon"] = _poly(rect(40, 8, 60))
    z = refresh_zone_angles(z, DEFAULT_SETTINGS)
    b = z["bearing"]
    assert (b["source"], b["value"]) == ("manual", 100.0)
    assert abs(b["derived_value"] - 60) < 0.5


def test_range_record_derived() -> None:
    r = refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS)["angle_range"]
    assert r["source"] == "derived"
    assert r["angle_range"] == r["derived"]
    assert r["basis"]["tolerance"] == 0.25
    assert r["tolerance_override"] is None
    assert isinstance(r["derived"], (dict, str))


def test_manual_range_survives_but_derived_is_kept_fresh() -> None:
    z = refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS)
    z["angle_range"].update(source="manual", angle_range={"min": -5, "max": 5})
    z = refresh_zone_angles(z, {**DEFAULT_SETTINGS, "range_tolerance": 0.01})
    r = z["angle_range"]
    assert (r["source"], r["angle_range"]) == ("manual", {"min": -5, "max": 5})
    assert r["basis"]["tolerance"] == 0.01
    assert r["derived"] != {"min": -5, "max": 5}


def test_zone_tolerance_overrides_mower_tolerance() -> None:
    z = refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS)
    z["angle_range"]["tolerance_override"] = 0.0
    z = refresh_zone_angles(z, DEFAULT_SETTINGS)
    assert z["angle_range"]["basis"]["tolerance"] == 0.0
    assert z["angle_range"]["derived"] != "full"


def test_basis_follows_mower_settings() -> None:
    z = refresh_zone_angles(
        _zone(LONG), {**DEFAULT_SETTINGS, "spacing": 0.3, "speed": 0.5,
                      "perimeter_laps": 2, "nogo_laps": 3},
    )
    assert z["angle_range"]["basis"] == {
        "tolerance": 0.25, "spacing": 0.3, "speed": 0.5,
        "perimeter_laps": 2, "nogo_laps": 3,
    }


def test_manual_bearing_drives_range_without_polygon_bearing() -> None:
    z = refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS)
    z["bearing"].update(source="manual", value=0.0)
    r1 = refresh_zone_angles(z, DEFAULT_SETTINGS)["angle_range"]["derived"]
    r0 = z["angle_range"]["derived"]
    assert r1 != r0


# -- estimated times for the panel (issue #28) ---------------------------------

from custom_components.mowing_director.zone_angles import angle_estimates  # noqa: E402


def test_estimates_at_base_and_both_edges() -> None:
    z = refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS)
    est = angle_estimates(z)
    assert set(est) == {"base", "min_edge", "max_edge"}
    # The derived range is "within tolerance of the best angle" (spec 5.4).
    assert est["base"]["time_s"] > 0
    assert est["max_edge"]["time_s"] <= est["base"]["time_s"] * 1.25 * 1.05 + 1
    assert est["base"]["stripe_length"] > 0


def test_estimates_follow_a_manual_range() -> None:
    z = refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS)
    z["angle_range"].update(source="manual", angle_range={"min": -5, "max": 5})
    est = angle_estimates(z)
    wide = angle_estimates(refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS))
    assert est["max_edge"]["time_s"] != wide["max_edge"]["time_s"]


def test_full_range_has_base_but_no_edges() -> None:
    z = refresh_zone_angles(_zone(LONG), DEFAULT_SETTINGS)
    z["angle_range"].update(source="manual", angle_range="full")
    est = angle_estimates(z)
    assert est["base"] is not None
    assert est["min_edge"] is None and est["max_edge"] is None


def test_no_estimates_while_unset_or_pending() -> None:
    assert angle_estimates(refresh_zone_angles(_zone(None), DEFAULT_SETTINGS)) is None
