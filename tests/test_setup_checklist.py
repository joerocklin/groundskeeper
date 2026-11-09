"""The per-Mower setup checklist: its steps, order and completion (spec §4.3)."""

from custom_components.groundskeeper.setup_checklist import build_checklist

ZONE = {"id": "backyard", "name": "Backyard"}
GATE = {"id": "g1", "name": "Fence gate", "channels": ["path1"]}


def _done(checklist: dict) -> dict[str, bool]:
    return {item["id"]: item["done"] for item in checklist["items"]}


def test_a_new_mower_starts_with_every_step_open_in_order() -> None:
    checklist = build_checklist({}, "Luba")
    assert [i["id"] for i in checklist["items"]] == [
        "zones", "bearings", "gates", "notify", "recording"
    ]
    assert not any(i["done"] for i in checklist["items"])
    assert checklist["complete"] is False


def test_bearings_are_done_only_when_the_owner_marks_them_reviewed() -> None:
    marked = {"zones": [ZONE], "setup": {"bearings_reviewed": True}}
    assert _done(build_checklist(marked, "Luba"))["bearings"] is True
    assert _done(build_checklist({"zones": [ZONE]}, "Luba"))["bearings"] is False


def test_answering_no_to_gates_completes_that_step() -> None:
    assert _done(build_checklist({"setup": {"gates_answer": "no"}}, "Luba"))["gates"] is True


def test_answering_yes_to_gates_completes_the_step_once_a_gate_exists() -> None:
    yes = {"setup": {"gates_answer": "yes"}}
    assert _done(build_checklist(yes, "Luba"))["gates"] is False
    assert _done(build_checklist({**yes, "gates": [GATE]}, "Luba"))["gates"] is True


def test_notify_is_done_with_a_phone_picked_or_panel_only_chosen() -> None:
    assert _done(build_checklist({}, "Luba"))["notify"] is False
    phone = {"notify_targets": ["mobile_app_pixel"]}
    assert _done(build_checklist(phone, "Luba"))["notify"] is True
    panel_only = {"setup": {"notify_panel_only": True}}
    assert _done(build_checklist(panel_only, "Luba"))["notify"] is True


def test_the_checklist_is_complete_only_when_every_step_is_done() -> None:
    config = {
        "zones": [ZONE],
        "notify_targets": ["mobile_app_pixel"],
        "setup": {"bearings_reviewed": True, "gates_answer": "no"},
    }
    almost = build_checklist(config, "Luba")
    assert _done(almost)["recording"] is False
    assert almost["complete"] is False

    config["setup"]["recording_reviewed"] = True
    assert build_checklist(config, "Luba")["complete"] is True


def test_each_step_is_labelled_for_the_mower_and_links_to_where_it_is_done() -> None:
    items = build_checklist({}, "Luba")["items"]
    assert [(i["label"], i["tab"]) for i in items] == [
        ("Zones imported", "zones"),
        ("Review Zone bearings", "zones"),
        ("Does Luba pass through any gates?", "zones"),
        ("Where should Luba reach you?", "settings"),
        ("Review debug recording", None),  # done inline in the banner
    ]


def test_zones_are_done_once_any_have_been_imported() -> None:
    assert _done(build_checklist({"zones": [ZONE]}, "Luba")) == {
        "zones": True, "bearings": False, "gates": False, "notify": False, "recording": False
    }


def test_the_gates_item_carries_the_owners_answer() -> None:
    def gates(config: dict) -> dict:
        return next(i for i in build_checklist(config, "Luba")["items"] if i["id"] == "gates")

    assert gates({})["answer"] is None
    assert gates({"setup": {"gates_answer": "yes"}})["answer"] == "yes"


def test_the_recording_step_reports_whether_recording_is_on_but_only_a_mark_completes_it() -> None:
    def items(**kw):
        return {i["id"]: i for i in build_checklist({}, "Luba", **kw)["items"]}

    assert items(recording=True)["recording"]["enabled"] is True
    assert items(recording=False)["recording"]["enabled"] is False
    assert items(recording=True)["recording"]["done"] is False
    assert "enabled" not in items(recording=True)["bearings"]
