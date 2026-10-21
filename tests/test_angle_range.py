"""Allowed angle range estimate (spec 5.4). Pure functions, no HA."""

import json
import math
import time
from pathlib import Path

import pytest

from custom_components.mowing_director.angle_range import (
    AngleRange,
    Basis,
    double_grid_estimate,
    estimate_angle_range,
    estimate_at,
)

ZONE_JSON = (
    Path(__file__).parent.parent
    / "tests/fixtures/zone.json"
)
LAT0, LON0 = 39.3956, -84.4569
M_LAT = 110540.0
M_LON = 111320.0 * math.cos(math.radians(LAT0))

# Spacing 1 m, 0.4 m/s, no laps: hand-countable numbers.
PLAIN = Basis(tolerance=0.25, spacing=1.0, speed=0.4, perimeter_laps=0, nogo_laps=0)


def ring_of(pts):
    return [[LON0 + x / M_LON, LAT0 + y / M_LAT] for x, y in pts]


def rect(length, width, bearing_deg=0.0, cx=0.0, cy=0.0):
    """Ring of a length x width rectangle whose long side runs along the
    compass bearing, centred on (cx, cy) metres from the origin."""
    b = math.radians(bearing_deg)
    lu = (math.sin(b), math.cos(b))
    su = (math.cos(b), -math.sin(b))
    pts = []
    for s, t in [(-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1)]:
        pts.append(
            (
                cx + s * length / 2 * lu[0] + t * width / 2 * su[0],
                cy + s * length / 2 * lu[1] + t * width / 2 * su[1],
            )
        )
    return ring_of(pts)


def real(title, kind="area"):
    data = json.loads(ZONE_JSON.read_text())
    return [
        f["geometry"]["coordinates"][0]
        for f in data["features"]
        if f["properties"].get("title") == title
    ][0]


def real_obstacles():
    data = json.loads(ZONE_JSON.read_text())
    return [
        f["geometry"]["coordinates"][0]
        for f in data["features"]
        if f["properties"].get("type_name") == "obstacle"
    ]


# --- per-angle estimate -----------------------------------------------


def test_rectangle_along_stripes():
    # 20 x 10 m, stripes along the long side: 10 stripes of 20 m.
    e = estimate_at(rect(20, 10), 0.0, 0.0, basis=PLAIN)
    assert e.stripe_count == 10
    assert e.time_s == pytest.approx(200 / 0.4 + 10 * 8)  # 580


def test_rectangle_across_stripes():
    e = estimate_at(rect(20, 10), 0.0, 90.0, basis=PLAIN)
    assert e.stripe_count == 20
    assert e.time_s == pytest.approx(200 / 0.4 + 20 * 8)  # 660


def test_perimeter_laps_shrink_interior():
    # One lap of 1 m: interior 18 x 8, stripes at 1.5..8.5 (8 stripes, 18 m).
    basis = Basis(0.25, 1.0, 0.4, perimeter_laps=1, nogo_laps=0)
    e = estimate_at(rect(20, 10), 0.0, 0.0, basis=basis)
    assert e.stripe_count == 8
    assert e.time_s == pytest.approx(8 * 18 / 0.4 + 8 * 8)  # 424


def test_obstacle_splits_stripes_and_removes_length():
    # 2 x 2 m obstacle in the middle: two stripes split in two, 4 m removed.
    e = estimate_at(
        rect(20, 10), 0.0, 0.0, obstacles=[rect(2, 2)], basis=PLAIN
    )
    assert e.stripe_count == 12
    assert e.time_s == pytest.approx(196 / 0.4 + 12 * 8)  # 586


def test_nogo_laps_grow_obstacle():
    # Grown by 1 m (rounded corners): stripes at y=+-0.5 lose 4 m each, at
    # y=+-1.5 they lose 2*(1+sqrt(1-0.5**2)) m each; four stripes split.
    basis = Basis(0.25, 1.0, 0.4, perimeter_laps=0, nogo_laps=1)
    e = estimate_at(rect(20, 10), 0.0, 0.0, obstacles=[rect(2, 2)], basis=basis)
    assert e.stripe_count == 10 + 4
    assert e.time_s == pytest.approx((200 - 8 - 2 * 2 * (1 + math.sqrt(0.75))) / 0.4 + 14 * 8)


def test_relative_angle_is_clockwise_from_bearing():
    zone = rect(40, 4, 30.0)
    a = estimate_at(zone, 30.0, 0.0, basis=PLAIN)
    b = estimate_at(zone, 0.0, 30.0, basis=PLAIN)
    c = estimate_at(zone, 0.0, -30.0, basis=PLAIN)
    assert a.time_s == pytest.approx(b.time_s, rel=1e-6)
    assert c.time_s > a.time_s * 1.5


def test_relative_angle_normalised_to_half_turn():
    zone = rect(30, 8, 10.0)
    a = estimate_at(zone, 10.0, 90.0, basis=PLAIN)
    b = estimate_at(zone, 10.0, -90.0, basis=PLAIN)
    c = estimate_at(zone, 10.0, 270.0, basis=PLAIN)
    assert a.time_s == pytest.approx(b.time_s)
    assert a.time_s == pytest.approx(c.time_s)


