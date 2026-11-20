"""Issue #135: a stale Mower error, or a mid-job recharge, must not fail a Run.

Two rules keep a Mower error evidence, not a verdict. An error stamped before
the Mower's last reading of mowing on a live job is superseded by that reading,
and a Mower paused or docked on a live job, recharging or not, is not a stopped
one. The rules that already exist still decide whether it really stopped.

Pure reducer tests at the `reduce` seam, as the other reducer tables are.
"""

import json
from dataclasses import replace

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity

from custom_components.groundskeeper.adapter import Movement, NewError
from custom_components.groundskeeper.core import (
    AbortRunEffect,
    ResumeRunEffect,
    RetryRun,
    SignalReceived,
    SnapshotReceived,
    StartRunEffect,
    TimerFired,
    initial_core_state,
    normalise_core_state,
    snapshot_to_dict,
)
from tests.test_core_reducer import (
    DOCKED,
    IDLE_OFF_JOB,
    MOWING,
    NOT_ON_A_LIVE_JOB,
    PAUSED_ON_JOB,
    at,
    events,
    failed_with,
    mowing_state,
    run_status,
    step,
)
from tests.test_on_hold import RECHARGING
from tests.test_reconcile import LIVE, LOAD, fresh_stall, reconcile, run_of

CHARGING_ON_JOB = replace(
    MOWING, activity=LawnMowerActivity.PAUSED, recharging=True, battery=2
)
DOCKED_ON_JOB = replace(MOWING, activity=LawnMowerActivity.DOCKED)
DOCKED_CHARGING_ON_JOB = replace(DOCKED_ON_JOB, recharging=True, battery=20)
LOCKED = replace(MOWING, activity=LawnMowerActivity.ERROR)
# What a restart reads when the Mower sits on the charger mid-job in the Zone.
RESTART_CHARGING = replace(LIVE, activity=LawnMowerActivity.PAUSED, recharging=True, battery=2)
STALE_ERROR = NewError("2748", "Angle position sensor of lift motor disconnected", at(1500))


def no_command(fx):
    assert not [
        e for e in fx if isinstance(e, (StartRunEffect, AbortRunEffect, ResumeRunEffect))
    ]


def mowing_at(progress):
    return replace(MOWING, progress=progress, movement=Movement(progress_pct=progress))


def last_mowing(state):
    return state["state"]["last_mowing_at"]


def assert_tolerated(state, fx, code):
    """The Run stays `mowing` with the error recorded: no failure, no prompt, no command."""
    assert run_status(state) == ["mowing"]
    assert state["state"]["schedule"]["status"] == "running"
    assert state["state"]["prompts"] == []
    assert not events(fx, "prompt_sent")
    assert [e.payload["code"] for e in events(fx, "error_tolerated")] == [code]
    assert state["state"]["runs"][0]["tolerated_errors"][-1]["code"] == code
    assert "error" not in state["state"]["runs"][0]
    no_command(fx)


def front_yard_2026_11_19(*, error_first):
    """The 2026-11-19 Front yard sequence: readings of 94 % then 100 %, the last
    reading of mowing at(1700), a long silence, the Mower back on the charger
    (`paused`, `recharging`, live job, battery 2 %) and, in the same second, the
    error stamped at(1500), before that last reading. Returns the state and
    every effect on the way."""
    state, effects = mowing_state(), []
    for event in (
        SnapshotReceived(at(1000), mowing_at(94)),
        SnapshotReceived(at(1700), mowing_at(100)),
        TimerFired(at(2300)),
        TimerFired(at(2900)),
        TimerFired(at(3500)),
    ):
        state, fx = step(state, event)
        effects += fx
    assert run_status(state) == ["mowing"]  # the silence alone changes nothing
    charger = SnapshotReceived(at(3600), CHARGING_ON_JOB)
    error = SignalReceived(at(3600), STALE_ERROR)
    for event in (error, charger) if error_first else (charger, error):
        state, fx = step(state, event)
        effects += fx
    return state, effects


@pytest.mark.parametrize("error_first", [False, True], ids=["charger_first", "error_first"])
def test_stale_error_on_the_charger_keeps_the_run_mowing_until_the_job_closes(error_first):
    state, fx = front_yard_2026_11_19(error_first=error_first)
    assert_tolerated(state, fx, "2748")
    assert state["state"]["runs"][0]["tolerated_errors"][0]["time"] == at(1500).isoformat()
    # The job closes: docked, no live job. The Run is done one window later.
    state, fx = step(state, SnapshotReceived(at(5000), DOCKED))
    assert run_status(state) == ["mowing"]
    state, fx = step(state, TimerFired(at(5061)))
    [archived] = state["history"]["runs"]
    assert archived["status"] == "done"
    assert archived["tolerated_errors"][0]["code"] == "2748"
    no_command(fx)


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(CHARGING_ON_JOB, id="paused_recharging"),
        pytest.param(DOCKED_ON_JOB, id="docked_on_a_live_job"),
        pytest.param(DOCKED_CHARGING_ON_JOB, id="docked_recharging_on_a_live_job"),
    ],
)
def test_fresh_error_while_paused_or_docked_on_a_live_job_is_tolerated(snapshot):
    state = mowing_state()
    state["state"]["snapshot"] = snapshot_to_dict(snapshot)
    state, fx = step(state, SignalReceived(at(500), NewError("1008", "boom", at(499))))
    assert_tolerated(state, fx, "1008")


