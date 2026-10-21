"""Anomaly detectors at the reducer seam (spec §11, §12, §13.4).

Pure: inputs in, state and effects out. Expected values come from the spec
(2 h, 95 %, 2x, >= 3 samples, last 20), not from the implementation.
"""

from dataclasses import replace

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity

from custom_components.mowing_director.core import (
    AddPlan,
    AnswerPrompt,
    DismissAnomaly,
    SignalReceived,
    SnapshotReceived,
    TimerFired,
    initial_core_state,
)
from custom_components.mowing_director.adapter import ZoneTaskComplete
from tests.test_core_reducer import (
    DOCKED,
    ENV,
    MOWING,
    at,
    events,
    make_plan,
    mowing_state,
    step,
)

H = 3600
LOW = replace(DOCKED, battery=60)


def anomalies(state, detector=None):
    active = state["state"]["anomalies"]["active"]
    return [a for a in active if detector in (None, a["detector"])]


def prompts(state):
    return [p for p in state["state"]["prompts"] if p["kind"] == "anomaly"]


def env_with(**actions):
    return replace(ENV, anomaly_actions=actions)


def docked_since_zero(env=ENV, snapshot=LOW, plan=False):
    state = initial_core_state()
    if plan:
        state, _ = step(state, AddPlan(at(0), *make_plan(), start=True), env)
    state, fx = step(state, SnapshotReceived(at(0), snapshot), env)
    return state, fx


# -- charge not rising while docked (§11.2) ------------------------------------------


def test_charge_not_rising_fires_after_two_hours_docked_below_95() -> None:
    state, _ = docked_since_zero()
    state, fx = step(state, TimerFired(at(2 * H - 1)))
    assert anomalies(state) == []
    state, fx = step(state, TimerFired(at(2 * H)))
    (anomaly,) = anomalies(state, "charge_not_rising")
    (event,) = events(fx, "anomaly_detected")
    assert event.payload["detector"] == "charge_not_rising"
    assert event.payload["battery"] == 60
    assert event.payload["flat_minutes"] == 120
    assert event.payload["anomaly_id"] == anomaly["id"]
    (prompt,) = prompts(state)
    assert prompt["actions"] == ["dismiss"]
    assert prompt["subject"] == "charge_not_rising"
    assert state["state"]["snapshot"]["activity"] == "docked"  # nothing commanded
    assert [type(e).__name__ for e in fx if "Run" in type(e).__name__] == []


def test_the_two_hour_deadline_is_armed_so_a_quiet_dock_still_fires() -> None:
    state, fx = docked_since_zero()
    assert state["state"]["deadlines"]["anomaly_charge"] == at(2 * H).isoformat()


def test_a_full_battery_is_not_an_anomaly() -> None:
    state, _ = docked_since_zero(snapshot=replace(DOCKED, battery=95))
    state, fx = step(state, TimerFired(at(5 * H)))
    assert anomalies(state) == []


def test_battery_rising_restarts_the_clock_and_clears_an_active_anomaly() -> None:
    state, _ = docked_since_zero()
    state, _ = step(state, SnapshotReceived(at(H), replace(LOW, battery=61)))
    state, fx = step(state, TimerFired(at(2 * H)))
    assert anomalies(state) == []
    state, fx = step(state, TimerFired(at(3 * H)))
    assert len(anomalies(state)) == 1
    state, fx = step(state, SnapshotReceived(at(3 * H + 60), replace(LOW, battery=62)))
    assert anomalies(state) == []
    assert [e.type for e in events(fx)] == ["anomaly_cleared"]
    assert events(fx)[0].payload["detector"] == "charge_not_rising"
    assert len(prompts(state)) == 1  # clearing sends nothing, so nothing is withdrawn


