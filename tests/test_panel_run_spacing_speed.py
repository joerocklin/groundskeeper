"""Editing one pending Run's Spacing and speed from the panel (#111), run through
node. Values are SI on the wire and in HA units on screen; the Python
`to_display` / `from_display` define the conversion the panel follows."""

from __future__ import annotations

import pytest

from custom_components.groundskeeper.heights import from_display, to_display
from tests.test_panel_spacing_speed import call

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="node missing"
)

IMPERIAL = "mi"
US = "us_customary"
STORED = {"spacing_cm": 30, "speed_m_s": 0.5}


def now_run(**kw):
    """A row of the Now view's up_next, as the Run sheet receives it."""
    base = {"run_id": "r1", "plan_id": "p", "zone_id": "z1", "zone_name": "Front",
            "height_mm": 70, "angle_deg": 12, "n": 2, "of": 3, **STORED}
    base.update(kw)
    return base


def sheet(run=None, unit="km", state=None) -> str:
    return call("runSheetHtml", run or now_run(), unit, state)


def edit_payload(d, unit="km", stored=STORED):
    return call("runEditPayload", {"height": "70", "angle": "10", **d}, unit, stored)


# -- the Now tab's Run sheet --------------------------------------------------------------


def test_the_run_sheet_offers_spacing_and_speed_prefilled_in_metric() -> None:
    html = sheet()
    assert 'id="run-spacing"' in html and 'id="run-speed"' in html
    assert 'value="30"' in html and 'value="0.5"' in html
    assert "Spacing (cm" in html and "Speed (m/s" in html


def test_the_run_sheet_asks_in_inches_and_feet_per_second_when_imperial() -> None:
    html = sheet(unit=IMPERIAL)
    assert 'value="11.81"' in html and 'value="1.64"' in html  # 30 cm, 0.5 m/s
    assert f'value="{to_display("spacing", 30, US):g}"' in html  # as Python shows it
    assert f'value="{to_display("speed", 0.5, US):g}"' in html
    assert "Spacing (in" in html and "Speed (ft/s" in html
    assert "(cm" not in html and "(m/s" not in html


def test_what_was_typed_survives_a_re_render_of_the_sheet() -> None:
    draft = {"height": "70", "angle": "12", "spacing": "41", "speed": "0.77"}
    html = sheet(state={"draft": draft})
    assert 'value="41"' in html and 'value="0.77"' in html
    assert 'value="30"' not in html and 'value="0.5"' not in html


def test_a_run_stored_without_spacing_and_speed_gets_no_inputs_for_them() -> None:
    html = sheet(now_run(spacing_cm=None, speed_m_s=None))
    assert "run-spacing" not in html and "run-speed" not in html
    assert 'id="run-height"' in html  # the rest of the sheet is as before


# -- the edit payload ------------------------------------------------------------------------


def test_a_changed_spacing_and_speed_go_in_one_settings_override_in_si() -> None:
    got = edit_payload({"spacing": "22", "speed": "0.3"})
    assert got == {"payload": {"height_mm": 70, "angle_deg": 10,
                               "settings": {"spacing_cm": 22, "speed_m_s": 0.3}}}


def test_only_the_changed_field_is_sent() -> None:
    assert edit_payload({"spacing": "22", "speed": "0.5"})["payload"]["settings"] == {"spacing_cm": 22}
    assert edit_payload({"spacing": "30", "speed": "0.3"})["payload"]["settings"] == {"speed_m_s": 0.3}


def test_unchanged_values_send_no_settings_at_all() -> None:
    got = edit_payload({"spacing": "30", "speed": "0.5"})
    assert got == {"payload": {"height_mm": 70, "angle_deg": 10}}


def test_imperial_values_are_converted_to_cm_and_m_per_s() -> None:
    got = edit_payload({"spacing": "8.66", "speed": "0.98"}, IMPERIAL)["payload"]["settings"]
    assert got == {"spacing_cm": 22.0, "speed_m_s": 0.3}
    assert got["spacing_cm"] == pytest.approx(from_display("spacing", 8.66, US), abs=0.05)
    assert got["speed_m_s"] == pytest.approx(from_display("speed", 0.98, US), abs=0.005)


