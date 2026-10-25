"""Channel path geometry on the map (issue #59), pure helpers run through node."""

from __future__ import annotations

import math
import re

import pytest

from tests.test_panel_zones import call, zone  # (importing also applies the node skip)

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="node missing"
)


# -- arc-length point ------------------------------------------------------------


def test_polyline_point_is_by_arc_length_not_by_vertex() -> None:
    # An L: 10 along x then 30 along y; the half-way point is 20 along, so on the
    # long leg, not at the middle vertex (10, 0).
    assert call("polylinePoint", [[0, 0], [10, 0], [10, 30]], 0.5) == [10, 10]


def test_polyline_point_ends_and_degenerate_lines() -> None:
    line = [[0, 0], [10, 0]]
    assert call("polylinePoint", line, 0) == [0, 0]
    assert call("polylinePoint", line, 1) == [10, 0]
    assert call("polylinePoint", [[3, 3], [3, 3]], 0.5) == [3, 3]


# -- orientation -----------------------------------------------------------------


def test_orient_path_flips_a_polyline_that_runs_from_b_to_a() -> None:
    pts = [[100, 0], [50, 5], [0, 0]]  # starts at b
    assert call("orientPath", pts, [0, 0], [100, 0]) == [[0, 0], [50, 5], [100, 0]]


def test_orient_path_keeps_a_polyline_already_running_a_to_b() -> None:
    pts = [[0, 0], [50, 5], [100, 0]]
    assert call("orientPath", pts, [0, 0], [100, 0]) == pts


def test_orient_path_keeps_the_given_order_when_ambiguous() -> None:
    pts = [[0, 0], [100, 0]]
    assert call("orientPath", pts, [50, 0], [50, 0]) == pts


# -- renderMap -------------------------------------------------------------------

def square(lon0: float) -> dict:
    return {"type": "Polygon", "coordinates": [[
        [lon0, 0], [lon0 + 0.001, 0], [lon0 + 0.001, 0.001], [lon0, 0.001], [lon0, 0],
    ]]}


Z1 = zone(id="z1", name="Front", polygon=square(0))
Z2 = zone(id="z2", name="Back", polygon=square(0.003))
ZONES = [Z1, Z2]
BUMP = {"type": "LineString",
        "coordinates": [[0.001, 0.0005], [0.002, 0.0015], [0.003, 0.0005]]}
DOCK_PATH = {"type": "LineString",  # given dock-first, ends at Front's edge
             "coordinates": [[-0.002, 0.0005], [-0.001, 0.0005], [0, 0.0005]]}


GATE_HALF = 6  # half the drawn gate square


def gate_list(*gates):
    return list(gates)


def channel(id_, a, b, path=None):
    return {"id": id_, "native_ref": id_, "end_a": a, "end_b": b, "path": path}