def test_double_grid_is_both_stripe_passes():
    zone = rect(20, 10)
    one = estimate_at(zone, 0.0, 0.0, basis=PLAIN)
    two = estimate_at(zone, 0.0, 90.0, basis=PLAIN)
    dg = double_grid_estimate(zone, 0.0, 0.0, 90.0, basis=PLAIN)
    assert dg.stripe_count == one.stripe_count + two.stripe_count
    assert dg.time_s == pytest.approx(one.time_s + two.time_s)


# --- range -------------------------------------------------------------


def test_square_is_full():
    r = estimate_angle_range(rect(10, 10), 0.0, basis=PLAIN)
    assert r.angle_range == "full"
    assert r.derived == "full"


def test_narrow_strip_range_is_contiguous_span_around_zero():
    r = estimate_angle_range(rect(40, 4), 0.0, basis=PLAIN)
    rng = r.angle_range
    assert rng != "full"
    # Hand sweep: +-15 deg passes (+18 %), +-20 deg fails (+27 %).
    assert -20 <= rng["min"] <= -14
    assert 14 <= rng["max"] <= 20


def test_unset_bearing_is_unset():
    r = estimate_angle_range(rect(40, 4), None, basis=PLAIN)
    assert r.angle_range == "unset"
    assert r.derived == "unset"


def test_no_polygon_is_unset():
    assert estimate_angle_range(None, 0.0, basis=PLAIN).angle_range == "unset"


def test_tolerance_widens_range():
    tight = estimate_angle_range(rect(40, 4), 0.0, basis=Basis(0.05, 1, 0.4, 0, 0))
    loose = estimate_angle_range(rect(40, 4), 0.0, basis=Basis(0.50, 1, 0.4, 0, 0))
    assert tight.angle_range["max"] < loose.angle_range["max"]


def test_island_not_touching_zero_ignored():
    # Zero-time ties: a result is never wider than the contiguous span.
    r = estimate_angle_range(rect(40, 4), 0.0, basis=PLAIN)
    sweep = [
        (a, estimate_at(rect(40, 4), 0.0, a, basis=PLAIN).time_s)
        for a in range(-89, 91)
    ]
    best = min(t for _, t in sweep)
    first_fail = min(a for a, t in sweep if a > 0 and t > best * 1.25)
    assert r.angle_range["max"] < first_fail


def test_result_carries_basis_and_json_forms():
    r = estimate_angle_range(rect(40, 4), 0.0, basis=PLAIN)
    assert r.basis == PLAIN
    assert r.to_dict()["basis"] == {
        "tolerance": 0.25,
        "spacing": 1.0,
        "speed": 0.4,
        "perimeter_laps": 0,
        "nogo_laps": 0,
    }
    assert set(r.to_dict()["derived"]) == {"min", "max"}
    assert estimate_angle_range(rect(10, 10), 0.0, basis=PLAIN).to_dict()["derived"] == "full"


def test_result_exposes_base_and_edge_estimates():
    r = estimate_angle_range(rect(40, 4), 0.0, basis=PLAIN)
    assert r.base.time_s == pytest.approx(estimate_at(rect(40, 4), 0.0, 0.0, basis=PLAIN).time_s)
    assert r.min_edge.time_s > r.base.time_s
    assert r.max_edge.time_s > r.base.time_s


# --- real map (sweep.py reproduction) ----------------------------------


@pytest.fixture(scope="module")
def real_bearing():
    from custom_components.mowing_director.zone_bearing import zone_bearing

    return lambda t: zone_bearing(real(t)).bearing


def span(title, bearing):
    r = estimate_angle_range(
        real(title), bearing, basis=Basis(0.25, 0.25, 0.4, 1, 1)
    )
    return r.angle_range


def test_real_side_yard(real_bearing):
    rng = span("Side yard", real_bearing("Side yard"))
    assert rng != "full"
    assert -27 <= rng["min"] <= -14
    assert 14 <= rng["max"] <= 27


def test_real_behind_the_fence(real_bearing):
    rng = span("Behind the fence", real_bearing("Behind the fence"))
    assert rng != "full"
    assert -30 <= rng["min"] <= -16
    assert 16 <= rng["max"] <= 30


@pytest.mark.parametrize("title", ["Front yard", "Backyard"])
def test_real_round_zones_are_full(real_bearing, title):
    assert span(title, real_bearing(title)) == "full"


def test_real_obstacles_do_not_break_estimate(real_bearing):
    # Obstacles that lie in Backyard still yield a sane, positive estimate.
    e = estimate_at(
        real("Backyard"),
        real_bearing("Backyard"),
        0.0,
        obstacles=real_obstacles(),
        basis=Basis(0.25, 0.25, 0.4, 1, 1),
    )
    assert e.stripe_count > 0 and e.time_s > 0


def test_cost_acceptable_for_executor_job(real_bearing):
    start = time.perf_counter()
    estimate_angle_range(
        real("Backyard"), real_bearing("Backyard"),
        obstacles=real_obstacles(), basis=Basis(0.25, 0.25, 0.4, 1, 1),
    )
    assert time.perf_counter() - start < 10
