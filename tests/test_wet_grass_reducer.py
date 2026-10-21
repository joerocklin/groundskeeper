"""Wet grass Check and "Is it raining?" at the reducer seam (spec §9.3, §9.4, §7.5)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.mowing_director.adapter import RainStop
from custom_components.mowing_director.checks import (
    default_check_records,
    forecast_confirms_rain,
    weather_condition_wet,
)
from custom_components.mowing_director.core import (
    AnswerPrompt,
    ChecksEdited,
    SignalReceived,
    SnapshotReceived,
    StartRunEffect,
    TimerFired,
    WetGrassDoneToday,
    WetInputs,
    initial_core_state,
    AddPlan,
    PauseSchedule,
)
from tests.test_core_reducer import DOCKED, ENV, MOWING, T0, at, events, make_plan, step

WET = replace(DOCKED, rain=True)
DRY = replace(DOCKED, rain=False)
H = 3600


def env_for(wet=None, *, wet_cfg=None, work_hours=None, enabled=True):
    records = default_check_records()
    for rec in records:
        if rec["type"] == "wet_grass":
            rec["enabled"] = enabled
            rec["config"] = {**rec["config"], **(wet_cfg or {})}
        if rec["type"] == "work_hours" and work_hours is not None:
            rec["config"] = {"days": work_hours}
    return replace(ENV, checks=tuple(records), tz=timezone.utc, wet=wet or WetInputs())


def sunrise_at(hour):
    return lambda day: datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def begin(env, snapshot=DRY):
    state, _ = step(initial_core_state(), AddPlan(at(0), *make_plan(), start=True), env)
    state, fx = step(state, SnapshotReceived(at(0), snapshot), env)
    return state, fx


def reasons(state):
    return [(w["check"], w["reason"]) for w in state["state"]["waiting_on"] if w["check"] == "wet_grass"]


def prompts(state, kind="is_raining"):
    return [p for p in state["state"]["prompts"] if p["kind"] == kind]


# -- pure rules ---------------------------------------------------------------------


@pytest.mark.parametrize("cond", ["rainy", "pouring", "lightning-rainy", "snowy", "hail"])
def test_wet_weather_conditions(cond) -> None:
    assert weather_condition_wet(cond)


@pytest.mark.parametrize("cond", ["cloudy", "sunny", "fog", "windy", "unavailable", None])
def test_dry_weather_conditions(cond) -> None:
    assert not weather_condition_wet(cond)


def test_forecast_confirms_on_rainy_condition_or_probability() -> None:
    assert forecast_confirms_rain([{"condition": "rainy"}], 50)
    assert forecast_confirms_rain([{"condition": "cloudy", "precipitation_probability": 50}], 50)
    assert not forecast_confirms_rain([{"condition": "cloudy", "precipitation_probability": 49}], 50)
    assert not forecast_confirms_rain([{"condition": "cloudy"}], 50)
    assert not forecast_confirms_rain([], 50)


def test_wet_grass_defaults() -> None:
    rec = {r["type"]: r for r in default_check_records()}["wet_grass"]
    assert rec["enabled"] is True
    assert (rec["timeout_s"], rec["policy"]) == (12 * H, "notify")
    cfg = rec["config"]
    assert (cfg["mower_rain"], cfg["dry_out_s"], cfg["morning_offset_s"]) == (True, 3 * H, 2 * H)
    assert cfg["weather"] == [] and cfg["sensors"] == []


# -- sources and dry-out --------------------------------------------------------------


def test_mower_flag_fails_then_dry_out_after_last_wet_reading() -> None:
    env = env_for(WetInputs(sensors=("on",)))
    state, _ = begin(env, WET)
    assert ("wet_grass", "mower_rain") in reasons(state)
    state, _ = step(state, SnapshotReceived(at(100), DRY), replace(env, wet=WetInputs(sensors=("off",))))
    env = replace(env, wet=WetInputs(sensors=("off",)))
    assert reasons(state) == [("wet_grass", "dry_out")]
    assert state["state"]["wet"]["last_wet_at"] == at(100).isoformat()
    state, _ = step(state, TimerFired(at(100 + 3 * H - 1)), env)
    assert reasons(state) == [("wet_grass", "dry_out")]
    state, _ = step(state, TimerFired(at(100 + 3 * H)), env)
    assert reasons(state) == []


def test_mower_flag_unknown_when_detection_off_is_ignored() -> None:
    state, _ = begin(env_for(), replace(DOCKED, rain=None))
    assert reasons(state) == []


def test_mower_source_can_be_turned_off() -> None:
    env = env_for(wet_cfg={"mower_rain": False})
    state, _ = begin(env, WET)
    assert reasons(state) == []
    assert prompts(state) == []


@pytest.mark.parametrize("cond", ["rainy", "pouring", "lightning-rainy", "snowy", "hail"])
def test_picked_weather_entity_wet_blocks(cond) -> None:
    state, _ = begin(env_for(WetInputs(weather=("cloudy", cond))))
    assert reasons(state) == [("wet_grass", "weather_wet")]


def test_picked_binary_sensor_on_blocks_and_dry_out_follows() -> None:
    env = env_for(WetInputs(sensors=("off", "on")))
    state, _ = begin(env)
    assert reasons(state) == [("wet_grass", "sensor_wet")]
    env = replace(env, wet=WetInputs(sensors=("off", "off")))
    state, _ = step(state, TimerFired(at(60)), env)
    assert reasons(state) == [("wet_grass", "dry_out")]


def test_unavailable_sources_are_skipped() -> None:
    env = env_for(WetInputs(sensors=("unavailable",), weather=("unavailable", "unknown")))
    state, _ = begin(env)
    assert reasons(state) == []


def test_dry_out_is_persisted_absolute_and_per_mower_configurable() -> None:
    env = env_for(WetInputs(sensors=("on",)), wet_cfg={"dry_out_s": 600})
    state, _ = begin(env)
    env = replace(env, wet=WetInputs(sensors=("off",)))
    state, _ = step(state, TimerFired(at(10)), env)
    assert state["state"]["wet"]["last_wet_at"] == at(10).isoformat()
    state, _ = step(state, TimerFired(at(10 + 599)), env)
    assert reasons(state) == [("wet_grass", "dry_out")]
    state, _ = step(state, TimerFired(at(10 + 600)), env)
    assert reasons(state) == []


def test_disabled_wet_grass_never_blocks_or_asks() -> None:
    env = env_for(WetInputs(sensors=("on",)), enabled=False)
    state, _ = begin(env, WET)
    assert reasons(state) == []
    assert prompts(state) == []


def test_run_starts_after_stability_once_dry() -> None:
    env = env_for()
    state, _ = begin(env)
    state, fx = step(state, TimerFired(at(60)), env)
    assert any(isinstance(e, StartRunEffect) for e in fx)


def test_wet_grass_timeout_is_12h_notify_and_never_pauses() -> None:
    env = env_for(WetInputs(sensors=("on",)))
    state, _ = begin(env)
    state, fx = step(state, TimerFired(at(12 * H)), env)
    [event] = events(fx, "check_timeout")
    assert (event.payload["check_id"], event.payload["policy"]) == ("wet_grass", "notify")
    assert state["state"]["schedule"]["status"] == "waiting"


# -- morning dry-out ------------------------------------------------------------------


def test_morning_dry_out_until_sunrise_plus_two_hours() -> None:
    env = env_for(WetInputs(sunrise=sunrise_at(6)))
    state, _ = begin(env)  # T0 is 12:00
    assert reasons(state) == []
    early = datetime(2026, 10, 22, 7, 59, tzinfo=timezone.utc)
    state, _ = step(state, TimerFired(early), env)
    assert reasons(state) == [("wet_grass", "morning_dry_out")]
    state, _ = step(state, TimerFired(early + timedelta(minutes=1)), env)
    assert reasons(state) == []


def test_morning_dry_out_can_be_toggled_off() -> None:
    env = env_for(WetInputs(sunrise=sunrise_at(6)), wet_cfg={"morning": False})
    state, _ = begin(env)
    state, _ = step(state, TimerFired(datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)), env)
    assert reasons(state) == []


# -- confirmation ---------------------------------------------------------------------


def test_sensor_wet_confirms_rain_without_asking() -> None:
    state, _ = begin(env_for(WetInputs(sensors=("on",))), WET)
    assert prompts(state) == []


def test_weather_current_condition_confirms_without_asking() -> None:
    state, _ = begin(env_for(WetInputs(weather=("pouring",))), WET)
    assert prompts(state) == []


def test_forecast_confirms_without_asking() -> None:
    state, _ = begin(env_for(WetInputs(weather=("cloudy",), forecast_rain=True)), WET)
    assert prompts(state) == []


def test_unconfirmed_flag_asks_is_it_raining_once_per_episode() -> None:
    env = env_for(WetInputs(sensors=("off",), weather=("cloudy",)))
    state, fx = begin(env, WET)
    [prompt] = prompts(state)
    assert prompt["actions"] == ["no", "yes", "yes_done_today"]
    assert prompt["next_reminder_at"] is None  # no reminder
    assert len(events(fx, "prompt_sent")) == 1
    state, fx = step(state, SnapshotReceived(at(30), WET), env)
    assert len(prompts(state)) == 1 and not events(fx, "prompt_sent")
    # flag clears (prompt settled), later goes wet again: a new episode asks again
    state, _ = step(state, SnapshotReceived(at(60), DRY), env)
    assert prompts(state) == []
    state, fx = step(state, SnapshotReceived(at(90), WET), env)
    assert len(prompts(state)) == 1 and len(events(fx, "prompt_sent")) == 1


def test_no_confirming_source_at_all_asks() -> None:
    state, _ = begin(env_for(), WET)
    assert len(prompts(state)) == 1


def test_not_asked_when_schedule_is_not_waiting_or_running_until_it_is() -> None:
    env = env_for()
    state, _ = begin(env, DRY)
    state, _ = step(state, PauseSchedule(at(5)), env)
    state, _ = step(state, SnapshotReceived(at(10), WET), env)
    assert prompts(state) == []
    state, _ = step(state, SnapshotReceived(at(20), DRY), env)
    assert prompts(state) == []


def test_toggled_off_category_does_not_ask_and_counts_as_rain() -> None:
    env = env_for(WetInputs(ask=False))
    state, _ = begin(env, WET)
    assert prompts(state) == []
    assert ("wet_grass", "mower_rain") in reasons(state)


def test_flag_going_wet_during_a_run_asks() -> None:
    env = env_for()
    state, _ = begin(env, DOCKED)
    state, _ = step(state, TimerFired(at(60)), env)
    state, _ = step(state, TimerFired(at(121)), env)
    state, _ = step(state, SnapshotReceived(at(130), replace(MOWING, rain=True)), env)
    assert state["state"]["schedule"]["status"] == "running"
    assert len(prompts(state)) == 1


def test_rain_stop_runs_confirm_or_ask_and_asks_once() -> None:
    env = env_for()
    state, _ = begin(env, DOCKED)
    state, _ = step(state, TimerFired(at(60)), env)
    state, _ = step(state, TimerFired(at(121)), env)
    state, _ = step(state, SnapshotReceived(at(130), MOWING), env)
    state, _ = step(state, SignalReceived(at(140), RainStop()), env)
    assert state["state"]["schedule"]["status"] == "waiting"
    assert len(prompts(state)) == 1
    state, fx = step(state, SnapshotReceived(at(150), replace(WET)), env)
    assert len(prompts(state)) == 1 and not events(fx, "prompt_sent")


# -- answers --------------------------------------------------------------------------


def asked(env):
    state, _ = begin(env, WET)
    [prompt] = prompts(state)
    return state, prompt["prompt_id"]


def test_answer_no_ignores_episode_no_dry_out() -> None:
    env = env_for()
    state, pid = asked(env)
    state, fx = step(state, AnswerPrompt(at(10), pid, "no"), env)
    assert prompts(state) == []
    assert events(fx, "prompt_answered")[0].payload["action"] == "no"
    state, _ = step(state, TimerFired(at(20)), env)
    assert reasons(state) == []  # passes even though the flag is still wet
    state, _ = step(state, SnapshotReceived(at(30), DRY), env)
    assert reasons(state) == []
    state, _ = step(state, SnapshotReceived(at(40), WET), env)  # next episode asks again
    assert len(prompts(state)) == 1


def test_answer_yes_is_normal_dry_out_and_more_rain_resets_it() -> None:
    env = env_for()
    state, pid = asked(env)
    state, _ = step(state, AnswerPrompt(at(10), pid, "yes"), env)
    assert ("wet_grass", "mower_rain") in reasons(state)
    state, _ = step(state, SnapshotReceived(at(100), DRY), env)
    assert reasons(state) == [("wet_grass", "dry_out")]
    state, _ = step(state, SnapshotReceived(at(2 * H), WET), env)
    state, _ = step(state, SnapshotReceived(at(2 * H + 10), DRY), env)
    state, _ = step(state, TimerFired(at(3 * H + 100)), env)
    assert reasons(state) == [("wet_grass", "dry_out")]  # reset by the later wet reading


def test_answer_done_for_today_blocks_until_next_work_start_plus_morning_dry_out() -> None:
    days = {d: ["08:00", "00:00"] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    env = env_for(WetInputs(sunrise=sunrise_at(7)), work_hours=days)
    state, _ = begin(env, WET)
    [prompt] = prompts(state)
    state, _ = step(state, AnswerPrompt(at(10), prompt["prompt_id"], "yes_done_today"), env)
    state, _ = step(state, SnapshotReceived(at(100), DRY), env)
    tomorrow = datetime(2026, 10, 22, tzinfo=timezone.utc)
    assert state["state"]["wet"]["done_until"] == (tomorrow + timedelta(hours=9)).isoformat()
    assert state["state"]["schedule"]["status"] == "waiting"
    state, _ = step(state, TimerFired(tomorrow + timedelta(hours=8, minutes=59)), env)
    assert ("wet_grass", "done_for_today") in reasons(state)
    state, _ = step(state, TimerFired(tomorrow + timedelta(hours=9)), env)
    assert ("wet_grass", "done_for_today") not in reasons(state)


def test_done_for_today_without_morning_dry_out_ends_at_work_start() -> None:
    days = {d: ["08:00", "00:00"] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    env = env_for(WetInputs(sunrise=sunrise_at(7)), work_hours=days, wet_cfg={"morning": False})
    state, _ = begin(env, DRY)
    state, _ = step(state, WetGrassDoneToday(at(10)), env)
    assert state["state"]["wet"]["done_until"] == datetime(
        2026, 10, 22, 8, tzinfo=timezone.utc
    ).isoformat()


def test_done_today_input_answers_pending_prompt() -> None:
    env = env_for()
    state, _ = asked(env)
    state, fx = step(state, WetGrassDoneToday(at(10)), env)
    assert prompts(state) == []
    assert events(fx, "prompt_answered")[0].payload["action"] == "yes_done_today"


def test_no_answer_is_treated_as_rain_without_reminder() -> None:
    env = env_for()
    state, _ = asked(env)
    state, fx = step(state, TimerFired(at(2 * H)), env)
    assert len(prompts(state)) == 1 and not events(fx, "prompt_reminder")
    assert ("wet_grass", "mower_rain") in reasons(state)


def test_check_edit_applies_immediately() -> None:
    env = env_for(WetInputs(sensors=("on",)))
    state, _ = begin(env)
    assert reasons(state)
    env = env_for(WetInputs(), enabled=False)
    state, _ = step(state, ChecksEdited(at(10), ("wet_grass",)), env)
    assert reasons(state) == []
