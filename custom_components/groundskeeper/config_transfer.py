"""Pure export, import planning and reference scanning (spec §14.8, §4.2).

Nothing here touches HA: callers pass resolvers in. Picked entities are stored
as registry ids and exported as entity_ids; notify targets are service names;
templates and actions are never read or rewritten.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .gates import prune_positions

EXPORT_FORMAT = "groundskeeper_export"
EXPORT_VERSION = 1

_WET_GRASS = "wet_grass"
_CUSTOM = "custom"


class ImportError_(ValueError):
    """The file is not a usable export."""


@dataclass(frozen=True)
class Reference:
    """One picked entity or notify target and where it is used."""

    kind: str  # "entity" or "notify"
    ref: str
    where: str


@dataclass
class ImportPlan:
    """The new `config` and what the import changed or could not resolve."""

    config: dict[str, Any]
    report: dict[str, Any]


def _entity_slots(config: dict[str, Any]):
    """Yield (container, key_or_index, where) for every stored entity reference."""
    for check in config.get("checks", []):
        cfg = check.get("config") or {}
        if check.get("type") == _WET_GRASS:
            for key, label in (("weather", "Wet grass weather entity"), ("sensors", "Wet grass sensor")):
                refs = cfg.get(key)
                if isinstance(refs, list):
                    for i in range(len(refs)):
                        yield refs, i, label
        elif check.get("type") == _CUSTOM and cfg.get("mode") == "entity" and cfg.get("entity"):
            yield cfg, "entity", f'Custom Check "{check.get("name", check.get("id"))}"'
    for gate in config.get("gates", []):
        if gate.get("state_entity"):
            yield gate, "state_entity", f'Gate "{gate.get("name", gate.get("id"))}" state entity'


def _rewrite(config: dict[str, Any], fn: Callable[[str, str], str]) -> None:
    for container, key, where in _entity_slots(config):
        container[key] = fn(container[key], where)


def find_references(config: dict[str, Any]) -> list[Reference]:
    """Every entity and notify reference in `config`."""
    refs = [Reference("entity", c[k], where) for c, k, where in _entity_slots(config)]
    refs += [Reference("notify", t, "Notify target") for t in config.get("notify_targets", [])]
    return refs


def broken_references(
    config: dict[str, Any],
    *,
    entity_exists: Callable[[str], bool],
    notify_exists: Callable[[str], bool],
) -> list[Reference]:
    """References that do not resolve right now."""
    return [
        r
        for r in find_references(config)
        if not (entity_exists if r.kind == "entity" else notify_exists)(r.ref)
    ]


def build_export(
    config: dict[str, Any],
    history: dict[str, Any],
    *,
    entity_id_of: Callable[[str], str | None],
    include_history: bool,
) -> dict[str, Any]:
    """The export file: `config` (entity_ids), optionally `history`; never state."""
    out_config = copy.deepcopy(config)
    _rewrite(out_config, lambda ref, _where: entity_id_of(ref) or ref)
    out: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "config": out_config,
    }
    if include_history:
        out["history"] = copy.deepcopy(history)
    return out


def check_export(export: Any) -> dict[str, Any]:
    """The export's config, or `ImportError_` when the file is unusable."""
    if not isinstance(export, dict) or export.get("format") != EXPORT_FORMAT:
        raise ImportError_("not_an_export")
    version = export.get("version")
    if not isinstance(version, int) or version > EXPORT_VERSION:
        raise ImportError_("export_too_new")
    if not isinstance(export.get("config"), dict):
        raise ImportError_("not_an_export")
    return export["config"]


def plan_import(
    export: Any,
    *,
    target_zones: list[dict[str, Any]] | None,
    registry_id_of: Callable[[str], str | None],
) -> ImportPlan:
    """Build the replacement `config` for the target Mower.

    `target_zones` None (safe idle: the Mower's Zones are unknown) keeps the
    file's Zone records for the next Zone sync to reconcile. Otherwise Zone
    records match the target's Zones by native ref, then name; unmatched file
    Zones (and Channels ending at them) are dropped and reported, and target
    Zones absent from the file stay as they are.
    """
    config = copy.deepcopy(check_export(export))
    report: dict[str, Any] = {"dropped_zones": [], "unresolved_entities": [], "unresolved_notify": []}

    if target_zones is not None:
        by_ref = {z["native_ref"]: z for z in target_zones}
        claimed: set[str] = set()
        mapping: dict[str, str] = {}
        zones: list[dict[str, Any]] = []
        file_zones = config.get("zones", [])
        matches: dict[int, dict[str, Any]] = {}
        for i, z in enumerate(file_zones):
            t = by_ref.get(z.get("native_ref"))
            if t is not None and t["id"] not in claimed:
                matches[i] = t
                claimed.add(t["id"])
        by_name = {z["name"]: z for z in target_zones if z["id"] not in claimed}
        for i, z in enumerate(file_zones):
            if i not in matches and (t := by_name.get(z.get("name"))) is not None:
                matches[i] = t
                by_name = {k: v for k, v in by_name.items() if v is not t}
        for i, z in enumerate(file_zones):
            t = matches.get(i)
            if t is None:
                report["dropped_zones"].append(z.get("name", z.get("id")))
                continue
            mapping[z["id"]] = t["id"]
            merged = {**z, "id": t["id"], "native_ref": t["native_ref"], "name": t["name"],
                      "missing": t.get("missing", False)}
            if "polygon" in t:
                merged["polygon"] = t["polygon"]
            zones.append(merged)
        zones += [z for z in target_zones if z["id"] not in mapping.values()]
        config["zones"] = zones
        channels = []
        for ch in config.get("channels", []):
            ends = [mapping.get(ch.get(k), ch.get(k)) if ch.get(k) != "dock" else "dock"
                    for k in ("end_a", "end_b")]
            if all(e == "dock" or e in mapping.values() for e in ends):
                channels.append({**ch, "end_a": ends[0], "end_b": ends[1]})
        config["channels"] = channels
        kept = {c["id"] for c in channels}
        for gate in config.get("gates", []):
            gate["channels"] = [c for c in gate.get("channels", []) if c in kept]
            if "positions" in gate:
                gate["positions"] = prune_positions(gate["positions"], kept, valid_only=True)

    def resolve(ref: str, where: str) -> str:
        registry_id = registry_id_of(ref)
        if registry_id is not None:
            return registry_id
        report["unresolved_entities"].append({"ref": ref, "where": where})
        return ref

    _rewrite(config, resolve)
    return ImportPlan(config=config, report=report)
