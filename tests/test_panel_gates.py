"""Gate editor helpers in the Zones & Gates tab (issue #29, spec §4.7, §16.3), run through node."""

from __future__ import annotations

import pytest

from tests.test_panel_zones import call, zone  # noqa: F401  (also applies the node skip)

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="node missing"
)

CHANNELS = [
    {"id": "c1", "end_a": "dock", "end_b": "z1"},
    {"id": "c2", "end_a": "z1", "end_b": "z2"},
]
ZONES = [zone(), zone(id="z2", name="Back")]


def gate(**kw):
    base = {"id": "g1", "name": "Fence gate", "channels": ["c1"], "state_entity": None,
            "state_entity_id": None, "open_action": None, "close_action": None,
            "notify": True, "open": False, "zone_ids": ["z1", "z2"]}
    base.update(kw)
    return base


# -- map -----------------------------------------------------------------------


def test_map_tags_channels_for_picking_and_highlights_the_picked_ones() -> None:
    svg = call("renderMap", ZONES, CHANNELS, {"pickedChannelIds": ["c2"]})
    assert svg.count('data-channel="c1"') >= 1 and svg.count('data-channel="c2"') >= 1
    picked = [s for s in svg.split("<line ") if 'data-channel="c2"' in s]
    assert all("picked" in s for s in picked)
    unpicked = [s for s in svg.split("<line ") if 'data-channel="c1"' in s]
    assert all("picked" not in s for s in unpicked)


def test_map_draws_each_gate_with_its_believed_state() -> None:
    gates = [gate(), gate(id="g2", name="Side", channels=["c2"], open=True)]
    svg = call("renderMap", ZONES, CHANNELS, {"gates": gates})
    assert svg.count('class="gate closed"') == 1
    assert svg.count('class="gate open"') == 1
    assert "Fence gate" in svg and "closed" in svg


def test_map_without_gates_draws_none() -> None:
    assert 'class="gate' not in call("renderMap", ZONES, CHANNELS, {})


# -- list and gated Zones --------------------------------------------------------


def test_gate_list_shows_state_and_derived_gated_zones_for_confirmation() -> None:
    view = {"zones": ZONES, "channels": CHANNELS, "gates": [gate(open=True)],
            "gated_zones": [{"id": "z1", "name": "Front", "gates": ["g1"]},
                            {"id": "z2", "name": "Back", "gates": ["g1"]}],
            "unrouted_zones": []}
    html = call("gateListHtml", view, "g1")
    assert "Fence gate" in html and "open" in html
    assert "Zones behind this Gate" in html
    assert "Front" in html and "Back" in html
    assert 'data-act="edit-gate"' in html and 'data-act="new-gate"' in html


def test_gate_list_flags_zones_with_no_route_to_the_dock() -> None:
    view = {"zones": ZONES, "channels": CHANNELS, "gates": [], "gated_zones": [],
            "unrouted_zones": [{"id": "z2", "name": "Back"}]}
    html = call("gateListHtml", view, None)
    assert "no route to the dock" in html and "Back" in html
    assert "not gated" in html


def test_gate_list_without_gates_has_no_unrouted_warning_when_all_routed() -> None:
    view = {"zones": ZONES, "channels": CHANNELS, "gates": [], "gated_zones": [],
            "unrouted_zones": []}
    html = call("gateListHtml", view, None)
    assert "no route" not in html


# -- editor ----------------------------------------------------------------------


def draft(**kw):
    base = {"id": None, "name": "", "channels": [], "state_entity_id": "",
            "open_action": "", "close_action": "", "notify": None}
    base.update(kw)
    return base


def test_editor_has_every_field() -> None:
    html = call("gateEditorHtml", draft(name="Fence gate", channels=["c1", "c2"]),
                {"zones": ZONES, "channels": CHANNELS}, ["binary_sensor.gate", "cover.g"])
    for needle in ('id="gate-name"', 'id="gate-state-entity"', 'id="gate-open-action"',
                   'id="gate-close-action"', 'id="gate-notify"', 'data-act="save-gate"'):
        assert needle in html
    assert 'value="Fence gate"' in html
    assert "binary_sensor.gate" in html  # entity suggestions
    assert "Dock" in html and "Front" in html and "Back" in html  # channels named by their ends
    assert "Pick the Channels on the map" in html


def test_editor_notify_defaults_on_without_an_open_action_and_off_with_one() -> None:
    zv = {"zones": ZONES, "channels": CHANNELS}
    no_action = call("gateEditorHtml", draft(), zv, [])
    assert 'id="gate-notify" checked' in no_action
    with_action = call("gateEditorHtml", draft(open_action='[{"action":"script.open"}]'), zv, [])
    assert 'id="gate-notify" checked' not in with_action
    explicit = call("gateEditorHtml", draft(open_action='[{"action":"x"}]', notify=True), zv, [])
    assert 'id="gate-notify" checked' in explicit


def test_editor_offers_delete_only_for_an_existing_gate() -> None:
    zv = {"zones": ZONES, "channels": CHANNELS}
    assert 'data-act="delete-gate"' not in call("gateEditorHtml", draft(), zv, [])
    assert 'data-act="delete-gate"' in call("gateEditorHtml", draft(id="g1"), zv, [])


def test_draft_from_gate_round_trips_actions_as_json_text() -> None:
    d = call("gateDraft", gate(open_action=[{"action": "script.open"}], state_entity_id="cover.g"))
    assert d["id"] == "g1" and d["state_entity_id"] == "cover.g"
    assert d["open_action"] == '[{"action":"script.open"}]'
    assert d["close_action"] == ""


def test_payload_needs_a_name_and_channels() -> None:
    assert "name" in call("gatePayload", draft(channels=["c1"]))["error"].lower()
    assert "channel" in call("gatePayload", draft(name="G"))["error"].lower()


def test_payload_parses_actions_and_clears_blank_fields() -> None:
    p = call("gatePayload", draft(name=" G ", channels=["c1"], state_entity_id="cover.g",
                                  open_action='[{"action": "script.open"}]'))["payload"]
    assert p == {"name": "G", "channels": ["c1"], "state_entity": "cover.g",
                 "open_action": [{"action": "script.open"}], "close_action": None,
                 "notify": None}
    p = call("gatePayload", draft(name="G", channels=["c1"], notify=False))["payload"]
    assert p["state_entity"] is None and p["notify"] is False


def test_payload_accepts_a_single_action_object_and_rejects_bad_json() -> None:
    p = call("gatePayload", draft(name="G", channels=["c1"],
                                  close_action='{"action": "script.close"}'))["payload"]
    assert p["close_action"] == [{"action": "script.close"}]
    err = call("gatePayload", draft(name="G", channels=["c1"], open_action="{nope"))["error"]
    assert "open action" in err.lower()


def test_toggle_channel_picks_and_unpicks() -> None:
    assert call("toggleChannel", ["c1"], "c2") == ["c1", "c2"]
    assert call("toggleChannel", ["c1", "c2"], "c1") == ["c2"]
