"""Check records and their pure rules (spec §9).

A Check is a typed record stored per Mower in `config.checks`:

    {"id", "type", "name", "enabled", "config", "timeout_s", "policy"}

Per-Plan overrides and Recurrence attach to these records later. The reducer
(`core.py`) owns the wait-time behaviour (clocks, stability, policies); this
module holds the record shape, validation and the type rules that need no
Mower state: Work hours windows and entity conditions. Anything that needs HA
(entity states, template rendering) is resolved by the runtime and handed to
the reducer as a result per Check.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

TYPE_MOWER_READY = "mower_ready"
TYPE_ZONE_READY = "zone_ready"
TYPE_BATTERY = "battery"
TYPE_WORK_HOURS = "work_hours"
TYPE_CUSTOM = "custom"
TYPE_WET_GRASS = "wet_grass"
TYPES = (
    TYPE_MOWER_READY, TYPE_ZONE_READY, TYPE_BATTERY, TYPE_WORK_HOURS, TYPE_WET_GRASS,
    TYPE_CUSTOM,
)
ALWAYS_ON = (TYPE_MOWER_READY, TYPE_ZONE_READY)  # spec §9.1: cannot be disabled

POLICY_NOTIFY = "notify"
POLICY_PAUSE = "pause"
POLICIES = (POLICY_NOTIFY, POLICY_PAUSE)

MOWER_READY_TIMEOUT_S = 15 * 60  # spec §9.2
ZONE_READY_TIMEOUT_S = 30 * 60
DEFAULT_BATTERY_PCT = 80
WET_TIMEOUT_S = 12 * 3600  # spec §9.2
DEFAULT_DRY_OUT_S = 3 * 3600  # spec §9.3
DEFAULT_MORNING_DRY_OUT = True
DEFAULT_MORNING_OFFSET_S = 2 * 3600  # sunrise + 2 h
DEFAULT_PRECIP_PCT = 50  # spec §9.4
WET_CONDITIONS = ("rainy", "pouring", "lightning-rainy", "snowy", "hail")
DEFAULT_WINDOW = ("08:00", "00:00")  # "00:00" as the end means midnight
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MODE_ENTITY = "entity"
MODE_TEMPLATE = "template"
OPERATORS = ("is", "is_not", "above", "below")

_BAD_STATES = ("unavailable", "unknown")


def default_check_records() -> list[dict[str, Any]]:
    """The built-in Checks with their defaults (spec §9.2)."""
    return [
        {"id": TYPE_MOWER_READY, "type": TYPE_MOWER_READY, "name": "Mower ready",
         "enabled": True, "config": {}, "timeout_s": MOWER_READY_TIMEOUT_S,
         "policy": POLICY_NOTIFY},
        {"id": TYPE_ZONE_READY, "type": TYPE_ZONE_READY, "name": "Zone ready",
         "enabled": True, "config": {}, "timeout_s": ZONE_READY_TIMEOUT_S,
         "policy": POLICY_PAUSE},
        {"id": TYPE_BATTERY, "type": TYPE_BATTERY, "name": "Battery",
         "enabled": False, "config": {"threshold_pct": DEFAULT_BATTERY_PCT},
         "timeout_s": None, "policy": POLICY_NOTIFY},
        {"id": TYPE_WORK_HOURS, "type": TYPE_WORK_HOURS, "name": "Work hours",
         "enabled": True,
         "config": {"days": {day: list(DEFAULT_WINDOW) for day in WEEKDAYS}},
         "timeout_s": None, "policy": POLICY_NOTIFY},
        {"id": TYPE_WET_GRASS, "type": TYPE_WET_GRASS, "name": "Wet grass",
         "enabled": True,
         "config": {"mower_rain": True, "weather": [], "sensors": [],
                    "dry_out_s": DEFAULT_DRY_OUT_S, "morning": DEFAULT_MORNING_DRY_OUT,
                    "morning_offset_s": DEFAULT_MORNING_OFFSET_S,
                    "precip_pct": DEFAULT_PRECIP_PCT},
         "timeout_s": WET_TIMEOUT_S, "policy": POLICY_NOTIFY},
    ]


def normalise_checks(records: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Stored records with every built-in present and always-on rules applied."""
    stored = {r["id"]: dict(r) for r in records or ()}
    out: list[dict[str, Any]] = []
    for default in default_check_records():
        rec = {**default, **stored.pop(default["id"], {})}
        if rec["type"] == TYPE_WET_GRASS:
            rec["config"] = {**default["config"], **rec["config"]}
        if rec["type"] in ALWAYS_ON:
            rec["enabled"] = True
            rec["timeout_s"] = default["timeout_s"]
            rec["policy"] = default["policy"]
        out.append(rec)
    out.extend(stored.values())
    return out


