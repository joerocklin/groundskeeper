"""What the two Run edit surfaces send and show when Save is pressed (#111),
driven through a stub DOM in `panel_run_edit_smoke.js`."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components.groundskeeper.panel import FRONTEND_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")

SCRIPT = Path(__file__).with_name("panel_run_edit_smoke.js")
SRC = FRONTEND_DIR / "groundskeeper-panel.js"
REJECTION = "spacing 5 cm is outside the device limits (20-60 cm)"


@pytest.fixture(scope="module")
def out() -> dict:
    res = subprocess.run(
        ["node", str(SCRIPT), str(SRC)], check=True, capture_output=True, text=True
    )
    return json.loads(res.stdout)


# -- the Schedule tab's Plan cards -----------------------------------------------------


def test_saving_a_plan_card_run_sends_only_the_changed_spacing_for_that_run(out) -> None:
    assert out["schedule_changed"] == [{
        "type": "groundskeeper/edit_run", "entry_id": "e", "run_id": "r2",
        "height_mm": 70, "angle_deg": 10, "settings": {"spacing_cm": 22},
    }]


def test_saving_without_touching_spacing_or_speed_sends_no_settings(out) -> None:
    (msg,) = out["schedule_untouched"]
    assert msg["run_id"] == "r2" and "settings" not in msg


def test_a_refused_plan_card_edit_shows_the_backends_message_and_reverts_the_inputs(out) -> None:
    got = out["schedule_rejected"]
    assert got["sent"] == 1 and got["notice"] == REJECTION and got["shown"] is True
    assert got["reloaded"] is True and got["spacingAfter"] == "30"


def test_a_blank_plan_card_value_is_refused_in_the_panel_and_nothing_is_sent(out) -> None:
    got = out["schedule_blank"]
    assert got == {"sent": 0, "notice": "Enter a speed above zero.", "shown": True}


# -- the Now tab's Run sheet --------------------------------------------------------------


def test_saving_the_run_sheet_sends_the_changed_values_in_settings_and_closes_it(out) -> None:
    got = out["sheet_changed"]
    assert got["closed"] is True
    assert got["sent"] == [{
        "type": "groundskeeper/edit_run", "entry_id": "e", "run_id": "r1",
        "height_mm": 70, "angle_deg": 40, "settings": {"spacing_cm": 22, "speed_m_s": 0.3},
    }]


def test_a_refused_sheet_edit_shows_the_backends_message_and_keeps_what_was_typed(out) -> None:
    got = out["sheet_rejected"]
    assert got["sent"] == 1 and got["error"] == REJECTION and got["shown"] is True
    assert got["typedKept"] == "5"


def test_a_blank_sheet_value_is_refused_in_the_panel_and_nothing_is_sent(out) -> None:
    assert out["sheet_blank"] == {"sent": 0, "error": "Enter a spacing above zero."}


def test_a_re_render_while_typing_keeps_the_new_inputs(out) -> None:
    assert out["rerender"] == {"spacing": "41", "speed": "0.77"}
