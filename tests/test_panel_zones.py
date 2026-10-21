"""Zones & Gates tab helpers (issue #28, spec §16.3), run through node.

The panel is buildless plain JS, so its pure helpers are evaluated in a bare
`vm` context (tests/panel_harness.js). Skipped when node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components.mowing_director.panel import FRONTEND_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")

HARNESS = Path(__file__).with_name("panel_harness.js")
SRC = FRONTEND_DIR / "mowing-director-panel.js"

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]],
}


def js(expr: str):
    out = subprocess.run(
        ["node", str(HARNESS), str(SRC), expr],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def zone(**kw):
    base = {
        "id": "z1", "name": "Front", "hidden": False, "missing": False,
        "polygon": SQUARE,
        "bearing": {"value": 30, "source": "derived", "derived_value": 30,
                    "ambiguous": False, "irregular": False, "status": "ready"},
        "angle_range": {"angle_range": {"min": -10, "max": 20}, "source": "derived",
                        "derived": {"min": -10, "max": 20}},
        "estimates": {"base": {"time_s": 3600, "stripe_length": 30},
                      "min_edge": {"time_s": 3900, "stripe_length": 31},
                      "max_edge": {"time_s": 4200, "stripe_length": 32}},
    }
    base.update(kw)
    return base


def call(fn: str, *args):
    return js(f"{fn}(" + ",".join(json.dumps(a) for a in args) + ")")


# -- map component ------------------------------------------------------------


def test_map_draws_zones_channels_and_stripes_at_the_given_angle() -> None:
    zones = [zone(), zone(id="z2", name="Back")]
    channels = [{"end_a": "z1", "end_b": "z2"}, {"end_a": "z1", "end_b": "dock"}]
    svg = call("renderMap", zones, channels, {"angle": 90, "stripeZoneId": "z1"})
    assert svg.count('class="zone') == 2
    assert svg.count('class="channel"') == 2
    assert 'class="stripes"' in svg
    # Angle 90 = east: stripes are horizontal, so every stripe has equal y.
    lines = [l for l in svg.split("<line ") if 'class="stripe"' in l]
    assert lines
    for l in lines:
        y1 = float(l.split('y1="')[1].split('"')[0])
        y2 = float(l.split('y2="')[1].split('"')[0])
        assert abs(y1 - y2) < 0.01


def test_map_skips_zones_without_polygon_and_marks_missing_and_hidden() -> None:
    svg = call(
        "renderMap",
        [zone(), zone(id="z2", polygon=None), zone(id="z3", missing=True),
         zone(id="z4", hidden=True)],
        [], {},
    )
    assert svg.count('class="zone') == 3
    assert "missing" in svg and "hidden" in svg


def test_map_highlights_selected_and_current_zone() -> None:
    svg = call("renderMap", [zone()], [], {"selectedZoneId": "z1", "currentZoneId": "z1"})
    assert "selected" in svg and "current" in svg


def test_stripes_are_not_drawn_without_an_angle() -> None:
    svg = call("renderMap", [zone()], [], {"stripeZoneId": "z1"})
    assert 'class="stripes"' not in svg


# -- units --------------------------------------------------------------------


def test_length_in_ha_units() -> None:
    assert call("fmtLength", 30, "km") == "30 m"
    assert call("fmtLength", 30, "mi") == "98 ft"


def test_duration() -> None:
    assert call("fmtDuration", 3600) == "1 h 0 min"
    assert call("fmtDuration", 2700) == "45 min"


# -- bearing and range panels -------------------------------------------------


def test_bearing_derived_shows_no_reset_and_no_warning() -> None:
    html = call("bearingHtml", zone())
    assert "30°" in html and "derived" in html
    assert 'data-act="reset-bearing"' not in html
    assert "warn" not in html


def test_bearing_manual_offers_reset_with_derived_value() -> None:
    z = zone()
    z["bearing"].update(source="manual", value=100)
    html = call("bearingHtml", z)
    assert "manual" in html and 'data-act="reset-bearing"' in html
    assert "30°" in html  # derived value remains visible


def test_bearing_warnings() -> None:
    def with_(**kw):
        z = zone()
        z["bearing"].update(**kw)
        return call("bearingHtml", z)

    amb = with_(ambiguous=True)
    assert "ambiguous" in amb
    irr = with_(irregular=True)
    assert "manual bearing" in irr and "splitting" in irr
    assert "Mammotion app" in irr and "perimeter laps only" in irr
    assert "unset" in with_(status="unset", value=None, derived_value=None)
    assert "pending" in with_(status="pending", value=None, derived_value=None)


def test_range_wedge_and_estimates() -> None:
    html = call("rangeHtml", zone(), "km")
    assert "<path" in html and 'class="wedge"' in html
    assert "-10°" in html and "+20°" in html
    assert "1 h 0 min" in html  # base
    assert "1 h 5 min" in html  # min edge, 3900 s
    assert "1 h 10 min" in html  # max edge


def test_range_manual_offers_reset_and_unset_explains() -> None:
    z = zone()
    z["angle_range"].update(source="manual")
    assert 'data-act="reset-range"' in call("rangeHtml", z, "km")
    u = zone(estimates=None)
    u["angle_range"].update(angle_range="unset")
    html = call("rangeHtml", u, "km")
    assert "unset" in html and "<path" not in html


def test_full_range_draws_full_circle_without_edge_times() -> None:
    z = zone()
    z["angle_range"].update(angle_range="full")
    z["estimates"] = {"base": z["estimates"]["base"], "min_edge": None, "max_edge": None}
    html = call("rangeHtml", z, "km")
    assert "full" in html and "<circle" in html


def test_zone_list_rows() -> None:
    html = call("zoneListHtml", [zone(), zone(id="z2", name="Old", missing=True),
                                 zone(id="z3", name="Hid", hidden=True)], "z1")
    assert html.count('data-zone="') >= 3
    assert 'data-act="toggle-hidden"' in html
    assert "missing" in html
    assert 'data-act="refresh-zones"' in html