def test_leaving_the_dock_clears_and_the_clock_restarts_on_return() -> None:
    state, _ = docked_since_zero()
    state, _ = step(state, TimerFired(at(2 * H)))
    out = replace(LOW, activity=LawnMowerActivity.MOWING, job_active=True)
    state, fx = step(state, SnapshotReceived(at(2 * H + 10), out))
    assert anomalies(state) == []
    assert [e.type for e in events(fx, "anomaly_cleared")] == ["anomaly_cleared"]
    state, _ = step(state, SnapshotReceived(at(2 * H + 20), LOW))
    state, _ = step(state, TimerFired(at(4 * H + 19)))
    assert anomalies(state) == []
    state, _ = step(state, TimerFired(at(4 * H + 20)))
    assert len(anomalies(state)) == 1


def test_not_checked_during_a_run() -> None:
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), replace(MOWING, battery=40)))
    state, _ = step(state, TimerFired(at(5 * H)))
    assert anomalies(state, "charge_not_rising") == []


def test_action_off_detects_nothing() -> None:
    env = env_with(charge_not_rising="off")
    state, _ = docked_since_zero(env)
    state, fx = step(state, TimerFired(at(3 * H)), env)
    assert anomalies(state) == [] and events(fx) == [] and prompts(state) == []


def test_action_pause_pauses_an_active_schedule_but_not_an_idle_one() -> None:
    env = env_with(charge_not_rising="pause")
    state, _ = docked_since_zero(  # battery 10 % blocks the start: waiting
        env, snapshot=replace(LOW, battery=10), plan=True)
    assert state["state"]["schedule"]["status"] == "waiting"
    state, _ = step(state, TimerFired(at(2 * H)), env)
    assert state["state"]["schedule"]["status"] == "paused"
    assert state["state"]["schedule"]["pause_requested"] is True
    idle, _ = docked_since_zero(env)
    idle, _ = step(idle, TimerFired(at(2 * H)), env)
    assert idle["state"]["schedule"]["status"] == "idle"
    assert idle["state"]["schedule"]["pause_requested"] is False
    assert len(anomalies(idle)) == 1


# -- dismissal (§11.7) ---------------------------------------------------------------


def test_dismiss_by_prompt_or_input_keeps_it_from_refiring_until_it_recurs() -> None:
    state, _ = docked_since_zero()
    state, _ = step(state, TimerFired(at(2 * H)))
    (prompt,) = prompts(state)
    state, fx = step(state, AnswerPrompt(at(2 * H + 5), prompt["prompt_id"], "dismiss"))
    assert prompts(state) == []
    assert anomalies(state)[0]["dismissed"] is True
    state, fx = step(state, TimerFired(at(6 * H)))
    assert events(fx, "anomaly_detected") == [] and prompts(state) == []
    state, _ = step(state, SnapshotReceived(at(6 * H + 1), replace(LOW, battery=70)))
    assert anomalies(state) == []
    state, _ = step(state, TimerFired(at(8 * H + 1)))
    assert anomalies(state)[0]["dismissed"] is False and len(prompts(state)) == 1
    state, _ = step(state, DismissAnomaly(at(8 * H + 2), "charge_not_rising"))
    assert anomalies(state)[0]["dismissed"] is True and prompts(state) == []


# -- Run far longer than Zone history (§11.3) -----------------------------------------

REF = "11111111"
PAUSED = replace(MOWING, activity=LawnMowerActivity.PAUSED)


def sample(mowing_s, ref=REF):
    """A stored sample at speed 0.4 m/s, spacing 25 cm: value = s * 0.4 * 0.25."""
    return {"value": mowing_s * 0.1, "mowing_s": mowing_s, "native_ref": ref,
            "at": at(0).isoformat(), "speed_m_s": 0.4, "spacing_cm": 25.0,
            "perimeter_laps": 1, "blade_height_mm": 70, "angle_deg": 0.0}


def mowing_with_history(seconds=(900, 1000, 1100), env=ENV):
    state = mowing_state()  # mowing since t=70
    state["learned"]["durations"]["z1"] = [sample(s) for s in seconds]
    return state


