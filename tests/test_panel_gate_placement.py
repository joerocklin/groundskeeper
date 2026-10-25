"""Gate placement along a Channel in the panel (issue #72), pure helpers run through node."""

from __future__ import annotations

import math
import re

import pytest

from tests.test_panel_channel_geometry import (
    BUMP, GATE_HALF, ZONES, channel, gate_list, points,
)
from tests.test_panel_zones import call  # noqa: F401  (also applies the node skip)

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="node missing"
)

L_SHAPE = [[0, 0], [10, 0], [10, 30]]


# -- snapping ----------------------------------------------------------------------


def test_nearest_fraction_snaps_to_the_closest_point_by_arc_length() -> None:
    # 40 long overall; a point beside the long leg, 10 up it, is 20/40 along.
    assert call("nearestFraction", L_SHAPE, [14, 10]) == pytest.approx(0.5)
    assert call("nearestFraction", L_SHAPE, [5, -3]) == pytest.approx(0.125)


def test_nearest_fraction_clamps_past_either_end() -> None:
    assert call("nearestFraction", L_SHAPE, [-50, -5]) == 0
    assert call("nearestFraction", L_SHAPE, [10, 90]) == 1


def test_nearest_fraction_on_a_straight_fallback_line_and_a_degenerate_one() -> None:
    assert call("nearestFraction", [[0, 0], [100, 0]], [25, 40]) == pytest.approx(0.25)
    assert call("nearestFraction", [[3, 3], [3, 3]], [9, 9]) == 0


# -- rendering ---------------------------------------------------------------------


def gate_centre(svg: str, gate_id: str = "g1", nth: int = 0) -> tuple[float, float]:
    block = svg.split(f'data-gate="{gate_id}"')[1 + nth]
    m = re.search(r'<rect x="([^"]+)" y="([^"]+)"', block)
    return float(m.group(1)) + GATE_HALF, float(m.group(2)) + GATE_HALF


def along(pts, f):
    return call("polylinePoint", pts, f)


def test_a_gate_is_drawn_at_its_saved_fraction_and_half_way_by_default() -> None:
    chans = [channel("c1", "z1", "z2", BUMP)]
    pts = points(call("renderMap", ZONES, chans, {}), "polyline", "c1")
    saved = {"id": "g1", "name": "G", "channels": ["c1"], "open": False, "positions": {"c1": 0.2}}
    svg = call("renderMap", ZONES, chans, {"gates": gate_list(saved)})
    assert gate_centre(svg) == pytest.approx(along(pts, 0.2), abs=0.02)
    bare = {"id": "g1", "name": "G", "channels": ["c1"], "open": False}
    svg = call("renderMap", ZONES, chans, {"gates": gate_list(bare)})
    assert gate_centre(svg) == pytest.approx(along(pts, 0.5), abs=0.02)


def test_a_channel_without_geometry_places_on_the_straight_line() -> None:
    chans = [{"id": "c1", "end_a": "z1", "end_b": "z2", "path": None}]
    saved = {"id": "g1", "name": "G", "channels": ["c1"], "open": False, "positions": {"c1": 0.25}}
    svg = call("renderMap", ZONES, chans, {"gates": gate_list(saved)})
    m = re.search(r'<line class="channel" data-channel="c1" x1="([^"]+)" y1="([^"]+)" x2="([^"]+)" y2="([^"]+)"', svg)
    x1, y1, x2, y2 = map(float, m.groups())
    cx, cy = gate_centre(svg)
    assert (cx, cy) == pytest.approx((x1 + (x2 - x1) * 0.25, y1 + (y2 - y1) * 0.25), abs=0.02)


def edit(positions=None):
    return {"id": "g1", "name": "G", "channels": ["c1", "c2"], "open": False,
            "positions": positions or {}}


TWO = [channel("c1", "z1", "z2", BUMP), channel("c2", "dock", "z1", None)]


def test_the_edit_view_has_one_handle_and_place_target_per_channel() -> None:
    svg = call("renderMap", ZONES, TWO, {"gates": [], "editGate": edit({"c1": 0.1, "c2": 0.9})})
    assert svg.count("data-gate-handle=") == 2
    assert svg.count("data-place=") == 2
    assert 'data-gate-handle="g1"' in svg and 'data-channel-id="c2"' in svg


def test_the_edit_view_replaces_the_saved_marker_of_the_same_gate() -> None:
    saved = {**edit({"c1": 0.9}), "channels": ["c1"]}
    svg = call("renderMap", ZONES, TWO, {"gates": [saved], "editGate": edit({"c1": 0.1})})
    assert svg.count('data-gate="g1"') == 2  # c1 and c2 markers of the draft, no saved twin


def test_the_now_tab_map_is_read_only() -> None:
    svg = call("renderMap", ZONES, TWO, {"gates": [edit({"c1": 0.1})]})
    assert 'class="gate' in svg
    assert "data-gate-handle" not in svg and "data-place" not in svg


# -- draft and payload --------------------------------------------------------------


def test_draft_carries_positions_and_payload_sends_only_picked_channels() -> None:
    d = call("gateDraft", {"id": "g1", "name": "G", "channels": ["c1"], "positions": {"c1": 0.3}})
    assert d["positions"] == {"c1": 0.3}
    assert call("gateDraft", None)["positions"] == {}
    d = {**d, "channels": ["c1"], "positions": {"c1": 0.3, "gone": 0.7}}
    assert call("gatePayload", d)["payload"]["positions"] == {"c1": 0.3}
