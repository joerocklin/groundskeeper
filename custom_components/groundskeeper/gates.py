"""Gate records and their pure rules (spec §4.7, §10).

A Gate is a physical barrier the Mower cannot open. It is user-declared per
Mower, stored in `config.gates`:

    {"id", "name", "channels", "state_entity", "open_action", "close_action",
     "notify", "positions"}

`positions` maps a Channel id to the fraction (0..1, from `end_a`) at which the
Gate is drawn along it; a missing entry means half-way. Display-only.

`derive_gating` works out which Zones lie behind which Gates from the Channel
graph; `believed_open` is the believed-state rule. Reading HA entities and
running actions is the runtime's job; the reducer only sees the results.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DOCK = "dock"

# States of a contact sensor, cover or lock that mean "open" (spec §4.7).
OPEN_STATES = frozenset({"on", "open", "opening", "unlocked", "unlocking"})


def normalise_gate(rec: Mapping[str, Any]) -> dict[str, Any]:
    """A stored Gate with every field present.

    "Notify me before crossings" defaults on when there is no open action.
    """
    channels = list(rec.get("channels") or [])
    gate = {
        "id": rec["id"],
        "name": rec.get("name", ""),
        "channels": channels,
        "state_entity": rec.get("state_entity") or None,
        "open_action": rec.get("open_action") or None,
        "close_action": rec.get("close_action") or None,
        "notify": rec.get("notify"),
        "positions": {
            c: f for c, f in (rec.get("positions") or {}).items() if c in channels
        },
    }
    if gate["notify"] is None:
        gate["notify"] = gate["open_action"] is None
    return gate


def validate_gate(gate: Mapping[str, Any], channel_ids: Iterable[str]) -> None:
    """Raise `ValueError` for a Gate that cannot be saved."""
    if not str(gate.get("name", "")).strip():
        raise ValueError("A Gate needs a name")
    known = set(channel_ids)
    channels = gate.get("channels") or []
    if not channels or any(c not in known for c in channels):
        raise ValueError("A Gate sits on one or more existing Channels")
    for channel, fraction in (gate.get("positions") or {}).items():
        if (
            isinstance(fraction, bool)
            or not isinstance(fraction, (int, float))
            or not 0 <= fraction <= 1
        ):
            raise ValueError(f"The Gate position on {channel} is a fraction from 0 to 1")
    for key in ("open_action", "close_action"):
        action = gate.get(key)
        if action is not None and not (
            isinstance(action, list) and all(isinstance(a, dict) for a in action)
        ):
            raise ValueError(f"The {key.replace('_', ' ')} is a list of actions")


def believed_open(*, entity_state: str | None, has_entity: bool, flag: bool) -> bool:
    """Linked entity first, else the manual flag, else closed (spec §4.7).

    A linked entity that is missing, `unavailable` or `unknown` reads closed:
    being wrong costs a Prompt, never an open gate.
    """
    if has_entity:
        return entity_state in OPEN_STATES
    return bool(flag)


@dataclass(frozen=True)
class Gating:
    """Zone id -> Gate ids it lies behind; Zones with no route to the dock."""

    gated: Mapping[str, tuple[str, ...]]
    unrouted: tuple[str, ...]


def _reachable(channels: Iterable[Mapping[str, Any]]) -> set[str]:
    adjacency: dict[str, set[str]] = {}
    for ch in channels:
        adjacency.setdefault(ch["end_a"], set()).add(ch["end_b"])
        adjacency.setdefault(ch["end_b"], set()).add(ch["end_a"])
    seen = {DOCK}
    todo = [DOCK]
    while todo:
        for nxt in adjacency.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def derive_gating(
    zones: Iterable[Mapping[str, Any]],
    channels: Iterable[Mapping[str, Any]],
    gates: Iterable[Mapping[str, Any]],
) -> Gating:
    """Which Zones lie behind which Gates (spec §4.7).

    A Zone is behind a Gate when every Channel route from the dock crosses one
    of the Gate's Channels, i.e. cutting those Channels disconnects it. A Zone
    no Channel route reaches is flagged and treated as not gated. Missing
    Zones are skipped.
    """
    channels = list(channels)
    live = [z["id"] for z in zones if not z.get("missing")]
    reach_all = _reachable(channels)
    unrouted = tuple(z for z in live if z not in reach_all)
    gated: dict[str, list[str]] = {}
    for gate in gates:
        own = set(gate.get("channels") or [])
        if not own:
            continue
        reach_open = _reachable(c for c in channels if c["id"] not in own)
        for zone in live:
            if zone in reach_all and zone not in reach_open:
                gated.setdefault(zone, []).append(gate["id"])
    return Gating({z: tuple(g) for z, g in gated.items()}, unrouted)
