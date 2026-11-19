"""Spacing and speed on the New Plan form, the preview and the Plan cards (#108),
run through node. Values are SI on the wire and in HA units on screen; the
Python `to_display` / `from_display` define the conversion the panel follows."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components.groundskeeper.heights import from_display, to_display
from custom_components.groundskeeper.panel import FRONTEND_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")

HARNESS = Path(__file__).with_name("panel_harness.js")
SRC = FRONTEND_DIR / "groundskeeper-panel.js"

LIMITS = {
    "spacing_cm": {"min": 20, "max": 60, "default": 25},
    "speed_m_s": {"min": 0.3, "max": 1.2, "default": 0.4},
}
ZONES = [
    {"id": "a", "name": "A", "spacing_cm_default": 25, "speed_m_s_default": 0.4},
    {"id": "b", "name": "B", "spacing_cm_default": 30, "speed_m_s_default": 0.5},
]
DRAFT = {"zone": "a", "start": "3", "end": "2", "max_cut": "", "base_angle": "",
         "finishing": "default", "spacing": "", "speed": ""}
IMPERIAL = "mi"
US = "us_customary"


def call(fn: str, *args):
    expr = f"{fn}(" + ",".join(json.dumps(a) for a in args) + ")"
    out = subprocess.run(
        ["node", str(HARNESS), str(SRC), expr], check=True, capture_output=True, text=True
    )
    return json.loads(out.stdout)


def form(unit="km", limits=LIMITS, **draft) -> str:
    return call("newPlanFormHtml", {**DRAFT, **draft}, ZONES, None, unit, None, limits)


def payload(unit="km", **draft):
    return call("newPlanPayload", {**DRAFT, **draft}, unit, ZONES, None)["payload"]


# -- the form ---------------------------------------------------------------------


def test_the_form_offers_optional_spacing_and_speed_inputs_in_metric() -> None:
    html = form()
    assert 'id="np-spacing"' in html and 'id="np-speed"' in html
    assert "Spacing (cm" in html and "Speed (m/s" in html


def test_the_form_asks_in_inches_and_feet_per_second_when_imperial() -> None:
    html = form(IMPERIAL)
    assert "Spacing (in" in html and "Speed (ft/s" in html
    assert "(cm" not in html and "(m/s" not in html


def test_the_placeholder_is_the_picked_zones_resolved_default() -> None:
    html = form(zone="b")
    assert 'id="np-spacing"' in html and 'placeholder="30"' in html
    assert 'placeholder="0.5"' in html


def test_the_placeholder_is_the_mower_default_while_no_zone_is_picked() -> None:
    html = form(zone="")
    assert 'placeholder="25"' in html and 'placeholder="0.4"' in html


def test_the_placeholder_is_shown_in_the_users_units_when_imperial() -> None:
    html = form(IMPERIAL)
    assert f'placeholder="{to_display("spacing", 25, US):g}"' in html
    assert f'placeholder="{to_display("speed", 0.4, US):g}"' in html


def test_the_device_limits_are_the_hint_in_metric() -> None:
    html = form()
    assert "20 to 60 cm" in html and "0.3 to 1.2 m/s" in html


def test_the_device_limits_are_the_hint_in_the_users_units_when_imperial() -> None:
    html = form(IMPERIAL)
    assert f'{to_display("spacing", 20, US):g} to {to_display("spacing", 60, US):g} in' in html
    assert f'{to_display("speed", 0.3, US):g} to {to_display("speed", 1.2, US):g} ft/s' in html


def test_the_hint_says_the_value_applies_to_the_finishing_run_too() -> None:
    html = form()
    assert "every Run" in html and "including the Finishing Run" in html


def test_an_entered_value_is_shown_back() -> None:
    html = form(spacing="31", speed="0.55")
    assert 'value="31"' in html and 'value="0.55"' in html


def test_the_form_still_renders_before_the_limits_are_known() -> None:
    html = form(limits=None)
    assert 'id="np-spacing"' in html and 'id="np-speed"' in html


def test_a_rejected_value_shows_the_backends_message_inline() -> None:
    msg = "speed 9 m/s is outside the device limits (0.3-1.2 m/s)"
    html = form(error=msg)
    assert 'id="np-error"' in html and msg in html


# -- the payload ------------------------------------------------------------------


def test_blank_inputs_send_no_settings_at_all() -> None:
    assert "settings" not in payload()
    # A draft that never had the fields (as before this feature) is the same.
    bare = {k: v for k, v in DRAFT.items() if k not in ("spacing", "speed")}
    assert "settings" not in call("newPlanPayload", bare, "km", ZONES, None)["payload"]


def test_both_values_go_in_one_settings_override_in_si_when_metric() -> None:
    assert payload(spacing="30", speed="0.5")["settings"] == {"spacing_cm": 30, "speed_m_s": 0.5}


def test_only_the_filled_field_is_sent() -> None:
    assert payload(spacing="30")["settings"] == {"spacing_cm": 30}
    assert payload(speed="0.5")["settings"] == {"speed_m_s": 0.5}


def test_imperial_values_are_converted_to_cm_and_m_per_s() -> None:
    got = payload(IMPERIAL, spacing="10", speed="1.5")["settings"]
    assert got["spacing_cm"] == pytest.approx(from_display("spacing", 10, US), abs=0.05)
    assert got["speed_m_s"] == pytest.approx(from_display("speed", 1.5, US), abs=0.005)


def test_typing_a_displayed_limit_back_in_lands_on_the_limit_and_is_not_rejected() -> None:
    """The hint shows the limit rounded to two decimals; typing it must not
    come out a hair over the limit and be refused."""
    for kind, si in (("spacing", 35.0), ("spacing", 20.0), ("speed", 0.6), ("speed", 0.2)):
        shown = to_display(kind, si, US)
        key = "spacing_cm" if kind == "spacing" else "speed_m_s"
        field = "spacing" if kind == "spacing" else "speed"
        got = payload(IMPERIAL, **{field: str(shown)})["settings"][key]
        assert got == si, (kind, shown, got)


def test_zero_is_a_value_not_a_blank_and_the_backend_decides() -> None:
    assert payload(speed="0")["settings"] == {"speed_m_s": 0}


@pytest.mark.parametrize("field", ["spacing", "speed"])
def test_text_that_is_not_a_number_is_an_error_and_sends_nothing(field) -> None:
    got = call("newPlanPayload", {**DRAFT, field: "fast"}, "km", ZONES, None)
    assert "payload" not in got and field.capitalize() in got["error"]


def test_spacing_and_speed_merge_with_no_go_laps_in_one_override() -> None:
    nogo = {"key": "laps_key", "label": "Laps", "min": 0, "max": 4}
    zones = [{**ZONES[0], "nogo_areas": [{}], "nogo_laps_default": 1}]
    draft = {**DRAFT, "spacing": "30", "nogo_laps": "2"}
    got = call("newPlanPayload", draft, "km", zones, nogo)["payload"]
    assert got["settings"] == {"spacing_cm": 30, "extras": {"laps_key": 2}}


# -- the preview ------------------------------------------------------------------

PREVIEW = {
    "provisional": False,
    "warnings": [],
    "runs": [
        {"n": 1, "m": 2, "height_mm": 70, "angle_deg": 12.5, "estimated_time_s": 3600,
         "provisional": False, "spacing_cm": 25, "speed_m_s": 0.4},
        {"n": 2, "m": 2, "height_mm": 60, "angle_deg": -3, "estimated_time_s": 5400,
         "provisional": False, "spacing_cm": 25, "speed_m_s": 0.3},
    ],
}


def preview_rows(unit="km", pv=PREVIEW) -> list[str]:
    html = call("previewHtml", pv, unit)
    return [row for row in html.split("<tr>")[2:]]


def test_each_preview_row_shows_its_own_spacing_and_speed() -> None:
    first, last = preview_rows()
    assert "25 cm" in first and "0.4 m/s" in first
    assert "25 cm" in last and "0.3 m/s" in last


def test_with_a_slower_finishing_set_only_the_last_row_shows_the_lower_speed() -> None:
    first, last = preview_rows()
    assert "0.3 m/s" not in first and "0.3 m/s" in last


def test_the_preview_shows_spacing_and_speed_in_the_users_units_when_imperial() -> None:
    first, _ = preview_rows(IMPERIAL)
    assert f'{to_display("spacing", 25, US):g} in' in first
    assert f'{to_display("speed", 0.4, US):g} ft/s' in first


def test_the_preview_has_spacing_and_speed_columns() -> None:
    html = call("previewHtml", PREVIEW, "km")
    assert "<th>Spacing</th>" in html and "<th>Speed</th>" in html


def test_a_preview_run_without_spacing_and_speed_still_renders() -> None:
    bare = {**PREVIEW, "runs": [{k: v for k, v in r.items() if k not in ("spacing_cm", "speed_m_s")}
                                for r in PREVIEW["runs"]]}
    html = call("previewHtml", bare, "km")
    assert "70 mm" in html and "1 h" in html


# -- the Plan cards ---------------------------------------------------------------


def run(rid, status, spacing=25, speed=0.4, **extra):
    return {"id": rid, "plan_id": "p", "index": 1, "of": 3, "status": status,
            "height_mm": 70, "angle_deg": 10, "zone_name": "Front",
            "settings": {"spacing_cm": spacing, "speed_m_s": speed, **extra}}


def cards(*runs, unit="km"):
    view = {"plans": [{"id": "p", "zone_name": "Front", "status": "active",
                       "start_height_mm": 70, "end_height_mm": 60}], "runs": list(runs)}
    return call("planCardsHtml", view, unit)


def test_a_plan_card_shows_each_runs_spacing_and_speed_for_every_status() -> None:
    html = cards(run("r1", "done", 25, 0.4), run("r2", "mowing", 30, 0.5), run("r3", "pending", 35, 0.3))
    for text in ("25 cm", "0.4 m/s", "30 cm", "0.5 m/s", "35 cm", "0.3 m/s"):
        assert html.count(text) == 1, text


def test_a_plan_card_shows_them_in_the_users_units_when_imperial() -> None:
    html = cards(run("r1", "pending", 25, 0.4), unit=IMPERIAL)
    assert f'{to_display("spacing", 25, US):g} in' in html
    assert f'{to_display("speed", 0.4, US):g} ft/s' in html


def test_a_run_without_stored_settings_shows_neither() -> None:
    legacy = {**run("r1", "pending"), "settings": {}}
    html = cards(legacy)
    assert " cm" not in html and "m/s" not in html