def test_fresh_error_while_returning_on_a_live_job_still_fails_the_run():
    state = mowing_state()
    state["state"]["snapshot"] = snapshot_to_dict(
        replace(MOWING, activity=LawnMowerActivity.RETURNING)
    )
    state, fx = step(state, SignalReceived(at(500), NewError("1008", "boom", at(499))))
    failed_with(state, "error")
    no_command(fx)
    assert [p["kind"] for p in state["state"]["prompts"]] == ["run_failed"]


@pytest.mark.parametrize("snapshot", NOT_ON_A_LIVE_JOB)
def test_error_stamped_before_the_last_mowing_reading_is_tolerated_whatever_the_snapshot(
    snapshot,
):
    """The reading of mowing supersedes the error; the other rules decide next."""
    state = mowing_state()  # last reading of mowing: at(70)
    state["state"]["snapshot"] = snapshot_to_dict(snapshot)
    state, fx = step(state, SignalReceived(at(500), NewError("1003", "boom", at(60))))
    assert run_status(state) == ["mowing"]
    assert state["state"]["prompts"] == []
    [event] = events(fx, "error_tolerated")
    assert event.payload["time"] == at(60).isoformat()
    no_command(fx)


def test_error_stamped_before_the_last_mowing_reading_is_tolerated_with_no_snapshot():
    state = mowing_state()
    state["state"]["snapshot"] = None
    state, fx = step(state, SignalReceived(at(500), NewError("1003", "boom", at(60))))
    assert run_status(state) == ["mowing"]
    assert len(events(fx, "error_tolerated")) == 1


def test_error_with_no_mowing_reading_ever_still_fails_the_run():
    """A Store written before the field existed: nothing supersedes the error."""
    state = mowing_state()
    del state["state"]["last_mowing_at"]
    state = normalise_core_state(state)
    assert last_mowing(state) is None
    state["state"]["snapshot"] = snapshot_to_dict(LOCKED)
    state, _ = step(state, SignalReceived(at(500), NewError("1003", "boom", at(60))))
    failed_with(state, "error")


def test_an_error_stamp_without_a_time_zone_is_not_compared_and_follows_the_other_rules():
    state = mowing_state()
    state["state"]["snapshot"] = snapshot_to_dict(LOCKED)
    naive = NewError("1003", "boom", at(60).replace(tzinfo=None))
    state, _ = step(state, SignalReceived(at(500), naive))
    failed_with(state, "error")


def test_error_stamped_at_the_last_mowing_reading_is_not_older_than_it():
    state = mowing_state()
    state["state"]["snapshot"] = snapshot_to_dict(LOCKED)
    state, _ = step(state, SignalReceived(at(500), NewError("1003", "boom", at(70))))
    failed_with(state, "error")


def test_stale_error_while_the_mower_reads_error_fails_the_run_after_the_error_window():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(400), mowing_at(50)))
    state, _ = step(state, SnapshotReceived(at(500), replace(LOCKED, progress=50)))
    state, fx = step(state, SignalReceived(at(510), NewError("1008", "Robot is locked", at(300))))
    assert run_status(state) == ["mowing"]
    assert len(events(fx, "error_tolerated")) == 1
    # The window starts when the error is seen: a locked Mower pushes no more snapshots.
    assert state["state"]["deadlines"]["error_stable"] == at(570).isoformat()
    state, _ = step(state, TimerFired(at(569)))
    assert run_status(state) == ["mowing"]
    state, fx = step(state, TimerFired(at(571)))
    failed_with(state, "error")
    assert state["state"]["runs"][0]["error"]["code"] == "1008"
    no_command(fx)


def test_stale_error_then_idle_without_a_live_job_fails_stopped_off_dock():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(400), mowing_at(50)))
    state, _ = step(state, SnapshotReceived(at(1000), IDLE_OFF_JOB))
    state, fx = step(state, SignalReceived(at(1010), NewError("1008", "stuck", at(300))))
    assert run_status(state) == ["mowing"]
    assert len(events(fx, "error_tolerated")) == 1
    state, _ = step(state, TimerFired(at(1030)))
    assert run_status(state) == ["mowing"]
    state, fx = step(state, TimerFired(at(1061)))
    failed_with(state, "stopped_off_dock")
    no_command(fx)


