"""Gate records, believed state and derived Gated Zones (spec §4.7)."""

import pytest

from custom_components.groundskeeper.gates import (
    believed_open,
    derive_gating,
    is_fraction,
    normalise_gate,
    prune_positions,
    validate_gate,
)

# The ExampleSite Channel graph: the dock opens on the Backyard; the Fence gate sits on
# Path 1 (Backyard - Front yard) and Path 5 (Backyard - Behind the fence).
CHANNELS = [
    {"id": "path0", "end_a": "dock", "end_b": "backyard"},
    {"id": "path1", "end_a": "backyard", "end_b": "front"},
    {"id": "path2", "end_a": "front", "end_b": "side"},
    {"id": "path5", "end_a": "backyard", "end_b": "behind"},
]
ZONES = [{"id": z, "missing": False} for z in ("backyard", "front", "side", "behind")]
FENCE = {"id": "g1", "name": "Fence gate", "channels": ["path1", "path5"]}


def test_every_zone_but_the_backyard_is_behind_the_fence_gate() -> None:
    view = derive_gating(ZONES, CHANNELS, [FENCE])
    assert view.gated == {
        "front": ("g1",),
        "side": ("g1",),
        "behind": ("g1",),
    }
    assert view.unrouted == ()


def test_a_bypass_channel_means_not_gated() -> None:
    bypass = [*CHANNELS, {"id": "back_door", "end_a": "dock", "end_b": "front"}]
    view = derive_gating(ZONES, bypass, [FENCE])
    # Front yard (and the Side yard behind it) can be reached without the gate.
    assert view.gated == {"behind": ("g1",)}


def test_a_zone_with_no_route_to_the_dock_is_flagged_and_not_gated() -> None:
    zones = [*ZONES, {"id": "island", "missing": False}]
    view = derive_gating(zones, CHANNELS, [FENCE])
    assert view.unrouted == ("island",)
    assert "island" not in view.gated


def test_two_gates_in_series_gate_the_far_zone_behind_both() -> None:
    inner = {"id": "g2", "name": "Side gate", "channels": ["path2"]}
    view = derive_gating(ZONES, CHANNELS, [FENCE, inner])
    assert view.gated["side"] == ("g1", "g2")
    assert view.gated["front"] == ("g1",)


def test_channels_that_no_longer_exist_are_ignored() -> None:
    ghost = {"id": "g3", "name": "Ghost", "channels": ["gone"]}
    assert derive_gating(ZONES, CHANNELS, [ghost]).gated == {}


def test_missing_zones_are_neither_gated_nor_unrouted() -> None:
    zones = [{"id": "backyard", "missing": False}, {"id": "front", "missing": True}]
    view = derive_gating(zones, CHANNELS, [FENCE])
    assert view.gated == {} and view.unrouted == ()


def test_notify_defaults_on_only_without_an_open_action() -> None:
    assert normalise_gate({"id": "g", "name": "G", "channels": []})["notify"] is True
    with_action = normalise_gate(
        {"id": "g", "name": "G", "channels": [], "open_action": [{"action": "x.y"}]}
    )
    assert with_action["notify"] is False
    explicit = normalise_gate(
        {"id": "g", "name": "G", "channels": [], "open_action": [{"action": "x.y"}],
         "notify": True}
    )
    assert explicit["notify"] is True


def test_believed_state_is_entity_then_flag_then_closed() -> None:
    assert believed_open(entity_state="on", has_entity=True, flag=False) is True
    assert believed_open(entity_state="closed", has_entity=True, flag=True) is False
    assert believed_open(entity_state="unavailable", has_entity=True, flag=True) is False
    assert believed_open(entity_state=None, has_entity=False, flag=True) is True
    assert believed_open(entity_state=None, has_entity=False, flag=False) is False


@pytest.mark.parametrize("state", ["on", "open", "unlocked", "opening"])
def test_open_looking_entity_states_read_open(state) -> None:
    assert believed_open(entity_state=state, has_entity=True, flag=False) is True


def test_validation() -> None:
    ids = {"path1", "path5"}
    validate_gate(normalise_gate(FENCE), ids)
    with pytest.raises(ValueError, match="name"):
        validate_gate(normalise_gate({**FENCE, "name": " "}), ids)
    with pytest.raises(ValueError, match="Channel"):
        validate_gate(normalise_gate({**FENCE, "channels": []}), ids)
    with pytest.raises(ValueError, match="Channel"):
        validate_gate(normalise_gate({**FENCE, "channels": ["nope"]}), ids)
    with pytest.raises(ValueError, match="action"):
        validate_gate(normalise_gate({**FENCE, "open_action": "not a list"}), ids)


def test_logbook_describes_the_gate_events() -> None:
    from custom_components.groundskeeper.logbook import describe

    for kind in ("gate_crossing_expected", "gate_crossing_ended", "gate_open_action_failed"):
        text = describe({"type": kind, "gate_name": "Fence gate", "zone_name": "Front Lawn"})
        assert "Fence gate" in text


# -- placement along a Channel (issue #72) -------------------------------------


def test_positions_default_to_empty_and_drop_removed_channels() -> None:
    assert normalise_gate({"id": "g", "channels": ["a"]})["positions"] == {}
    gate = normalise_gate(
        {"id": "g", "channels": ["a"], "positions": {"a": 0.25, "gone": 0.9}}
    )
    assert gate["positions"] == {"a": 0.25}


def test_validate_rejects_out_of_range_or_non_numeric_positions() -> None:
    for bad in (-0.1, 1.5, "half", True, None):
        gate = normalise_gate({"id": "g", "name": "G", "channels": ["a"], "positions": {"a": bad}})
        with pytest.raises(ValueError, match="position"):
            validate_gate(gate, ["a"])
    ok = normalise_gate({"id": "g", "name": "G", "channels": ["a"], "positions": {"a": 0}})
    validate_gate(ok, ["a"])


def test_is_fraction_accepts_only_real_numbers_from_zero_to_one() -> None:
    assert all(is_fraction(v) for v in (0, 1, 0.5))
    assert not any(is_fraction(v) for v in (-0.1, 1.1, float("nan"), True, "0.5", None))


def test_prune_positions_keeps_listed_channels_and_optionally_only_valid_fractions() -> None:
    positions = {"a": 0.2, "b": 7, "gone": 0.4}
    assert prune_positions(positions, ["a", "b"]) == {"a": 0.2, "b": 7}
    assert prune_positions(positions, ["a", "b"], valid_only=True) == {"a": 0.2}
    assert prune_positions(None, ["a"]) == {}
