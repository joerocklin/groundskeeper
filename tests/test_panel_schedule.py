"""Schedule & Plans tab helpers (issue #37, spec §16.3, §5.5, §5.7), run through node."""

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


def call(fn: str, *args):
    expr = f"{fn}(" + ",".join(json.dumps(a) for a in args) + ")"
    out = subprocess.run(
        ["node", str(HARNESS), str(SRC), expr], check=True, capture_output=True, text=True
    )
    return json.loads(out.stdout)


def run(rid, plan, n, of, status="pending", h=80, angle=10, **kw):
    return {"id": rid, "plan_id": plan, "index": n, "of": of, "status": status,
            "height_mm": h, "angle_deg": angle, "zone_name": "Front", **kw}


VIEW = {
    "schedule": {"status": "idle"},
    "plans": [
        {"id": "a", "zone_name": "Front", "status": "active", "start_height_mm": 80,
         "end_height_mm": 60, "note": "angles_redrawn_for_range",
         "angle_warnings": ["Run 2 is outside the range"]},
        {"id": "b", "zone_name": "Back", "status": "active", "start_height_mm": 70, "end_height_mm": 60},
    ],
    "runs": [
        run("a1", "a", 1, 2, status="mowing", h=80),
        run("a2", "a", 2, 2, h=70),
        run("b1", "b", 1, 2, h=70),
        run("b2", "b", 2, 2, h=60),
    ],
}


# -- units ---------------------------------------------------------------------


def test_heights_show_in_ha_units_and_convert_back() -> None:
    assert call("fmtHeight", 76, "km") == "76 mm"
    assert call("fmtHeight", 76.2, "mi") == "3 in"
    assert call("heightToMm", 3, "mi") == 76.2
    assert call("heightToMm", 70, "km") == 70


# -- reorder (same constraint as Up next) ---------------------------------------


def test_move_target_skips_nothing_and_refuses_crossing_a_sibling_run() -> None:
    pending = [r for r in VIEW["runs"] if r["status"] == "pending"]  # a2, b1, b2
    assert call("moveTarget", pending, "b1", -1) == 0  # ahead of a2: different Plan
    assert call("moveTarget", pending, "a2", 1) == 1
    assert call("moveTarget", pending, "b2", -1) is None  # b1 is its earlier Run
    assert call("moveTarget", pending, "b1", 1) is None  # b2 is its later Run
    assert call("moveTarget", pending, "a2", -1) is None  # top
    assert call("moveTarget", pending, "b2", 1) is None  # bottom


def test_schedule_table_lists_runs_in_order_with_move_buttons_on_pending_only() -> None:
    html = call("scheduleTableHtml", VIEW, "km")
    assert html.index('data-run="a1"') < html.index('data-run="a2"') < html.index('data-run="b1"')
    assert 'data-act="move-up"' in html and 'data-act="move-down"' in html
    assert 'data-act="move-up" data-run="a1"' not in html  # active Run is frozen
    assert 'data-act="move-down" data-run="a2"' in html
    assert "70 mm" in html and "10°" in html
    # b2 can neither go up (its earlier sibling) nor down (bottom): both disabled
    assert 'data-act="move-up" data-run="b2" disabled' in html


# -- Plan cards -----------------------------------------------------------------


def test_plan_card_has_per_run_edit_delete_and_plan_actions() -> None:
    html = call("planCardsHtml", VIEW, "km")
    assert 'data-act="edit-run" data-run="a2"' in html
    assert 'data-act="delete-run" data-run="a2"' in html
    assert 'data-act="reroll" data-plan="b"' in html
    assert 'data-act="rederive" data-plan="b"' in html
    # Plan "a" has started: no edit/delete on its active Run, and re-roll/re-derive are disabled
    assert 'data-act="edit-run" data-run="a1"' not in html
    assert 'data-act="reroll" data-plan="a" disabled' in html
    assert "Run 2 is outside the range" in html
    assert "redrawn" in html.lower()  # note about angles redrawn for the range


def test_edit_warnings_are_readable() -> None:
    text = " ".join(call("editWarningsText", ["height_increases", "step_above_max_cut", "angle_outside_range"]))
    assert "taller" in text.lower() and "max cut" in text.lower() and "range" in text.lower()


# -- New Plan -------------------------------------------------------------------


def test_new_plan_payload_converts_units_and_omits_blanks() -> None:
    draft = {"zone": "z1", "start": "3", "end": "2", "max_cut": "", "base_angle": "", "finishing": "default"}
    got = call("newPlanPayload", draft, "mi")
    assert got["payload"] == {"zone": "z1", "start_height_mm": 76.2, "end_height_mm": 50.8}
    draft.update(max_cut="10", base_angle="5", finishing="off")
    got = call("newPlanPayload", draft, "km")["payload"]
    assert got["max_cut_pct"] == 10 and got["base_angle"] == 5 and got["finishing"] is False
    assert call("newPlanPayload", {"zone": "", "start": "3", "end": "2"}, "km")["error"]
    assert call("newPlanPayload", {"zone": "z", "start": "x", "end": "2"}, "km")["error"]


def test_preview_shows_height_angle_time_and_warnings() -> None:
    preview = {
        "provisional": True,
        "runs": [
            {"n": 1, "m": 2, "height_mm": 70, "angle_deg": 12.5, "estimated_time_s": 5400, "provisional": True},
            {"n": 2, "m": 2, "height_mm": 60, "angle_deg": -3, "estimated_time_s": None, "provisional": True},
        ],
        "warnings": ["Minimum separation relaxed to 8°", "Angles are provisional: the Zone's range"],
    }
    html = call("previewHtml", preview, "km")
    assert "70 mm" in html and "12.5°" in html and "1 h 30 min" in html
    assert "relaxed to 8°" in html
    assert 'class="warn" id="provisional-warning"' in html
    assert "provisional" not in call("previewHtml", {**preview, "provisional": False,
        "warnings": [], "runs": [{**preview["runs"][0], "provisional": False}]}, "km").lower()