def test_fires_once_when_mowing_time_passes_twice_the_median() -> None:
    state = mowing_with_history()  # median 1000 s -> expected 1000 s, fires at 2000 s
    assert state["state"]["deadlines"].get("anomaly_run") is None  # armed on next eval
    state, _ = step(state, SnapshotReceived(at(100), MOWING))
    assert state["state"]["deadlines"]["anomaly_run"] == at(70 + 2000).isoformat()
    state, fx = step(state, TimerFired(at(70 + 1999)))
    assert anomalies(state) == []
    state, fx = step(state, TimerFired(at(70 + 2000)))
    (anomaly,) = anomalies(state, "run_too_long")
    (event,) = events(fx, "anomaly_detected")
    assert event.payload["run_id"] == "p1-r1"
    assert event.payload["expected_minutes"] == pytest.approx(1000 / 60)
    assert event.payload["elapsed_minutes"] == pytest.approx(2000 / 60)
    assert state["state"]["runs"][0]["status"] == "mowing"  # only observes
    assert state["state"]["runs"][0]["anomaly_ids"] == [anomaly["id"]]
    (prompt,) = prompts(state)
    assert prompt["context"]["run_id"] == "p1-r1"
    state, fx = step(state, TimerFired(at(70 + 3000)))
    assert events(fx, "anomaly_detected") == []
    assert len(anomalies(state)) == 1


def test_needs_three_samples() -> None:
    state = mowing_with_history((900, 1000))
    state, _ = step(state, SnapshotReceived(at(100), MOWING))
    state, _ = step(state, TimerFired(at(70 + 10_000)))
    assert anomalies(state) == []


def test_expected_time_scales_with_the_runs_speed_and_spacing() -> None:
    state = mowing_with_history((90, 100, 110))  # median 100 s at full speed
    state["state"]["runs"][0]["settings"]["speed_m_s"] = 0.2  # half speed: 200 s
    state, _ = step(state, SnapshotReceived(at(100), MOWING))
    state, _ = step(state, TimerFired(at(70 + 399)))
    assert anomalies(state) == []
    state, _ = step(state, TimerFired(at(70 + 400)))
    assert len(anomalies(state)) == 1


def test_samples_of_a_rebuilt_native_ref_are_ignored() -> None:
    state = mowing_with_history()
    for s in state["learned"]["durations"]["z1"]:
        s["native_ref"] = "stale"
    state, _ = step(state, SnapshotReceived(at(100), MOWING))
    state, _ = step(state, TimerFired(at(70 + 10_000)))
    assert anomalies(state) == []


def test_on_hold_time_does_not_count() -> None:
    state = mowing_with_history()
    state, _ = step(state, SnapshotReceived(at(100), PAUSED))
    state, _ = step(state, TimerFired(at(160)))  # on hold: 30 s mowed so far (paused is not mowing)
    assert state["state"]["runs"][0]["status"] == "on_hold"
    assert state["state"]["deadlines"].get("anomaly_run") is None
    state, _ = step(state, SnapshotReceived(at(5000), MOWING))
    state, _ = step(state, TimerFired(at(5060)))  # back mowing at 5060
    assert state["state"]["runs"][0]["status"] == "mowing"
    assert state["state"]["deadlines"]["anomaly_run"] == at(5060 + 2000 - 30).isoformat()


def test_mid_job_recharge_does_not_count() -> None:
    state = mowing_with_history()
    docked = replace(MOWING, activity=LawnMowerActivity.DOCKED, recharging=True)
    state, _ = step(state, SnapshotReceived(at(100), docked))  # 30 s mowed
    state, _ = step(state, SnapshotReceived(at(3000), MOWING))
    state, _ = step(state, TimerFired(at(3001)))
    assert state["state"]["deadlines"]["anomaly_run"] == at(3000 + 2000 - 30).isoformat()


