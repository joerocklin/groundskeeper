"""Pure on-load validation of the Store sections (spec §14.7).

`config` and `state` are all-or-nothing: invalid means the caller must not
trust them. `history` and `learned` are lists of independent records, so a bad
record is dropped and reported while the rest is kept.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from numbers import Real
from typing import Any

import voluptuous as vol

_RECORD = {vol.Required("id"): str}
_ANY = vol.ALLOW_EXTRA

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional("zones"): [vol.Schema(_RECORD, extra=_ANY)],
        vol.Optional("channels"): list,
        vol.Optional("checks"): [vol.Schema(_RECORD, extra=_ANY)],
        vol.Optional("recurrences"): list,
        vol.Optional("anomaly_actions"): dict,
    },
    extra=_ANY,
)

_RUN = vol.Schema({vol.Required("id"): str, vol.Required("status"): str}, extra=_ANY)
STATE_SCHEMA = vol.Schema(
    {
        vol.Optional("runs"): [_RUN],
        vol.Optional("plans"): dict,
        vol.Optional("schedule"): dict,
        vol.Optional("deadlines"): dict,
    },
    extra=_ANY,
)

_HISTORY_RUN = vol.Schema(
    {vol.Required("id"): str, vol.Required("plan_id"): str, vol.Required("status"): str},
    extra=_ANY,
)
_HISTORY_PLAN = vol.Schema(_RECORD, extra=_ANY)


@dataclass
class StoreValidation:
    """Outcome: cleaned copy of the data plus what was wrong."""

    data: dict[str, Any]
    config_ok: bool = True
    state_ok: bool = True
    dropped: list[str] = field(default_factory=list)


def _valid(schema: vol.Schema, value: Any) -> bool:
    try:
        schema(value)
    except vol.Invalid:
        return False
    return True


def _clean_history(history: Any, dropped: list[str]) -> dict[str, Any]:
    if not isinstance(history, dict):
        dropped.append("history: section is not a mapping")
        return {}
    out = dict(history)
    runs = history.get("runs", [])
    if "runs" not in history:
        pass
    elif isinstance(runs, list):
        kept = []
        for rec in runs:
            if _valid(_HISTORY_RUN, rec):
                kept.append(rec)
            else:
                dropped.append(f"history run dropped: {rec!r}")
        out["runs"] = kept
    else:
        dropped.append("history.runs: not a list")
        out["runs"] = []
    plans = history.get("plans", {})
    if "plans" not in history:
        pass
    elif isinstance(plans, dict):
        out["plans"] = {}
        for plan_id, rec in plans.items():
            if _valid(_HISTORY_PLAN, rec):
                out["plans"][plan_id] = rec
            else:
                dropped.append(f"history plan dropped: {plan_id}")
    else:
        dropped.append("history.plans: not a mapping")
        out["plans"] = {}
    return out


def _sample_ok(sample: Any) -> bool:
    value = sample.get("value") if isinstance(sample, dict) else None
    return isinstance(value, Real) and not isinstance(value, bool)


def _clean_learned(learned: Any, dropped: list[str]) -> dict[str, Any]:
    if not isinstance(learned, dict):
        dropped.append("learned: section is not a mapping")
        return {}
    out = dict(learned)
    durations = learned.get("durations")
    if durations is None:
        return out
    if not isinstance(durations, dict):
        dropped.append("learned.durations: not a mapping")
        out["durations"] = {}
        return out
    out["durations"] = {}
    for zone_id, samples in durations.items():
        if not isinstance(samples, list):
            dropped.append(f"learned durations for {zone_id} dropped")
            continue
        kept = []
        for sample in samples:
            if _sample_ok(sample):
                kept.append(sample)
            else:
                dropped.append(f"learned sample dropped for {zone_id}: {sample!r}")
        out["durations"][zone_id] = kept
    return out


def validate_store(raw: Any) -> StoreValidation:
    """Validate a loaded Store payload; never mutates `raw`."""
    if not isinstance(raw, dict):
        return StoreValidation(data={}, config_ok=False, state_ok=False)
    data = copy.deepcopy(raw)
    result = StoreValidation(data=data)
    config = data.get("config", {})
    result.config_ok = isinstance(config, dict) and _valid(CONFIG_SCHEMA, config)
    state = data.get("state") or {}
    result.state_ok = isinstance(state, dict) and _valid(STATE_SCHEMA, state)
    if not result.state_ok:
        data["state"] = {}
    if "history" in data:
        data["history"] = _clean_history(data["history"], result.dropped)
    if "learned" in data:
        data["learned"] = _clean_learned(data["learned"], result.dropped)
    return result