def test_tolerated_error_on_the_charger_still_holds_the_run_when_the_battery_never_rises():
    """The stall rules stay in charge, and none of them sends a command."""
    state = mowing_state()
    effects = []
    for event in (
        SnapshotReceived(at(100), RECHARGING),
        SignalReceived(at(110), NewError("2748", "lift", at(105))),
        TimerFired(at(100 + 3599)),
    ):
        state, fx = step(state, event)
        effects += fx
    assert run_status(state) == ["mowing"]
    assert [e.payload["code"] for e in events(effects, "error_tolerated")] == ["2748"]
    state, fx = step(state, TimerFired(at(100 + 3600)))
    effects += fx
    assert run_status(state) == ["on_hold"]
    assert state["state"]["runs"][0]["on_hold_reason"] == "recharge_stall"
    no_command(effects)


# -- the last reading of mowing (persisted) ---------------------------------------


def test_a_mowing_reading_on_a_live_job_records_its_time():
    state = mowing_state()
    assert last_mowing(state) == at(70).isoformat()
    state, _ = step(state, SnapshotReceived(at(900), mowing_at(60)))
    assert last_mowing(state) == at(900).isoformat()


def test_a_new_state_has_no_reading_of_mowing():
    assert last_mowing(initial_core_state()) is None


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(PAUSED_ON_JOB, id="paused"),
        pytest.param(CHARGING_ON_JOB, id="paused_recharging"),
        pytest.param(DOCKED_ON_JOB, id="docked_on_a_live_job"),
        pytest.param(LOCKED, id="activity_error"),
        pytest.param(replace(MOWING, activity=LawnMowerActivity.RETURNING), id="returning"),
        pytest.param(replace(MOWING, job_active=False), id="mowing_but_no_live_job"),
        pytest.param(replace(MOWING, available=False), id="unavailable"),
        pytest.param(DOCKED, id="docked"),
    ],
)
def test_a_snapshot_that_is_not_mowing_on_a_live_job_does_not_move_it(snapshot):
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(900), snapshot))
    assert last_mowing(state) == at(70).isoformat()


def test_a_timer_tick_does_not_move_it():
    state = mowing_state()
    state, _ = step(state, TimerFired(at(900)))
    assert last_mowing(state) == at(70).isoformat()


def test_it_survives_a_store_round_trip():
    state = json.loads(json.dumps(mowing_state()))
    assert last_mowing(state) == at(70).isoformat()
    state, _ = step(state, TimerFired(at(80)))
    assert last_mowing(state) == at(70).isoformat()


def test_a_retry_does_not_reset_it():
    state = mowing_state()
    state, _ = step(state, SnapshotReceived(at(1000), IDLE_OFF_JOB))
    state, _ = step(state, TimerFired(at(1061)))
    failed_with(state, "stopped_off_dock")
    state, _ = step(state, RetryRun(at(1100)))
    assert last_mowing(state) == at(70).isoformat()


# -- after a restart (reconcile replays signals through the same rules) -----------

def test_restart_replayed_stale_error_on_the_charger_follows_the_live_rule():
    """Stamped before the last reading of mowing: superseded, the Run goes on."""
    state = fresh_stall(mowing_state())
    state["state"]["last_error_time"] = at(50).isoformat()
    state["state"]["last_mowing_at"] = at(4000).isoformat()
    state, fx = reconcile(state, RESTART_CHARGING, signals=[NewError("2748", "lift", at(3000))])
    assert_tolerated(state, fx, "2748")


def test_restart_replayed_fresh_error_while_recharging_on_a_live_job_is_tolerated():
    state = fresh_stall(mowing_state())
    state["state"]["last_error_time"] = at(50).isoformat()
    state["state"]["last_mowing_at"] = at(4000).isoformat()
    state, fx = reconcile(state, RESTART_CHARGING, signals=[NewError("2748", "lift", at(5000))])
    assert_tolerated(state, fx, "2748")


def test_restart_replayed_fresh_error_with_the_mower_locked_still_fails():
    state = fresh_stall(mowing_state())
    state["state"]["last_error_time"] = at(50).isoformat()
    state["state"]["last_mowing_at"] = at(4000).isoformat()
    state, fx = reconcile(state, replace(LIVE, activity=LawnMowerActivity.ERROR),
                          signals=[NewError("2748", "lift", at(5000))])
    assert run_of(state)["status"] == "failed"
    no_command(fx)


def test_a_live_mowing_snapshot_at_the_restart_is_a_reading_of_mowing():
    state = fresh_stall(mowing_state())
    state, _ = reconcile(state, LIVE)
    assert last_mowing(state) == LOAD.isoformat()