def numbers(s: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", s)]


def points(svg: str, tag: str, cid: str) -> list[list[float]]:
    m = re.search(rf'<{tag} class="channel[^"]*" data-channel="{cid}" points="([^"]+)"', svg)
    assert m, f"no {tag} for {cid}"
    flat = numbers(m.group(1))
    return [flat[i:i + 2] for i in range(0, len(flat), 2)]


def centre_of(svg: str, zone_id: str) -> list[float]:
    m = re.search(rf'data-zone="{zone_id}" points="([^"]+)"', svg)
    flat = numbers(m.group(1))
    pts = [flat[i:i + 2] for i in range(0, len(flat), 2)]
    return [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]


def test_a_channel_with_a_path_is_drawn_along_it() -> None:
    svg = call("renderMap", ZONES, [channel("c1", "z1", "z2", BUMP)], {})
    assert len(points(svg, "polyline", "c1")) == 3
    assert '<line class="channel"' not in svg


def test_a_channel_without_a_path_keeps_the_straight_line() -> None:
    svg = call("renderMap", ZONES, [channel("c1", "z1", "z2")], {})
    assert '<line class="channel" data-channel="c1"' in svg
    assert "<polyline" not in svg


def test_a_path_running_b_to_a_is_drawn_from_a_to_b() -> None:
    backwards = {"type": "LineString", "coordinates": BUMP["coordinates"][::-1]}
    svg = call("renderMap", ZONES, [channel("c1", "z1", "z2", backwards)], {})
    pts = points(svg, "polyline", "c1")
    assert math.dist(pts[0], centre_of(svg, "z1")) < math.dist(pts[0], centre_of(svg, "z2"))
    assert math.dist(pts[-1], centre_of(svg, "z2")) < math.dist(pts[-1], centre_of(svg, "z1"))


def test_a_path_outside_every_zone_still_fits_the_map() -> None:
    far = {"type": "LineString", "coordinates": [[0.001, 0.0005], [0.002, 0.01], [0.003, 0.0005]]}
    svg = call("renderMap", ZONES, [channel("c1", "z1", "z2", far)], {})
    for x, y in points(svg, "polyline", "c1"):
        assert 0 <= x <= 400 and 0 <= y <= 300


def dock_at(svg: str) -> tuple[float, float]:
    m = re.search(r'<circle class="dock" cx="([^"]+)" cy="([^"]+)"', svg)
    return float(m.group(1)), float(m.group(2))


def test_the_dock_sits_at_the_dock_end_of_the_dock_channel_path() -> None:
    svg = call("renderMap", ZONES, [channel("c1", "dock", "z1", DOCK_PATH)], {})
    start = points(svg, "polyline", "c1")[0]  # end_a is the dock
    assert dock_at(svg) == pytest.approx(start, abs=0.02)
    assert dock_at(svg) != pytest.approx((16, 284), abs=1)


def test_the_dock_keeps_the_corner_pin_without_a_path() -> None:
    svg = call("renderMap", ZONES, [channel("c1", "dock", "z1")], {})
    assert 'class="dock" cx="16" cy="284"' in svg


def test_the_dock_end_is_found_whichever_way_the_path_runs() -> None:
    zone_first = {"type": "LineString", "coordinates": DOCK_PATH["coordinates"][::-1]}
    svg = call("renderMap", ZONES, [channel("c1", "z1", "dock", zone_first)], {})
    pts = points(svg, "polyline", "c1")
    # end_a is the Zone, so the polyline runs Zone -> dock and the dock is last.
    assert dock_at(svg) == pytest.approx(pts[-1], abs=0.02)


def test_a_gate_sits_at_the_arc_length_midpoint_not_the_middle_vertex() -> None:
    # An L: the short leg is 1 unit, the long leg 3, so half-way along is on the
    # long leg and is not the middle vertex.
    elbow = {"type": "LineString",
             "coordinates": [[0.001, 0.0005], [0.0015, 0.0005], [0.0015, 0.0020]]}
    gate = {"id": "g1", "name": "G", "channels": ["c1"], "open": False}
    svg = call("renderMap", ZONES, [channel("c1", "z1", "z2", elbow)], {"gates": gate_list(gate)})
    pts = points(svg, "polyline", "c1")
    m = re.search(r'<rect x="([^"]+)" y="([^"]+)"', svg)
    centre = (float(m.group(1)) + GATE_HALF, float(m.group(2)) + GATE_HALF)
    legs = [math.dist(pts[0], pts[1]), math.dist(pts[1], pts[2])]
    assert centre != pytest.approx(pts[1], abs=0.5)
    on_long_leg = math.dist(pts[1], centre) == pytest.approx(sum(legs) / 2 - legs[0], abs=0.1)
    assert on_long_leg


def test_the_pick_target_follows_the_path_while_picking() -> None:
    svg = call("renderMap", ZONES, [channel("c1", "z1", "z2", BUMP)], {"pickedChannelIds": []})
    assert re.search(r'<polyline class="channel-hit" data-pick="c1" points="', svg)


def test_a_tied_dock_path_keeps_its_given_order() -> None:
    # Both ends are equally far from Front's centre (0.0004, 0.0004, the mean of
    # its ring vertices): ambiguous, so keep the order.
    tie = {"type": "LineString", "coordinates": [[0.0004, -0.0006], [0.0004, 0.0014]]}
    svg = call("renderMap", [Z1], [channel("c1", "dock", "z1", tie)], {})
    pts = points(svg, "polyline", "c1")
    assert pts[0][1] > pts[1][1]  # the southern point (larger y) stays first
    assert dock_at(svg) == pytest.approx(pts[0], abs=0.02)


def test_a_dock_channel_without_a_path_joins_the_dock_the_other_path_found() -> None:
    other = channel("c2", "dock", "z2", None)
    svg = call("renderMap", ZONES, [channel("c1", "dock", "z1", DOCK_PATH), other], {})
    m = re.search(r'<line class="channel" data-channel="c2" x1="([^"]+)" y1="([^"]+)"', svg)
    assert dock_at(svg) == pytest.approx((float(m.group(1)), float(m.group(2))), abs=0.02)


def test_a_path_that_cannot_be_drawn_does_not_widen_the_map() -> None:
    nowhere = zone(id="z3", name="Nowhere", polygon=None)
    far = {"type": "LineString", "coordinates": [[0.0, 0.0], [5.0, 5.0]]}
    with_far = call("renderMap", [Z1, nowhere], [channel("c1", "z1", "z3", far)], {})
    alone = call("renderMap", [Z1, nowhere], [], {})
    assert centre_of(with_far, "z1") == pytest.approx(centre_of(alone, "z1"))


def test_geometry_other_than_a_line_string_falls_back_to_the_straight_line() -> None:
    multi = {"type": "MultiLineString", "coordinates": [BUMP["coordinates"]]}
    svg = call("renderMap", ZONES, [channel("c1", "z1", "z2", multi)], {})
    assert '<line class="channel" data-channel="c1"' in svg
