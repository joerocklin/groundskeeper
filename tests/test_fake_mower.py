"""FakeMower scripting behaviour (issue #2 AC).

The contract suite proves FakeMower satisfies the protocol; these tests
pin the scripting surface later core tickets drive: snapshot sequences,
delayed effects for unconfirmed-verify, command recording, and the
panel-renderable extras schema.
"""

from __future__ import annotations

import json
from dataclasses import replace

from homeassistant.components.lawn_mower import LawnMowerActivity

from custom_components.mowing_director.adapter import (
    Accepted,
    Refused,
    Unconfirmed,
    ZoneTaskComplete,
)
from tests.fake_mower import (
    DEFAULT_LIMITS,
    DEFAULT_SNAPSHOT,
    FakeMower,
    make_sink_recorder,
)


async def test_command_records_name_and_args() -> None:
    """Every command call is recorded as (name, args)."""
    mower = FakeMower()
    settings = _settings()
    await mower.start_run("11111111", settings)
    await mower.dock()
    assert [name for name, _ in mower.commands] == ["start_run", "dock"]
    assert mower.commands[0][1] == ("11111111", settings)
    assert mower.commands[1][1] == ()


async def test_default_result_is_accepted() -> None:
    """Unscripted commands return accepted."""
    mower = FakeMower()
    assert isinstance(await mower.refresh(), Accepted)


async def test_scripted_results_are_consumed_in_order() -> None:
    """Scripted results come back per call; then defaults resume."""
    mower = FakeMower()
    mower.script_result("dock", Refused(reason="not_ready"))
    assert isinstance(await mower.dock(), Refused)
    assert isinstance(await mower.dock(), Accepted)


async def test_delayed_effect_lands_after_command_returns() -> None:
    """A scripted effect is NOT applied by the command itself.

    This is the unconfirmed-verify shape: `start_run` returns
    `unconfirmed`, the core keeps watching, and the snapshot change only
    becomes visible when the test advances the effect.
    """
    mower = FakeMower()
    mower.script_result("start_run", Unconfirmed())
    mower.script_effect(
        "start_run",
        replace(
            DEFAULT_SNAPSHOT,
            activity=LawnMowerActivity.MOWING,
            job_active=True,
            zone="11111111",
            job_ref="job-7",
        ),
    )
    result = await mower.start_run("11111111", _settings())
    assert isinstance(result, Unconfirmed)
    assert mower.snapshot == DEFAULT_SNAPSHOT  # nothing visible yet

    sink, received = make_sink_recorder()
    mower.subscribe(sink)
    assert mower.apply_effects() == 1
    assert mower.snapshot.activity is LawnMowerActivity.MOWING
    assert mower.snapshot.job_ref == "job-7"
    assert received == [("snapshot", mower.snapshot)]


async def test_snapshot_sequence_pushes_in_order() -> None:
    """script_snapshot/push_next_snapshot walks a sequence."""
    mower = FakeMower()
    s1 = replace(DEFAULT_SNAPSHOT, battery=50)
    s2 = replace(DEFAULT_SNAPSHOT, battery=40)
    mower.script_snapshot(s1)
    mower.script_snapshot(s2)
    assert mower.push_next_snapshot() is True
    assert mower.snapshot == s1
    assert mower.push_next_snapshot() is True
    assert mower.snapshot == s2
    assert mower.push_next_snapshot() is False


async def test_signals_reach_sinks() -> None:
    """emit_signal pushes to every subscribed sink."""
    mower = FakeMower()
    sink_a, got_a = make_sink_recorder()
    sink_b, got_b = make_sink_recorder()
    mower.subscribe(sink_a)
    unsub_b = mower.subscribe(sink_b)
    unsub_b()
    mower.emit_signal(ZoneTaskComplete(zone="11111111"))
    assert got_a == [("signal", ZoneTaskComplete(zone="11111111"))]
    assert got_b == []


async def test_set_task_enabled_applies_only_when_accepted() -> None:
    """A refused set_task_enabled leaves the task list untouched."""
    mower = FakeMower()
    mower.script_result("set_task_enabled", Refused(reason="offline"))
    await mower.set_task_enabled("t-1", False)
    tasks = await mower.list_native_tasks()
    assert tasks[0].enabled is True

    await mower.set_task_enabled("t-1", False)
    tasks = await mower.list_native_tasks()
    assert tasks[0].enabled is False


def test_extras_schema_is_panel_renderable() -> None:
    """Every extras field carries a selector the panel can serialise (A7)."""
    payload = [
        {"key": f.key, "label": f.label, **f.selector.serialize()}
        for f in DEFAULT_LIMITS.extras_schema
    ]
    round_tripped = json.loads(json.dumps(payload))
    assert [f["key"] for f in round_tripped] == ["border_grid_order", "ultra_wave"]
    assert round_tripped[0]["selector"]["select"]["options"][0]["value"] == "border"
    assert round_tripped[1]["selector"] == {"boolean": {}}


def _settings():
    from custom_components.mowing_director.adapter import RunSettings

    return RunSettings(
        blade_height_mm=70,
        bearing_deg=135.0,
        spacing_cm=22.0,
        speed_m_s=0.6,
        perimeter_laps=1,
        extras={f.key: f.default for f in DEFAULT_LIMITS.extras_schema},
    )
