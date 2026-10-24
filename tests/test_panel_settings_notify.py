"""Settings tab: notify targets and categories (issue #39, spec §16.3, §12.3, §4.2), run through node."""

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


CATEGORIES = {
    "run_failed": True, "run_on_hold": True, "check_waits": True, "is_raining": True,
    "run_started": False, "run_done": False, "schedule_completed": True,
}
SETTINGS = {"targets": ["mobile_app_pixel_8"], "categories": CATEGORIES}


def test_available_targets_are_the_mobile_app_notify_services() -> None:
    services = {"notify": {"mobile_app_pixel_8": {}, "persistent_notification": {},
                           "mobile_app_ipad": {}, "notify": {}, "send_message": {}}}
    assert call("notifyTargetOptions", services) == ["mobile_app_ipad", "mobile_app_pixel_8"]
    assert call("notifyTargetOptions", {}) == []
    assert call("notifyTargetOptions", None) == []


def test_picker_lists_existing_services_checked_when_stored() -> None:
    html = call("notifySettingsHtml", SETTINGS, ["mobile_app_ipad", "mobile_app_pixel_8"])
    assert 'data-target="mobile_app_pixel_8" checked' in html
    assert 'data-target="mobile_app_ipad"' in html
    assert 'data-target="mobile_app_ipad" checked' not in html
    assert "No phone, panel only" not in html  # phones exist: no empty-state hint


def test_stored_target_that_no_longer_exists_is_kept_and_flagged() -> None:
    html = call("notifySettingsHtml", {"targets": ["mobile_app_old"], "categories": CATEGORIES},
                ["mobile_app_pixel_8"])
    assert 'data-target="mobile_app_old" checked' in html
    assert "no longer exists" in html


def test_no_services_says_panel_only() -> None:
    html = call("notifySettingsHtml", {"targets": [], "categories": CATEGORIES}, [])
    assert "panel only" in html.lower()


def test_failure_and_on_hold_are_shown_always_on_and_disabled() -> None:
    html = call("notifySettingsHtml", SETTINGS, [])
    for cat in ("run_failed", "run_on_hold"):
        assert f'data-category="{cat}" checked disabled' in html


def test_toggleable_categories_reflect_state() -> None:
    html = call("notifySettingsHtml", SETTINGS, [])
    assert 'data-category="check_waits" checked>' in html
    assert 'data-category="run_started">' in html  # off: not checked
    assert 'data-category="schedule_completed" checked>' in html
    assert 'data-category="is_raining" checked>' in html


def test_gate_and_anomaly_rows_point_elsewhere_without_toggles() -> None:
    html = call("notifySettingsHtml", SETTINGS, [])
    assert "Notify me before crossings" in html
    assert 'data-category="gate' not in html and 'data-category="anomaly' not in html


def test_payload_is_selected_targets_only() -> None:
    assert call("notifyTargetsPayload", ["mobile_app_a", "mobile_app_b"]) == {
        "targets": ["mobile_app_a", "mobile_app_b"]}
    assert call("notifyTargetsPayload", []) == {"targets": []}


def test_settings_tab_has_reserved_sections_for_later_tickets() -> None:
    html = call("settingsTabHtml", "<p>notify</p>")
    assert 'id="settings-notifications"' in html
    assert 'id="settings-tunables"' in html
    assert 'id="settings-storage"' in html
    assert html.index("settings-notifications") < html.index("settings-tunables") < html.index("settings-storage")
