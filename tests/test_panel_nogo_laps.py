"""No-go laps on the New Plan form and the Plan cards (#109), run through node.

The panel never names an adapter's own key: the descriptor it is handed (the
Zones view's `nogo_laps`) carries the key, the range and the label.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components.groundskeeper.panel import FRONTEND_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")

HARNESS = Path(__file__).with_name("panel_harness.js")
SRC = FRONTEND_DIR / "groundskeeper-panel.js"

NOGO = {"key": "laps_key", "label": "Laps", "min": 0, "max": 4}
ZONES = [
    {"id": "with", "name": "With", "nogo_areas": [{}, {}], "nogo_laps_default": 1},
    {"id": "none", "name": "None", "nogo_areas": [], "nogo_laps_default": 0},
    {"id": "unknown", "name": "Unknown", "nogo_areas": None, "nogo_laps_default": 0},
]
DRAFT = {"zone": "with", "start": "3", "end": "2", "max_cut": "", "base_angle": "",
         "finishing": "default", "nogo_laps": ""}


def call(fn: str, *args):
    expr = f"{fn}(" + ",".join(json.dumps(a) for a in args) + ")"
    out = subprocess.run(
        ["node", str(HARNESS), str(SRC), expr], check=True, capture_output=True, text=True
    )
    return json.loads(out.stdout)


def form(zone_id: str, **draft) -> str:
    return call("newPlanFormHtml", {**DRAFT, "zone": zone_id, **draft}, ZONES, None, "km", NOGO)


# -- what the form offers -------------------------------------------------------


def test_a_zone_with_no_go_areas_offers_the_input_with_default_and_range() -> None:
    html = form("with")
    assert 'id="np-nogo-laps"' in html
    assert 'placeholder="1"' in html  # the resolved default for this Zone
    assert "0 to 4" in html  # allowed range, from the descriptor


def test_the_form_hints_that_the_value_applies_to_the_finishing_run_too() -> None:
    html = form("with")
    assert "Finishing Run" in html and "every Run" in html


def test_a_zone_without_no_go_areas_offers_no_input_and_says_it_would_do_nothing() -> None:
    html = form("none")
    assert 'id="np-nogo-laps"' not in html
    assert 'id="np-nogo-none"' in html and "no effect" in html


def test_a_zone_whose_no_go_areas_are_unknown_offers_no_input_and_says_not_known_yet() -> None:
    html = form("unknown")
    assert 'id="np-nogo-laps"' not in html
    assert 'id="np-nogo-unknown"' in html and "not known yet" in html
    assert 'id="np-nogo-none"' not in html  # unknown is not the same as none


def test_no_zone_picked_offers_nothing() -> None:
    html = form("")
    assert "np-nogo" not in html


def test_an_adapter_without_the_setting_offers_nothing() -> None:
    html = call("newPlanFormHtml", DRAFT, ZONES, None, "km", None)
    assert "np-nogo" not in html


def test_an_entered_value_is_shown_back() -> None:
    assert 'value="3"' in form("with", nogo_laps="3")


# -- the payload ----------------------------------------------------------------


def test_a_blank_input_sends_no_settings_at_all() -> None:
    got = call("newPlanPayload", DRAFT, "km", ZONES, NOGO)["payload"]
    assert "settings" not in got


def test_a_value_goes_in_one_settings_override_under_the_adapters_key() -> None:
    got = call("newPlanPayload", {**DRAFT, "nogo_laps": "2"}, "km", ZONES, NOGO)["payload"]
    assert got["settings"] == {"extras": {"laps_key": 2}}


def test_zero_is_a_value_not_a_blank() -> None:
    got = call("newPlanPayload", {**DRAFT, "nogo_laps": "0"}, "km", ZONES, NOGO)["payload"]
    assert got["settings"] == {"extras": {"laps_key": 0}}


@pytest.mark.parametrize("zone", ["none", "unknown"])
def test_a_stale_value_is_never_sent_for_a_zone_that_does_not_offer_the_input(zone) -> None:
    got = call("newPlanPayload", {**DRAFT, "zone": zone, "nogo_laps": "2"}, "km", ZONES, NOGO)["payload"]
    assert "settings" not in got


def test_an_out_of_range_value_is_still_sent_so_the_backend_decides() -> None:
    got = call("newPlanPayload", {**DRAFT, "nogo_laps": "5"}, "km", ZONES, NOGO)["payload"]
    assert got["settings"] == {"extras": {"laps_key": 5}}


def test_the_payload_without_the_extra_arguments_is_unchanged() -> None:
    got = call("newPlanPayload", DRAFT, "km")["payload"]
    assert got == {"zone": "with", "start_height_mm": 3, "end_height_mm": 2}


def test_settings_from_several_sources_merge_into_one_object() -> None:
    merged = call("mergeSettings", {"spacing_cm": 30, "extras": {"a": 1}}, {"extras": {"b": 2}})
    assert merged == {"spacing_cm": 30, "extras": {"a": 1, "b": 2}}


# -- changing Zone ----------------------------------------------------------------


def test_picking_another_zone_discards_the_value_entered_for_the_previous_one() -> None:
    prev = {**DRAFT, "nogo_laps": "3"}
    got = call("readPlanDraft", prev, {**DRAFT, "zone": "none", "nogo_laps": "3"})
    assert got["zone"] == "none" and got["nogo_laps"] == ""


def test_the_value_stays_while_the_zone_does_not_change() -> None:
    got = call("readPlanDraft", {**DRAFT, "nogo_laps": "1"}, {**DRAFT, "nogo_laps": "3"})
    assert got["nogo_laps"] == "3"


# -- Plan cards -------------------------------------------------------------------


def run(rid, laps, status="pending"):
    return {"id": rid, "plan_id": "p", "index": 1, "of": 2, "status": status,
            "height_mm": 70, "angle_deg": 10, "zone_name": "Front",
            "settings": {"extras": {"laps_key": laps}}}


def cards(*runs, key="laps_key"):
    view = {"plans": [{"id": "p", "zone_name": "Front", "status": "active",
                       "start_height_mm": 70, "end_height_mm": 60}],
            "runs": list(runs), "nogo_laps_key": key}
    return call("planCardsHtml", view, "km")


def test_a_plan_card_shows_each_runs_no_go_laps_when_above_zero() -> None:
    html = cards(run("r1", 2), run("r2", 3, status="mowing"))
    assert html.count("No-go laps: 2") == 1 and html.count("No-go laps: 3") == 1


def test_a_plan_card_hides_no_go_laps_at_zero() -> None:
    assert "No-go laps" not in cards(run("r1", 0))


def test_a_plan_card_hides_no_go_laps_without_the_setting_or_a_stored_value() -> None:
    assert "No-go laps" not in cards(run("r1", 2), key=None)
    legacy = {**run("r1", 2), "settings": {}}
    assert "No-go laps" not in cards(legacy)
