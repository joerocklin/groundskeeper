"""The setup checklist banner above every tab (issue #42, spec §4.3, §16.3), run through node."""

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


def call(fn: str, *args):
    expr = f"{fn}(" + ",".join(json.dumps(a) for a in args) + ")"
    out = subprocess.run(
        ["node", str(HARNESS), str(SRC), expr], check=True, capture_output=True, text=True
    )
    return json.loads(out.stdout)


def checklist(done: tuple[str, ...] = (), recording: bool | None = None) -> dict:
    """The snapshot's checklist for a Mower called Luba, with `done` steps ticked.

    `recording` is whether its debug recording is on (the snapshot always says).
    """
    steps = [
        ("zones", "Zones imported", "zones"),
        ("bearings", "Review Zone bearings", "zones"),
        ("gates", "Does Luba pass through any gates?", "zones"),
        ("notify", "Where should Luba reach you?", "settings"),
        ("recording", "Review debug recording", None),
    ]
    items = [{"id": i, "label": l, "tab": t, "done": i in done} for i, l, t in steps]
    if recording is not None:
        next(i for i in items if i["id"] == "recording")["enabled"] = recording
    return {"items": items, "complete": len(done) == len(steps)}


def test_the_banner_lists_every_step_in_order() -> None:
    html = call("setupBannerHtml", checklist())
    assert 'id="setup-checklist"' in html
    labels = ["Zones imported", "Review Zone bearings", "Does Luba pass through any gates?",
              "Where should Luba reach you?", "Review debug recording"]
    positions = [html.index(label) for label in labels]
    assert positions == sorted(positions)


def test_done_steps_are_marked_and_open_ones_are_not() -> None:
    html = call("setupBannerHtml", checklist(("zones",)))
    assert html.count('class="done"') == 1
    assert html.index('class="done"') < html.index("Zones imported") < html.index("Review Zone bearings")


def test_the_banner_is_gone_once_every_step_is_done() -> None:
    every = ("zones", "bearings", "gates", "notify", "recording")
    assert call("setupBannerHtml", checklist(every)) == ""
    assert call("setupBannerHtml", None) == ""  # no snapshot yet


def test_open_steps_link_to_the_tab_where_they_are_done() -> None:
    html = call("setupBannerHtml", checklist())
    assert html.count('data-act="open-tab" data-tab="zones"') == 3
    assert html.count('data-act="open-tab" data-tab="settings"') == 1
    # The debug recording step is done inside the banner, so it has no tab to open.
    recording = html[html.index("Review debug recording"):]
    assert "open-tab" not in recording

    later = call("setupBannerHtml", checklist(("zones", "bearings")))
    assert later.count('data-act="open-tab" data-tab="zones"') == 1  # only Gates is left there


def test_open_steps_offer_the_control_that_completes_them() -> None:
    html = call("setupBannerHtml", checklist())
    for control in (
        'data-act="setup-step" data-step="bearings"',
        'data-act="setup-step" data-step="gates" data-answer="yes"',
        'data-act="setup-step" data-step="gates" data-answer="no"',
        'data-act="setup-step" data-step="notify"',
        'data-act="setup-step" data-step="recording"',
    ):
        assert control in html
    assert "No phone, panel only" in html
    assert 'data-step="zones"' not in html  # Zones imported completes itself


def test_a_finished_step_offers_no_control() -> None:
    html = call("setupBannerHtml", checklist(("zones", "bearings", "gates", "notify")))
    assert html.count('data-act="setup-step"') == 1  # only the debug recording step is left


def test_the_recording_step_says_what_is_recorded() -> None:
    html = call("setupBannerHtml", checklist(recording=True))
    recording = html[html.index("Review debug recording"):]
    assert "GPS position" in recording
    assert "on by default" not in recording  # not true of an entry from before the feature


def _recording_row(html: str) -> str:
    return html[html.index("Review debug recording"):]


def test_the_recording_step_says_whether_recording_is_on_and_offers_the_switch() -> None:
    on = _recording_row(call("setupBannerHtml", checklist(recording=True)))
    assert "Debug recording is on" in on
    assert 'data-act="recording-toggle" data-enabled="false"' in on
    assert "Turn off" in on

    off = _recording_row(call("setupBannerHtml", checklist(recording=False)))
    assert "Debug recording is off" in off
    assert 'data-act="recording-toggle" data-enabled="true"' in off
    assert "Turn on" in off


def test_the_switch_does_not_stand_in_for_done() -> None:
    row = _recording_row(call("setupBannerHtml", checklist(recording=False)))
    assert 'data-act="setup-step" data-step="recording"' in row  # Done is still how it completes


def test_the_switch_stays_after_done_while_other_steps_are_open() -> None:
    html = call("setupBannerHtml", checklist(("recording",), recording=True))
    row = _recording_row(html)
    assert 'data-act="recording-toggle" data-enabled="false"' in row
    assert 'data-act="setup-step" data-step="recording"' not in row  # Done is done


def test_no_switch_without_the_state_to_show() -> None:
    assert "recording-toggle" not in call("setupBannerHtml", checklist())


SMOKE = Path(__file__).with_name("panel_setup_smoke.js")


@pytest.fixture(scope="module")
def clicks() -> dict:
    """What the banner's buttons did against a stub DOM and a recording connection."""
    res = subprocess.run(
        ["node", str(SMOKE), str(SRC)], check=True, capture_output=True, text=True
    )
    return json.loads(res.stdout)


def test_the_banner_shows_on_every_tab(clicks) -> None:
    assert clicks["banner_on_any_tab"] is True


def test_a_link_opens_its_tab(clicks) -> None:
    assert clicks["tab_after_link"] == "settings"


def test_a_control_sends_its_step_and_answer_for_the_current_mower(clicks) -> None:
    assert clicks["sent"] == [
        {"type": "groundskeeper/set_setup_step", "entry_id": "e", "step": "bearings"},
        {"type": "groundskeeper/set_setup_step", "entry_id": "e", "step": "gates", "answer": "no"},
    ]


def test_a_refused_step_shows_why(clicks) -> None:
    assert clicks["error_shown"] is True


def test_the_switch_sends_the_new_choice_for_the_current_mower(clicks) -> None:
    assert clicks["recording_sent"] == [
        {"type": "groundskeeper/set_debug_recording", "entry_id": "e", "enabled": False},
    ]


def test_a_refused_switch_shows_why(clicks) -> None:
    assert clicks["recording_error_shown"] is True


def test_a_yes_to_gates_says_what_to_do_next() -> None:
    open_checklist = checklist()
    next(i for i in open_checklist["items"] if i["id"] == "gates")["answer"] = "yes"
    gates = call("setupBannerHtml", open_checklist)
    gates = gates[gates.index("pass through any gates"):gates.index("Where should")]
    assert "Add a Gate" in gates
    assert 'data-answer="yes"' not in gates  # already said
    assert 'data-answer="no"' in gates  # but they can change their mind

    unanswered = call("setupBannerHtml", checklist())
    assert "Add a Gate" not in unanswered


def test_a_refusal_does_not_follow_the_owner_to_another_tab_or_mower(clicks) -> None:
    assert clicks["error_after_tab_change"] is False
    assert clicks["banner_after_mower_switch"] is True  # the check below means something
    assert clicks["error_after_mower_switch"] is False
