"""Zone bearing from a Zone polygon (spec §4.5). Pure function, no HA."""

import json
import math
from pathlib import Path

import pytest

from custom_components.mowing_director.zone_bearing import zone_bearing

ZONE_JSON = (
    Path(__file__).parent.parent
    / "tests/fixtures/zone.json"
)
LAT0, LON0 = 39.3956, -84.4569


def rect(length, width, bearing_deg, *, lat0=LAT0, lon0=LON0):
    """WGS84 ring of a length x width (m) rectangle whose long side runs
    along the given compass bearing."""
    m_lat = 110540.0
    m_lon = 111320.0 * math.cos(math.radians(lat0))
    b = math.radians(bearing_deg)
    long_u = (math.sin(b), math.cos(b))  # (east, north)
    short_u = (math.cos(b), -math.sin(b))
    ring = []
    for s, t in [(-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1)]:
        e = s * length / 2 * long_u[0] + t * width / 2 * short_u[0]
        n = s * length / 2 * long_u[1] + t * width / 2 * short_u[1]
        ring.append([lon0 + e / m_lon, lat0 + n / m_lat])
    return ring


def real_zone(title):
    data = json.loads(ZONE_JSON.read_text())
    for f in data["features"]:
        if f["properties"].get("title") == title:
            return f["geometry"]["coordinates"][0]
    raise KeyError(title)


@pytest.mark.parametrize("bearing", [0, 3.4, 45, 90, 135, 170])
def test_bearing_is_long_axis_of_rectangle(bearing):
    result = zone_bearing(rect(30, 10, bearing))
    assert result.bearing == pytest.approx(bearing, abs=0.1)
    assert result.aspect == pytest.approx(3.0, rel=0.01)
    assert result.fill == pytest.approx(1.0, abs=0.01)
    assert result.warnings == ()


def test_bearing_folds_to_0_180():
    # Same line, drawn as 190 degrees: folds to 10.
    result = zone_bearing(rect(30, 10, 190))
    assert result.bearing == pytest.approx(10, abs=0.1)
    assert 0 <= result.bearing < 180


def test_ring_orientation_and_closure_do_not_matter():
    ring = rect(30, 10, 60)
    open_reversed = ring[:-1][::-1]
    assert zone_bearing(open_reversed).bearing == pytest.approx(60, abs=0.1)


def test_no_polygon_is_unset():
    for empty in (None, [], [[LON0, LAT0], [LON0, LAT0 + 1e-4]]):
        result = zone_bearing(empty)
        assert result.bearing is None
        assert result.warnings == ("unset",)


def test_ambiguous_below_1_2_aspect():
    assert zone_bearing(rect(11.9, 10, 20)).warnings == ("ambiguous",)
    assert zone_bearing(rect(12.1, 10, 20)).warnings == ()


def test_irregular_below_half_fill():
    # Thin L shape (fill ~0.36 of its bounding rectangle), axis-aligned.
    m_lat = 110540.0
    m_lon = 111320.0 * math.cos(math.radians(LAT0))
    pts = [(0, 0), (30, 0), (30, 3), (3, 3), (3, 30), (0, 30), (0, 0)]
    ring = [[LON0 + x / m_lon, LAT0 + y / m_lat] for x, y in pts]
    result = zone_bearing(ring)
    assert result.fill < 0.5
    assert "irregular" in result.warnings


def test_irregular_threshold():
    # Right triangle fills exactly 0.5 of its rectangle: not irregular.
    m_lat = 110540.0
    m_lon = 111320.0 * math.cos(math.radians(LAT0))
    pts = [(0, 0), (40, 0), (0, 20), (0, 0)]
    ring = [[LON0 + x / m_lon, LAT0 + y / m_lat] for x, y in pts]
    assert "irregular" not in zone_bearing(ring).warnings


def test_real_backyard_is_ambiguous():
    result = zone_bearing(real_zone("Backyard"))
    assert result.aspect == pytest.approx(1.06, abs=0.01)
    assert "ambiguous" in result.warnings


def test_real_behind_the_fence_is_irregular():
    result = zone_bearing(real_zone("Behind the fence"))
    assert result.fill == pytest.approx(0.08, abs=0.01)
    assert "irregular" in result.warnings


@pytest.mark.parametrize(
    ("title", "expected"), [("Side yard", 3.4), ("Front yard", 4.0)]
)
def test_real_zones_match_measured_long_axis(title, expected):
    # Long axes recorded in design ticket 17 (path-angle semantics).
    result = zone_bearing(real_zone(title))
    assert result.bearing == pytest.approx(expected, abs=0.5)
    assert result.warnings == ()
