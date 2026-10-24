"""Checks tab helpers (issue #38, spec §16.3, §9, §11.4), run through node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components.groundskeeper.checks import default_check_records
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


CUSTOM = {"id": "c1", "type": "custom", "name": "Sprinklers off", "enabled": True,
          "config": {"mode": "entity", "entity": "switch.sprinklers", "operator": "is", "value": "off"},
          "timeout_s": None, "policy": "notify"}


def view(rain=True, custom=()):
    return {"checks": default_check_records() + list(custom),
            "anomaly_actions": {"charge_not_rising": "notify", "run_too_long": "pause"},
            "mower_rain_detection": rain}


def rec(check_id):
    return next(r for r in default_check_records() if r["id"] == check_id)


# -- rendering -----------------------------------------------------------------


def test_tab_lists_every_check_with_its_settings() -> None:
    html = call("checksTabHtml", view(custom=[CUSTOM]), ["switch.sprinklers"])
    for name in ("Mower ready", "Zone ready", "Battery", "Work hours", "Wet grass", "Gate open",
                 "Sprinklers off"):
        assert name in html
    assert 'data-check="battery"' in html and 'value="80"' in html  # threshold
    for day in ("mon", "sun"):
        assert f'data-day="{day}"' in html
    start = rec("work_hours")["config"]["days"]["mon"][0]
    assert f'data-f="start" value="{start}"' in html
    assert 'value="12"' in html  # wet grass timeout, hours
    assert 'value="3"' in html  # dry-out hours


def test_always_on_checks_cannot_be_edited() -> None:
    html = call("checkCardHtml", rec("mower_ready"), view(), [])
    assert "data-act=\"save-check\"" not in html and "Always on" in html


def test_mower_rain_flag_reads_off_on_the_mower_when_detection_is_off() -> None:
    assert "off on the Mower" in call("checksTabHtml", view(rain=False), [])
    assert "off on the Mower" not in call("checksTabHtml", view(rain=True), [])


def test_anomaly_detectors_show_their_action() -> None:
    html = call("checksTabHtml", view(), [])
    assert 'data-detector="run_too_long"' in html
    pause = html.split('data-detector="run_too_long"')[1].split("</select>")[0]
    assert '<option value="pause" selected>' in pause
    for action in ("off", "notify", "pause"):
        assert f'value="{action}"' in pause


# -- saving --------------------------------------------------------------------


def test_battery_changes_carry_threshold_timeout_and_policy() -> None:
    got = call("checkChanges", rec("battery"),
               {"enabled": True, "threshold_pct": "70", "timeout_h": "2", "policy": "pause"})
    assert got["changes"] == {"enabled": True, "timeout_s": 7200, "policy": "pause",
                              "config": {"threshold_pct": 70}}
    got = call("checkChanges", rec("battery"), {"enabled": True, "threshold_pct": "x", "timeout_h": "",
                                                 "policy": "notify"})
    assert got["error"]


def test_blank_timeout_means_none() -> None:
    got = call("checkChanges", rec("battery"),
               {"enabled": False, "threshold_pct": "80", "timeout_h": "", "policy": "notify"})
    assert got["changes"]["timeout_s"] is None


def test_work_hours_changes_per_weekday() -> None:
    days = {d: {"start": "08:00", "end": "00:00"} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    days["sat"] = {"start": "10:00", "end": "18:30"}
    got = call("checkChanges", rec("work_hours"),
               {"enabled": True, "timeout_h": "", "policy": "notify", "days": days})
    assert got["changes"]["config"]["days"]["sat"] == ["10:00", "18:30"]
    assert got["changes"]["config"]["days"]["mon"] == ["08:00", "00:00"]


def test_wet_grass_changes_in_seconds_and_lists() -> None:
    got = call("checkChanges", rec("wet_grass"), {
        "enabled": True, "timeout_h": "12", "policy": "notify", "mower_rain": False,
        "weather": ["weather.home"], "sensors": ["binary_sensor.rain"],
        "dry_out_h": "4", "morning": True, "morning_offset_h": "1.5", "precip_pct": "60"})
    assert got["changes"]["config"] == {
        "mower_rain": False, "weather": ["weather.home"], "sensors": ["binary_sensor.rain"],
        "dry_out_s": 14400, "morning": True, "morning_offset_s": 5400, "precip_pct": 60}


# -- Custom Checks -------------------------------------------------------------


def test_entity_condition_payload() -> None:
    got = call("customCheckPayload", {"name": " Sprinklers off ", "mode": "entity",
               "entity": "switch.sprinklers", "operator": "is", "value": "off",
               "template": "", "timeout_h": "1", "policy": "notify"})
    assert got["payload"] == {"name": "Sprinklers off", "timeout_s": 3600, "policy": "notify",
                              "config": {"mode": "entity", "entity": "switch.sprinklers",
                                         "operator": "is", "value": "off"}}


def test_template_payload_and_missing_fields() -> None:
    got = call("customCheckPayload", {"name": "T", "mode": "template", "template": "{{ true }}",
               "timeout_h": "", "policy": "notify"})
    assert got["payload"]["config"] == {"mode": "template", "template": "{{ true }}"}
    assert got["payload"]["timeout_s"] is None
    base = {"name": "T", "mode": "entity", "entity": "x.y", "operator": "is", "value": "on",
            "template": "", "timeout_h": "", "policy": "notify"}
    for bad in ({"name": ""}, {"entity": ""}, {"value": ""}, {"mode": "template"}):
        assert call("customCheckPayload", {**base, **bad})["error"]