def test_clears_when_the_run_ends_and_a_run_ending_fires_cleared_only() -> None:
    state = mowing_with_history()
    state, _ = step(state, SnapshotReceived(at(100), MOWING))
    state, _ = step(state, TimerFired(at(70 + 2000)))
    state, fx = step(state, SignalReceived(at(2100), ZoneTaskComplete(REF)))
    assert anomalies(state) == []
    assert [e.type for e in events(fx, "anomaly_cleared")] == ["anomaly_cleared"]
    assert "anomaly_run" not in state["state"]["deadlines"]
    (archived,) = state["history"]["runs"]
    assert len(archived["anomaly_ids"]) == 1  # the link survives on the history record


def test_action_pause_sets_pause_requested_and_the_run_finishes_first() -> None:
    env = env_with(run_too_long="pause")
    state = mowing_with_history(env=env)
    state, _ = step(state, SnapshotReceived(at(100), MOWING), env)
    state, _ = step(state, TimerFired(at(70 + 2000)), env)
    assert state["state"]["schedule"]["pause_requested"] is True
    assert state["state"]["schedule"]["status"] == "running"
    assert state["state"]["runs"][0]["status"] == "mowing"


# -- samples (§11.3) ---------------------------------------------------------------------


def test_a_done_run_adds_a_sample_with_its_raw_settings_and_mowing_time() -> None:
    state = mowing_state()  # mowing from t=70
    state, _ = step(state, SnapshotReceived(at(500), MOWING))
    state, _ = step(state, SignalReceived(at(1070), ZoneTaskComplete(REF)))
    (s,) = state["learned"]["durations"]["z1"]
    assert s["mowing_s"] == 1000
    assert s["value"] == pytest.approx(1000 * 0.4 * 0.25)
    assert (s["speed_m_s"], s["spacing_cm"], s["perimeter_laps"]) == (0.4, 25.0, 1)
    assert (s["blade_height_mm"], s["angle_deg"], s["native_ref"]) == (70, 0.0, REF)
    assert s["at"] == at(1070).isoformat()


def test_sample_excludes_on_hold_time() -> None:
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(100), PAUSED))
    state, _ = step(state, TimerFired(at(160)))
    state, _ = step(state, SnapshotReceived(at(1000), MOWING))
    state, _ = step(state, TimerFired(at(1060)))
    state, _ = step(state, SignalReceived(at(1160), ZoneTaskComplete(REF)))
    (s,) = state["learned"]["durations"]["z1"]
    assert s["mowing_s"] == 30 + 100


def test_only_the_last_20_samples_are_kept() -> None:
    state = mowing_state()
    state["learned"]["durations"]["z1"] = [sample(i + 1) for i in range(20)]
    state, _ = step(state, SnapshotReceived(at(500), MOWING))
    state, _ = step(state, SignalReceived(at(1070), ZoneTaskComplete(REF)))
    samples = state["learned"]["durations"]["z1"]
    assert len(samples) == 20
    assert samples[0]["mowing_s"] == 2 and samples[-1]["mowing_s"] == 1000


def test_a_changed_native_ref_resets_the_history() -> None:
    state = mowing_state()
    state["learned"]["durations"]["z1"] = [sample(5, ref="old") for _ in range(4)]
    state, _ = step(state, SnapshotReceived(at(500), MOWING))
    state, _ = step(state, SignalReceived(at(1070), ZoneTaskComplete(REF)))
    assert [s["native_ref"] for s in state["learned"]["durations"]["z1"]] == [REF]


def test_a_run_that_goes_back_to_pending_adds_nothing_and_restarts_its_clock() -> None:
    from custom_components.mowing_director.adapter import RainStop

    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(500), MOWING))
    state, _ = step(state, SignalReceived(at(600), RainStop()))
    assert state["learned"]["durations"].get("z1", []) == []
    assert state["state"]["runs"][0]["status"] == "pending"
