"""The Now tab's Mower marker (issue #113), run through node.

Pure helpers through the shared harness; the lifecycle (subscription, in-place
updates, teardown) through a stub DOM in `panel_mower_smoke.js`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_panel_zones import SQUARE, SRC, call, js, zone

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")

CENTRE = {"latitude": 0.0005, "longitude": 0.0005}
SLOT = re.compile(r'<g id="mower-slot">.*</g></svg>', re.S)


def place(fix, zones=None, channels=None):
    return call("mowerPlace", [zone()] if zones is None else zones, channels or [], fix)


# -- placement -----------------------------------------------------------------------


def test_a_fix_is_placed_with_the_maps_own_projection() -> None:
    """Zone vertices and the marker go through one `at`, so they line up."""
    rings = js("projectZones([" + json.dumps(zone()) + "]).rings[0]")
    # The SQUARE's corners are (lon, lat) = (0,0), (0.001,0), (0.001,0.001), (0,0.001).
    corner = place({"latitude": 0.001, "longitude": 0.001, "direction": None})
    assert [corner["x"], corner["y"]] == [round(v, 2) for v in rings[2]]
    origin = place({"latitude": 0, "longitude": 0, "direction": None})
    assert [origin["x"], origin["y"]] == [round(v, 2) for v in rings[0]]


def test_the_map_is_north_up_and_the_direction_is_the_rotation_unconverted() -> None:
    centre = place({**CENTRE, "direction": 90})
    north = place({"latitude": 0.0008, "longitude": 0.0005, "direction": 270})
    assert centre == {"x": 200, "y": 150, "direction": 90}
    assert north["y"] < centre["y"] and north["x"] == centre["x"]  # north is up
    assert north["direction"] == 270
    east = place({"latitude": 0.0005, "longitude": 0.0008})
    assert east["x"] > centre["x"] and east["y"] == centre["y"]


def test_a_fix_without_a_direction_has_no_rotation() -> None:
    assert place({**CENTRE})["direction"] is None
    assert place({**CENTRE, "direction": None})["direction"] is None


@pytest.mark.parametrize(
    "fix",
    [
        None,
        {"latitude": None, "longitude": 0.0005},
        {"latitude": 0.0005},
        {"latitude": "x", "longitude": 0.0005},
        {"latitude": 5, "longitude": 5},  # far outside the map
        {"latitude": 0.0005, "longitude": -3},
    ],
)
def test_a_fix_that_cannot_be_placed_inside_the_map_is_hidden(fix) -> None:
    assert place(fix) is None


def test_there_is_no_marker_while_the_map_has_nothing_to_be_placed_by() -> None:
    assert place(CENTRE, zones=[]) is None
    assert place(CENTRE, zones=[zone(polygon=None)]) is None


# -- the fit does not move ----------------------------------------------------------------


def with_marker(fix, zones, channels=None):
    mark = place(fix, zones, channels)
    return call("renderMap", zones, channels or [], {
        "mower": {"mark": mark, "ageS": 3, "labelOpen": False}})


def test_the_marker_never_changes_the_maps_fit() -> None:
    far = zone(id="z2", name="Back", polygon={
        "type": "Polygon",
        "coordinates": [[[0.01, 0.01], [0.011, 0.01], [0.011, 0.011], [0.01, 0.011], [0.01, 0.01]]]})
    zones = [zone(), far]
    plain = call("renderMap", zones, [], {})
    for fix in (CENTRE, {"latitude": 0.0105, "longitude": 0.0105}, {"latitude": 9, "longitude": 9}):
        assert SLOT.sub("</svg>", with_marker(fix, zones)) == plain  # zones, gates, dock: untouched
    # ...and the same projection places Zone points and the marker alike
    assert call("mowerPlace", zones, [], {"latitude": 0.0105, "longitude": 0.0105}) is not None


def test_the_marker_is_drawn_rotated_with_its_age_as_a_tooltip() -> None:
    svg = call("renderMap", [zone()], [], {"mower": {
        "mark": place({**CENTRE, "direction": 276}), "ageS": 4.2, "labelOpen": False}})
    assert '<g id="mower-slot"><g class="mower" data-shape="arrow" transform="translate(200 150)">' in svg
    assert 'transform="rotate(276)"' in svg
    assert "<title>last fix 4 s ago</title>" in svg
    assert 'class="fix"' not in svg  # the label shows only once tapped


def test_the_marker_is_a_plain_dot_without_a_direction() -> None:
    svg = call("renderMap", [zone()], [], {"mower": {
        "mark": place({**CENTRE, "direction": None}), "ageS": 0, "labelOpen": True}})
    assert 'data-shape="dot"' in svg and '<circle class="glyph" r="5"/>' in svg
    assert '<polygon class="glyph"' not in svg
    assert '<text class="fix" y="-14">last fix 0 s ago</text>' in svg


def test_other_maps_have_no_marker_slot() -> None:
    """The Zones & Gates map and the Gate editor are unchanged."""
    assert "mower" not in call("renderMap", [zone()], [], {})
    assert "mower" not in call("renderMap", [zone()], [], {"gates": []})


def test_an_empty_slot_is_still_drawn_so_a_later_fix_has_somewhere_to_go() -> None:
    svg = call("renderMap", [zone()], [], {"mower": {"mark": None, "ageS": 0, "labelOpen": False}})
    assert '<g id="mower-slot"></g></svg>' in svg


# -- the age -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "text"),
    [
        (0, "last fix 0 s ago"),
        (4.4, "last fix 4 s ago"),
        (89, "last fix 89 s ago"),
        (120, "last fix 2 min ago"),
        (3600, "last fix 60 min ago"),
        (7200, "last fix 2 h ago"),
        (-3, "last fix 0 s ago"),
    ],
)
def test_the_fix_age_reads_in_seconds_minutes_or_hours(age, text) -> None:
    assert call("fixAgeText", age) == text


# -- the panel: subscription, in-place updates, teardown ------------------------------------


@pytest.fixture(scope="module")
def smoke() -> dict:
    out = subprocess.run(
        ["node", str(Path(__file__).with_name("panel_mower_smoke.js")), str(SRC)],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_the_panel_subscribes_once_to_the_dedicated_position_subscription(smoke) -> None:
    assert smoke["subscribeMessage"] == {
        "type": "groundskeeper/subscribe_mower_position", "entry_id": "e"}
    assert smoke["subscribedOnce"] is True  # three renders, one subscription
    assert smoke["slotDrawnEmpty"] is True and smoke["noTimerWithoutFix"] is True


def test_the_marker_moves_and_turns_in_place_without_re_rendering_the_tab(smoke) -> None:
    assert smoke["firstMarker"].startswith('<g class="mower" data-shape="arrow" transform="translate(200 150)">')
    assert smoke["sameElementKept"] is True  # moved, not replaced: a hover tooltip survives
    assert smoke["movedTransform"] == "translate(200 96.4)"
    assert smoke["turnedTo"] == "rotate(180)"
    assert smoke["rendersForPushes"] == 0  # input focus and half-typed text survive
    assert smoke["rendersForTick"] == 0


def test_the_fix_age_counts_on_between_pushes(smoke) -> None:
    assert smoke["title"] == "last fix 1 s ago"
    assert smoke["titleAfterTenSeconds"] == "last fix 11 s ago"
    assert smoke["titleAfterTwoMinutes"] == "last fix 2 min ago"
    assert smoke["timerWhileFix"] == 1


def test_tapping_the_marker_shows_its_age_as_a_label(smoke) -> None:
    assert smoke["labelAfterTap"] == "last fix 4 s ago"
    assert smoke["labelAfterSecondTap"] is False


def test_no_fix_or_a_place_off_the_map_shows_no_marker_and_no_error(smoke) -> None:
    assert smoke["dotWithoutDirection"] is True
    assert smoke["hiddenOutsideTheMap"] is True
    assert smoke["hiddenWithNoFix"] is True and smoke["timerStopsWithNoFix"] is True
    assert smoke["noErrorShown"] is True
    assert smoke["refusedQuietly"] is True and smoke["refusedOnce"] is True
    assert smoke["retriedAfterLeaving"] is True


def test_a_full_re_render_keeps_the_marker(smoke) -> None:
    assert smoke["survivesARender"] is True


def test_the_subscription_lives_only_while_the_now_tab_shows(smoke) -> None:
    assert smoke["unsubscribedOnLeaving"] is True
    assert smoke["timersOnAnotherTab"] == 0 and smoke["fixForgotten"] is True
    assert smoke["resubscribed"] is True
    assert smoke["unsubscribedOnDisconnect"] is True
    assert smoke["otherMowerForgotten"] is True
    assert smoke["lateSubscribeClosed"] is True  # left the tab while subscribing
