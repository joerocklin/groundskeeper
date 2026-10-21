"""Run history and retention limits (spec §14.3, §14.6).

Pure and HA-free. Limits live in `config.settings` per Mower; `history` holds
finished Runs (frozen record, copied Zone name, linked Anomaly ids, Prompt
outcomes) and the inputs of Plans that still have a Run there.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

KEY_RUNS = "history_runs_max"
KEY_DURATIONS = "duration_samples_max"
KEY_BATTERY = "battery_samples_max"

# key -> (default, lowest, highest)
LIMITS: dict[str, tuple[int, int, int]] = {
    KEY_RUNS: (500, 0, 2000),
    KEY_DURATIONS: (20, 0, 100),
    KEY_BATTERY: (5, 0, 20),
}


def retention_limits(settings: Mapping[str, Any] | None) -> dict[str, int]:
    """Effective limits: stored value (clamped to its range) or the default."""
    settings = settings or {}
    out: dict[str, int] = {}
    for key, (default, low, high) in LIMITS.items():
        value = settings.get(key)
        out[key] = (
            default
            if isinstance(value, bool) or not isinstance(value, int)
            else max(low, min(high, value))
        )
    return out


def validate_limits(changes: Mapping[str, Any]) -> None:
    """Raise ValueError for an unknown key or a value outside its range."""
    for key, value in changes.items():
        if key not in LIMITS:
            raise ValueError(f"unknown limit: {key}")
        _, low, high = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")


def prune_runs(history: dict[str, Any], keep: int) -> list[dict[str, Any]]:
    """Drop the oldest finished Runs beyond `keep`; a Plan goes with its last
    Run. Mutates `history` and returns the dropped Runs."""
    runs = history.setdefault("runs", [])
    plans = history.setdefault("plans", {})
    cut = max(0, len(runs) - keep)
    dropped = runs[:cut]
    del runs[:cut]
    left = {r["plan_id"] for r in runs}
    for run in dropped:
        if run["plan_id"] not in left:
            plans.pop(run["plan_id"], None)
    return dropped


def trim_samples(samples: list[Any], keep: int) -> list[Any]:
    """Newest `keep` samples."""
    return samples[len(samples) - keep :] if keep else []


def prune_preview(history: Mapping[str, Any], keep: int) -> dict[str, Any]:
    """What lowering the Run limit to `keep` would delete."""
    runs = history.get("runs", [])
    dropped = runs[: max(0, len(runs) - keep)]
    dates = [r.get("finished_at") for r in dropped if r.get("finished_at")]
    return {"count": len(dropped), "oldest": min(dates) if dates else None}


def capability_warnings(
    limits: Mapping[str, int], runs_in_window: int, window_days: int
) -> list[dict[str, str]]:
    """Consequences of the limits (spec §14.6); warned, never blocked."""
    out: list[dict[str, str]] = []
    runs = limits[KEY_RUNS]
    if runs == 0:
        out.append({"code": "history_empty", "message": "History tab will be empty"})
    elif runs < runs_in_window:
        out.append({
            "code": "angle_history_short",
            "message": f"Angle history avoidance will see fewer than {window_days} days of Runs",
        })
    samples = limits[KEY_DURATIONS]
    if samples < 3:
        out.append({
            "code": "run_long_never",
            "message": "Run far longer than history will never fire",
        })
    elif samples < 10:
        out.append({"code": "run_long_noisy", "message": "Run-long detection will be noisier"})
    battery = limits[KEY_BATTERY]
    if battery == 0:
        out.append({
            "code": "recharge_warnings_off",
            "message": "Gate recharge warnings are disabled",
        })
    elif battery < 3:
        out.append({
            "code": "recharge_timing_less_reliable",
            "message": "Gate recharge timing will be less reliable",
        })
    return out


def page(history: Mapping[str, Any], offset: int, limit: int) -> dict[str, Any]:
    """Finished Runs newest first, each with its Plan's inputs."""
    runs = history.get("runs", [])
    plans = history.get("plans", {})
    ordered = sorted(runs, key=lambda r: r.get("finished_at") or "", reverse=True)
    items = [{**r, "plan": plans.get(r["plan_id"])} for r in ordered[offset : offset + limit]]
    return {"runs": items, "total": len(runs), "offset": offset, "limit": limit}


def sample_preview(durations: Mapping[str, list[Any]], keep: int) -> int:
    """How many duration samples (all Zones) lowering the limit would delete."""
    return sum(max(0, len(v) - keep) for v in durations.values())


def trim_durations(durations: dict[str, list[Any]], keep: int) -> None:
    """Apply the per-Zone sample limit in place."""
    for zone_id in list(durations):
        durations[zone_id] = trim_samples(durations[zone_id], keep)
