"""Now tab helpers (issue #36, spec §16.2), run through node."""

from __future__ import annotations

import pytest

from tests.test_panel_zones import call, zone  # noqa: F401  (also applies the node skip)

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="node missing"
)


def run(**kw):
    base = {"run_id": "r1", "plan_id": "p1", "zone_id": "z1", "zone_name": "Front",
            "height_mm": 70, "angle_deg": 123.5, "n": 2, "of": 3,
            "can_up": True, "can_down": True}
    base.update(kw)
    return base


def view(**kw):
    base = {"status": "idle", "pause_requested": False, "paused_reason": None, "battery": None,
            "current_run": None, "current_zone_id": None, "next_run": None, "up_next": [],
            "waiting_on": [], "failure": None, "checks": [], "controls": []}
    base.update(kw)
    return base


def current(**kw):
    base = run(status="mowing", battery=64, height_check="verified")
    base.update(kw)
    return base


# -- heights in HA units ----------------------------------------------------------


def test_heights_follow_the_unit_system() -> None:
    assert call("fmtHeight", 70, "km") == "70 mm"
    assert call("fmtHeight", 70, "mi") == "2.76 in"


# -- map ----------------------------------------------------------------------------


def test_now_map_options_stripe_the_next_run_zone_at_its_absolute_angle() -> None:
    v = view(next_run=run(), up_next=[run()], current_zone_id="z2")
    opts = call("nowMapOpts", v, None)
    assert opts["stripeZoneId"] == "z1" and opts["angle"] == 123.5
    assert opts["currentZoneId"] == "z2" and opts["highlightZoneId"] is None


def test_now_map_stripes_the_active_run_while_mowing_and_highlights_a_tapped_run() -> None:
    v = view(current_run=current(zone_id="z2", angle_deg=10), up_next=[run()])
    opts = call("nowMapOpts", v, "r1")
    assert opts["stripeZoneId"] == "z2" and opts["angle"] == 10
    assert opts["highlightZoneId"] == "z1"


# -- status card -------------------------------------------------------------------------


def test_status_card_shows_the_current_run_and_running_controls() -> None:
    v = view(status="running", current_run=current(),
             controls=["pause", "stop_now", "done_today"])
    html = call("statusCardHtml", v, "km")
    for text in ("Front", "70 mm", "123.5°", "Run 2 of 3", "64 %", "verified"):
        assert text in html
    for ctl in ("pause", "stop_now", "done_today"):
        assert f'data-ctl="{ctl}"' in html
    assert "Pause after Run" in html and "Stop now" in html and "Done for today" in html
    assert 'data-ctl="retry"' not in html


def test_status_card_notes_a_requested_pause() -> None:
    html = call("statusCardHtml", view(status="running", current_run=current(),
                                       pause_requested=True, controls=["stop_now"]), "km")
    assert "Pausing after this Run" in html


def test_waiting_status_lists_failing_checks_with_readable_reasons() -> None:
    v = view(status="waiting", next_run=run(), up_next=[run()], controls=["pause"],
             waiting_on=[{"check": "mower_ready", "name": "Mower ready", "reason": "battery_low"},
                         {"check": "work_hours", "name": "Work hours", "reason": "outside_work_hours"}])
    html = call("statusCardHtml", v, "km")
    assert "Waiting" in html
    assert "Mower ready" in html and "battery too low" in html.lower()
    assert "Work hours" in html and "outside work hours" in html.lower()


def test_failed_run_shows_retry_skip_plan_and_abort() -> None:
    v = view(status="paused", paused_reason="start_failed",
             failure={**run(), "reason": "start_failed"},
             controls=["retry", "skip_plan", "abort"])
    html = call("statusCardHtml", v, "km")
    for label in ("Retry", "Skip Plan", "Abort"):
        assert label in html
    assert 'data-ctl="abort"' in html and 'data-ctl="resume"' not in html
    assert "did not start" in html


def test_paused_status_shows_resume() -> None:
    html = call("statusCardHtml", view(status="paused", controls=["resume"]), "km")
    assert 'data-ctl="resume"' in html and "Resume" in html


def test_on_hold_run_offers_resume_run_and_end_run() -> None:
    v = view(status="running", current_run=current(status="on_hold", on_hold_reason="external_pause"),
             controls=["stop_now", "resume_run", "end_run"])
    html = call("statusCardHtml", v, "km")
    assert 'data-ctl="resume_run"' in html and 'data-ctl="end_run"' in html


