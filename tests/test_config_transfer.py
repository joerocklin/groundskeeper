"""Pure export, import planning and reference scanning (spec §14.8, §4.2)."""

import pytest

from custom_components.mowing_director.config_transfer import (
    EXPORT_FORMAT,
    ImportError_,
    broken_references,
    build_export,
    plan_import,
)

REG = {"reg-weather": "weather.home", "reg-gate": "binary_sensor.gate", "reg-x": "sensor.x"}
IDS = {v: k for k, v in REG.items()}


def config():
    return {
        "recurrences": [],
        "zones": [
            {"id": "z1", "native_ref": "n1", "name": "Front", "hidden": True,
             "missing": False, "bearing": {"value": 90, "source": "manual"}},
            {"id": "z2", "native_ref": "n2", "name": "Back", "hidden": False, "missing": False},
        ],
        "channels": [
            {"id": "c1", "native_ref": "cn1", "end_a": "z1", "end_b": "dock"},
            {"id": "c2", "native_ref": "cn2", "end_a": "z1", "end_b": "z2"},
        ],
        "checks": [
            {"id": "wet_grass", "type": "wet_grass", "name": "Wet grass", "enabled": True,
             "config": {"weather": ["reg-weather"], "sensors": ["reg-x", "sensor.unregistered"]}},
            {"id": "k1", "type": "custom", "name": "Sprinklers", "enabled": True,
             "config": {"mode": "entity", "entity": "reg-x", "operator": "is", "value": "off"}},
            {"id": "k2", "type": "custom", "name": "Tpl", "enabled": True,
             "config": {"mode": "template", "template": "{{ states('reg-x') }}"}},
        ],
        "gates": [
            {"id": "g1", "name": "Side gate", "channels": ["c1", "c2"], "state_entity": "reg-gate",
             "open_action": [{"service": "script.open_reg_gate"}]},
        ],
        "notify_targets": ["mobile_app_pixel"],
    }


def to_entity_id(ref):
    return REG.get(ref)


def to_registry_id(entity_id):
    return IDS.get(entity_id)


def test_export_writes_entity_ids_verbatim_templates_and_never_state_or_learned() -> None:
    out = build_export(config(), {"runs": []}, entity_id_of=to_entity_id, include_history=False)
    assert out["format"] == EXPORT_FORMAT
    cfg = out["config"]
    wet = cfg["checks"][0]["config"]
    assert wet["weather"] == ["weather.home"]
    assert wet["sensors"] == ["sensor.x", "sensor.unregistered"]
    assert cfg["checks"][1]["config"]["entity"] == "sensor.x"
    assert cfg["checks"][2]["config"]["template"] == "{{ states('reg-x') }}"
    assert cfg["gates"][0]["state_entity"] == "binary_sensor.gate"
    assert cfg["gates"][0]["open_action"] == [{"service": "script.open_reg_gate"}]
    assert cfg["notify_targets"] == ["mobile_app_pixel"]
    assert set(out) == {"format", "version", "config"}


def test_export_history_is_optional() -> None:
    out = build_export(config(), {"runs": [1]}, entity_id_of=to_entity_id, include_history=True)
    assert out["history"] == {"runs": [1]}


def test_export_does_not_mutate_the_live_config() -> None:
    live = config()
    build_export(live, {}, entity_id_of=to_entity_id, include_history=False)
    assert live == config()


def exported():
    return build_export(config(), {}, entity_id_of=to_entity_id, include_history=False)


TARGET = [
    {"id": "T1", "native_ref": "n1", "name": "Renamed", "missing": False, "polygon": [1]},
    {"id": "T2", "native_ref": "other", "name": "Back", "missing": False, "polygon": [2]},
    {"id": "T3", "native_ref": "n3", "name": "Untouched", "missing": False},
]


def test_zones_match_by_native_ref_then_name_and_remap_ids() -> None:
    plan = plan_import(exported(), target_zones=TARGET, registry_id_of=to_registry_id)
    zones = {z["id"]: z for z in plan.config["zones"]}
    assert zones["T1"]["hidden"] is True and zones["T1"]["bearing"]["value"] == 90
    assert zones["T1"]["name"] == "Renamed" and zones["T1"]["polygon"] == [1]
    assert zones["T2"]["native_ref"] == "other" and zones["T2"]["hidden"] is False
    assert zones["T3"] == TARGET[2]  # not in the file: left as it is
    assert plan.report["dropped_zones"] == []
    ends = {(c["end_a"], c["end_b"]) for c in plan.config["channels"]}
    assert ends == {("T1", "dock"), ("T1", "T2")}
    assert plan.config["gates"][0]["channels"] == ["c1", "c2"]


def test_unmatched_zones_are_dropped_listed_with_their_channels() -> None:
    plan = plan_import(
        exported(), target_zones=TARGET[:1], registry_id_of=to_registry_id
    )
    assert plan.report["dropped_zones"] == ["Back"]
    assert [z["id"] for z in plan.config["zones"]] == ["T1"]
    assert [c["id"] for c in plan.config["channels"]] == ["c1"]
    assert plan.config["gates"][0]["channels"] == ["c1"]


def test_entity_ids_re_resolve_to_registry_ids_and_unresolved_are_kept_and_reported() -> None:
    plan = plan_import(exported(), target_zones=TARGET, registry_id_of=to_registry_id)
    cfg = plan.config
    assert cfg["checks"][0]["config"]["weather"] == ["reg-weather"]
    assert cfg["checks"][0]["config"]["sensors"] == ["reg-x", "sensor.unregistered"]
    assert cfg["gates"][0]["state_entity"] == "reg-gate"
    assert [r["ref"] for r in plan.report["unresolved_entities"]] == ["sensor.unregistered"]
    assert "Wet grass" in plan.report["unresolved_entities"][0]["where"]


def test_templates_and_actions_are_never_rewritten() -> None:
    cfg = plan_import(exported(), target_zones=TARGET, registry_id_of=to_registry_id).config
    assert cfg["checks"][2]["config"]["template"] == "{{ states('reg-x') }}"
    assert cfg["gates"][0]["open_action"] == [{"service": "script.open_reg_gate"}]


@pytest.mark.parametrize("bad", [None, [], {"format": "nope", "config": {}}, {"format": EXPORT_FORMAT}])
def test_not_an_export_is_refused(bad) -> None:
    with pytest.raises(ImportError_):
        plan_import(bad, target_zones=[], registry_id_of=to_registry_id)


def test_newer_export_version_is_refused() -> None:
    bad = {**exported(), "version": 99}
    with pytest.raises(ImportError_):
        plan_import(bad, target_zones=[], registry_id_of=to_registry_id)


def test_broken_references_list_each_with_where_used() -> None:
    cfg = plan_import(exported(), target_zones=TARGET, registry_id_of=to_registry_id).config
    broken = broken_references(
        cfg,
        entity_exists=lambda ref: ref in {"reg-weather", "reg-gate"},
        notify_exists=lambda name: False,
    )
    pairs = {(b.ref, b.where) for b in broken}
    assert ("reg-x", 'Custom Check "Sprinklers"') in pairs
    assert ("reg-x", "Wet grass sensor") in pairs
    assert ("sensor.unregistered", "Wet grass sensor") in pairs
    assert ("mobile_app_pixel", "Notify target") in pairs
    assert not any(b.ref in ("reg-weather", "reg-gate") for b in broken)


def test_nothing_broken_when_everything_resolves() -> None:
    assert broken_references(config(), entity_exists=lambda r: True, notify_exists=lambda n: True) == []