def validate_record(rec: Mapping[str, Any]) -> None:
    """Raise `ValueError` for a record that cannot be saved (custom Checks)."""
    if not str(rec.get("name", "")).strip():
        raise ValueError("A Check needs a name")
    if rec.get("policy") not in POLICIES:
        raise ValueError("Policy must be notify or pause")
    timeout = rec.get("timeout_s")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ValueError("Timeout must be a positive number of seconds or none")
    if rec["type"] == TYPE_BATTERY:
        pct = rec["config"].get("threshold_pct")
        if not isinstance(pct, (int, float)) or not 1 <= pct <= 100:
            raise ValueError("Battery threshold must be 1 to 100 %")
    elif rec["type"] == TYPE_WORK_HOURS:
        for window in rec["config"].get("days", {}).values():
            _parse_window(window)
    elif rec["type"] == TYPE_WET_GRASS:
        cfg = rec["config"]
        for key in ("weather", "sensors"):
            if not isinstance(cfg.get(key, []), list):
                raise ValueError("Weather entities and sensors are lists")
        for key in ("dry_out_s", "morning_offset_s"):
            value = cfg.get(key, 0)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError("Dry-out times must be zero or more seconds")
        pct = cfg.get("precip_pct", DEFAULT_PRECIP_PCT)
        if not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            raise ValueError("Precipitation probability must be 0 to 100 %")
    elif rec["type"] == TYPE_CUSTOM:
        cfg = rec["config"]
        if cfg.get("mode") == MODE_ENTITY:
            if not cfg.get("entity"):
                raise ValueError("Pick an entity")
            if cfg.get("operator") not in OPERATORS:
                raise ValueError("Operator must be is, is_not, above or below")
            if cfg.get("value") in (None, ""):
                raise ValueError("A value is required")
            if cfg["operator"] in ("above", "below"):
                _number(cfg["value"])
        elif cfg.get("mode") == MODE_TEMPLATE:
            if not str(cfg.get("template", "")).strip():
                raise ValueError("A template is required")
        else:
            raise ValueError("Custom Checks are an entity condition or a template")
    else:
        raise ValueError("Only Custom Checks can be created or deleted")


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def _parse_window(window: Any) -> tuple[int, int]:
    try:
        start, end = _minutes(window[0]), _minutes(window[1])
    except (TypeError, ValueError, IndexError, AttributeError) as err:
        raise ValueError("Work hours are start and end as HH:MM") from err
    if end == 0:
        end = 24 * 60  # an end of 00:00 is midnight
    return start, end


def work_window_start(config: Mapping[str, Any], weekday: int) -> int:
    """Minutes after midnight the Work hours window opens on `weekday` (0 = Monday)."""
    window = config.get("days", {}).get(WEEKDAYS[weekday], DEFAULT_WINDOW)
    return _parse_window(window)[0]


def work_hours_open(config: Mapping[str, Any], local_now: datetime) -> bool:
    """Whether `local_now` (already in local time) is inside the day's window."""
    days = config.get("days", {})
    window = days.get(WEEKDAYS[local_now.weekday()], DEFAULT_WINDOW)
    start, end = _parse_window(window)
    return start <= local_now.hour * 60 + local_now.minute < end


def next_work_start(config: Mapping[str, Any], local_now: datetime) -> datetime:
    """The next Work hours start strictly after `local_now` (local time)."""
    days = config.get("days", {})
    for offset in range(0, 8):
        day = local_now + timedelta(days=offset)
        window = days.get(WEEKDAYS[day.weekday()], DEFAULT_WINDOW)
        start, _ = _parse_window(window)
        candidate = day.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if candidate > local_now:
            return candidate
    return local_now + timedelta(days=1)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as err:
        raise ValueError("Above/below need a number") from err


def condition_result(config: Mapping[str, Any], state: str | None) -> str | None:
    """Reason an entity condition fails, or None when it holds (spec §9.2).

    `state` is the entity's state string, None when the entity is missing.
    `unavailable`/`unknown` always fail.
    """
    if state is None or state in _BAD_STATES:
        return "unavailable" if state is None else state
    operator, wanted = config["operator"], config["value"]
    if operator == "is":
        ok = state == str(wanted)
    elif operator == "is_not":
        ok = state != str(wanted)
    else:
        try:
            value, bound = float(state), float(wanted)
        except ValueError:
            return "not_a_number"
        ok = value > bound if operator == "above" else value < bound
    return None if ok else "condition_false"


def weather_condition_wet(condition: str | None) -> bool:
    """Whether a `weather.*` condition reads wet (spec §9.3)."""
    return condition in WET_CONDITIONS


def forecast_confirms_rain(
    entries: Iterable[Mapping[str, Any]], precip_pct: float
) -> bool:
    """Whether any given forecast hour (current/next) says rain (spec §9.4)."""
    for entry in entries:
        if weather_condition_wet(entry.get("condition")):
            return True
        probability = entry.get("precipitation_probability")
        if isinstance(probability, (int, float)) and probability >= precip_pct:
            return True
    return False