def test_an_untouched_imperial_value_is_not_resent_so_it_cannot_drift() -> None:
    """27.35 cm shows as 10.77 in, which converts back to 27.4 cm: editing only
    the height must not quietly change the Spacing."""
    stored = {"spacing_cm": 27.35, "speed_m_s": 0.45}
    shown = {"spacing": "10.77", "speed": "1.48"}  # 27.35 / 2.54 in; 0.45 / 0.3048 ft/s
    got = edit_payload({**shown, "height": "2.76"}, IMPERIAL, stored)
    assert got == {"payload": {"height_mm": 70.1, "angle_deg": 10}}


@pytest.mark.parametrize("field", ["spacing", "speed"])
@pytest.mark.parametrize("bad", ["", "  ", "fast", "0", "-1"])
def test_a_blank_or_non_numeric_value_is_an_error_and_sends_nothing(field, bad) -> None:
    got = edit_payload({field: bad})
    assert "payload" not in got
    assert field in got["error"].lower() and "above zero" in got["error"]


def test_a_value_outside_the_limits_is_still_sent_for_the_backend_to_refuse() -> None:
    assert edit_payload({"spacing": "99"})["payload"]["settings"] == {"spacing_cm": 99}


def test_without_spacing_or_speed_inputs_the_payload_is_height_and_angle_only() -> None:
    got = call("runEditPayload", {"height": "70", "angle": "10"}, "km", {})
    assert got == {"payload": {"height_mm": 70, "angle_deg": 10}}


def test_height_and_angle_errors_are_unchanged() -> None:
    assert "error" in edit_payload({"height": ""})
    assert "error" in edit_payload({"angle": "x"})


# -- the Schedule tab's Plan cards -----------------------------------------------------------


def plan_run(rid, status="pending", spacing=25, speed=0.4, index=1, **extra):
    settings = {k: v for k, v in (("spacing_cm", spacing), ("speed_m_s", speed)) if v is not None}
    return {"id": rid, "plan_id": "p", "index": index, "of": 3, "status": status,
            "height_mm": 70, "angle_deg": 10, "zone_name": "Front", "settings": settings, **extra}


def cards(*runs, unit="km") -> str:
    view = {"plans": [{"id": "p", "zone_name": "Front", "status": "active",
                       "start_height_mm": 70, "end_height_mm": 60}], "runs": list(runs)}
    return call("planCardsHtml", view, unit)


def test_a_pending_run_has_spacing_and_speed_inputs_prefilled_from_its_stored_values() -> None:
    html = cards(plan_run("r1", spacing=35, speed=0.3))
    assert 'class="edit-spacing" data-run="r1" value="35"' in html
    assert 'class="edit-speed" data-run="r1" value="0.3"' in html
    assert "Spacing (cm" in html and "Speed (m/s" in html


def test_the_inputs_are_in_inches_and_feet_per_second_when_imperial() -> None:
    html = cards(plan_run("r1", spacing=30, speed=0.5), unit=IMPERIAL)
    assert 'class="edit-spacing" data-run="r1" value="11.81"' in html  # 30 cm
    assert 'class="edit-speed" data-run="r1" value="1.64"' in html  # 0.5 m/s
    assert "Spacing (in" in html and "Speed (ft/s" in html


def test_each_pending_run_carries_its_own_values() -> None:
    html = cards(plan_run("r1", spacing=35, index=1), plan_run("r2", spacing=28, index=2),
                 plan_run("r3", spacing=22, index=3))
    for rid, spacing in (("r1", 35), ("r2", 28), ("r3", 22)):
        assert f'class="edit-spacing" data-run="{rid}" value="{spacing}"' in html


@pytest.mark.parametrize("status", ["done", "mowing", "failed", "skipped", "cancelled", "on_hold"])
def test_a_run_that_is_not_pending_shows_spacing_and_speed_read_only(status) -> None:
    html = cards(plan_run("r1", status, spacing=35, speed=0.3))
    assert "edit-spacing" not in html and "edit-speed" not in html
    assert "35 cm" in html and "0.3 m/s" in html


def test_a_pending_run_stored_without_spacing_and_speed_gets_no_inputs_for_them() -> None:
    html = cards(plan_run("r1", spacing=None, speed=None))
    assert "edit-spacing" not in html and "edit-speed" not in html
    assert 'class="edit-height"' in html  # the rest of the row is as before