def test_abort_must_be_confirmed() -> None:
    assert "Abort" in call("confirmText", "abort")
    assert call("confirmText", "pause") is None


# -- up next -----------------------------------------------------------------------------


def test_up_next_rows_have_enabled_move_buttons_only_where_allowed() -> None:
    rows = [run(run_id="a", can_up=False, can_down=False), run(run_id="b", can_up=False, can_down=True)]
    html = call("upNextHtml", view(up_next=rows), "km", "b")
    assert html.count('data-act="move-up"') == 2 and html.count('data-act="move-down"') == 2
    a_row = html.split('data-run="b"')[0]
    assert 'disabled' in a_row
    assert 'data-act="run-menu"' in html
    assert 'data-act="add-plan"' in html and "+ Plan" in html
    assert "selected" in html.split('data-run="b"')[1].split(">")[0] or 'data-run="b" class="run selected"' in html


def test_move_position_is_the_neighbouring_index_among_pending_runs() -> None:
    rows = [run(run_id="a"), run(run_id="b"), run(run_id="c")]
    assert call("movePosition", rows, "b", "up") == 0
    assert call("movePosition", rows, "b", "down") == 2


# -- edit sheet ------------------------------------------------------------------------------


def test_run_sheet_offers_edit_and_delete_in_display_units() -> None:
    html = call("runSheetHtml", run(), "mi", None)
    assert 'value="2.76"' in html and 'value="123.5"' in html
    assert 'data-act="save-run"' in html and 'data-act="delete-run"' in html
    assert 'data-act="close-sheet"' in html


def test_run_sheet_shows_warnings_and_errors() -> None:
    html = call("runSheetHtml", run(), "km", {"warnings": ["Crossing under 45°"], "error": "Too low"})
    assert "Crossing under 45°" in html and "Too low" in html


def test_run_edit_payload_converts_back_to_si_and_validates() -> None:
    assert call("runEditPayload", {"height": "2.76", "angle": "10"}, "mi") == {
        "payload": {"height_mm": 70.1, "angle_deg": 10}}
    assert call("runEditPayload", {"height": "70", "angle": "10"}, "km") == {
        "payload": {"height_mm": 70, "angle_deg": 10}}
    assert "error" in call("runEditPayload", {"height": "", "angle": "10"}, "km")
    assert "error" in call("runEditPayload", {"height": "70", "angle": "x"}, "km")


# -- checks and prompts ---------------------------------------------------------------------


def test_one_pill_per_enabled_check_with_its_state() -> None:
    checks = [{"id": "mower_ready", "name": "Mower ready", "state": "ok"},
              {"id": "work_hours", "name": "Work hours", "state": "failing"},
              {"id": "gate_open", "name": "Gate open", "state": "idle"}]
    html = call("checkPillsHtml", checks)
    assert html.count('class="pill ') == 3
    assert 'pill failing' in html.split("Work hours")[0].rsplit("<span", 1)[1]
    assert "Gate open" in html


def test_no_pills_message_when_no_checks_enabled() -> None:
    assert "No Checks enabled" in call("checkPillsHtml", [])


def test_prompt_cards_show_title_body_and_every_action() -> None:
    prompts = [{"prompt_id": "pp1", "kind": "run_failed", "title": "Example Mower: Run failed",
                "body": "Zone: Front\nRun 1 of 1 at 70 mm", "actions": ["retry", "skip_plan", "abort"]}]
    html = call("promptCardsHtml", prompts)
    assert "Example Mower: Run failed" in html and "Zone: Front" in html and "Run 1 of 1" in html
    for act, label in (("retry", "Retry"), ("skip_plan", "Skip Plan"), ("abort", "Abort")):
        assert f'data-prompt="pp1" data-action="{act}"' in html and label in html


def test_no_prompt_cards_when_none_pending() -> None:
    assert call("promptCardsHtml", []) == ""


def test_panel_renders_the_now_tab_from_a_view() -> None:
    import subprocess
    from pathlib import Path

    from tests.test_panel_zones import SRC

    out = subprocess.run(
        ["node", str(Path(__file__).with_name("panel_now_smoke.js")), str(SRC)],
        check=True, capture_output=True, text=True).stdout
    assert '<svg class="map"' in out and 'class="stripe"' in out  # stripes at the next Run's angle
    assert "Waiting for:" in out and "Battery too low" in out
    assert "Example Mower: Run failed" in out and "Up next" in out and "Checks" in out
    assert 'role="dialog"' in out  # edit sheet open
    assert 'aria-selected="true">Now' in out
