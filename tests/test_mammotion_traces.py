"""Replay the ExampleSite-derived trace fixtures through the adapter (issue #8 AC).

The fixtures under `tests/traces/` are hand-built from the research
records (ExampleSite REST credentials are not available on this machine; each
fixture says so and cites its source). Each step is a batch of entity
state changes; replaying a trace drives the adapter exactly as live
state would and asserts the snapshots and signals it produced.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.core import HomeAssistant

from custom_components.mowing_director.adapter import (
    NewError,
    SelfCheckReason,
    Snapshot,
)
from custom_components.mowing_director.adapters.mammotion import MammotionAdapter
from tests.mammotion_states import install_baseline, set_entity

from .test_mammotion_adapter import NAME, make_adapter, make_director_entry

TRACES = Path(__file__).parent / "traces"


def load_trace(name: str) -> dict[str, Any]:
    trace = json.loads((TRACES / name).read_text())
    assert "HAND-BUILT" in trace["source"], "trace fixtures must declare their provenance"
    return trace


class _Recorder:
    def __init__(self) -> None:
        self.signals: list[Any] = []
        self.snapshots: list[Snapshot] = []

    def on_snapshot(self, snapshot: Snapshot) -> None:
        self.snapshots.append(snapshot)

    def on_signal(self, signal: Any) -> None:
        self.signals.append(signal)


def _transitions(values):
    """Consecutive-equal collapse: one trace step moves several entities,
    and each changed snapshot is pushed separately."""
    out = []
    for value in values:
        if not out or out[-1] != value:
            out.append(value)
    return out


async def _replay(hass: HomeAssistant, trace: dict[str, Any]) -> tuple[MammotionAdapter, _Recorder]:
    """Install the baseline, then apply each step's states in order."""
    entry, mower = await make_director_entry(hass)
    install_baseline(hass, entry, mower, NAME)
    adapter = make_adapter(hass, entry)
    recorder = _Recorder()
    adapter.subscribe(recorder)
    for step in trace["steps"]:
        for key, (state, attributes) in step["states"].items():
            set_entity(hass, entry, mower, NAME, key, state, attributes)
        await hass.async_block_till_done()
    return adapter, recorder


async def test_stepdown_run_trace_stays_docked(hass: HomeAssistant) -> None:
    """The 10-18 run: start_mow 500-unconfirmed, mower docked throughout;
    the adapter reports docked, not job active, and the readback holds at
    80 mm (the knife never reported a change)."""
    trace = load_trace("examplesite_2026_10_18_stepdown_run.json")
    adapter, recorder = await _replay(hass, trace)

    snapshot = adapter.snapshot
    assert snapshot.activity is LawnMowerActivity.DOCKED
    assert snapshot.job_active is False
    assert snapshot.progress == 0
    assert snapshot.blade_height_mm == 80
    # No signals: no error sensors moved, no task sensors appeared.
    assert recorder.signals == []
    # The docked state differs from the idle baseline, so one push landed.
    assert recorder.snapshots and recorder.snapshots[-1] == snapshot


async def test_2748_recurrence_trace(hass: HomeAssistant) -> None:
    """The 10-19 day: self_check normalisation across the spans, the
    non_work_hours flaps ignored, and the 2748 recurrence emitted once
    with its real timestamp."""
    trace = load_trace("examplesite_2026_10_19_2748_recurrence.json")
    adapter, recorder = await _replay(hass, trace)

    errors = [s for s in recorder.signals if isinstance(s, NewError)]
    assert errors == [
        NewError(
            code="2748",
            message="Angle position sensor of lift motor disconnected",
            time=datetime(2026, 10, 19, 18, 59, 58, tzinfo=UTC),
        )
    ]

    # End of day: dry, normal, docked and charging but not in a
    # charging-pause mode, so `recharging` (mid-job pause) stays False.
    snapshot = adapter.snapshot
    assert snapshot.self_check is None
    assert snapshot.rain is False
    assert snapshot.recharging is False

    # The self_check spans normalised at their moments. Consecutive equal
    # values collapse: one step moves several entities, and only real
    # changes push.
    checks = _transitions(s.self_check for s in recorder.snapshots)
    assert checks == [
        SelfCheckReason.DEVICE_NON_WORK_HOURS,
        None,
        SelfCheckReason.RAIN,
        None,
    ]


async def test_reconnect_blip_trace(hass: HomeAssistant) -> None:
    """The blip: unknown then a non-numeric placeholder read -> height
    None (never a trusted number), then the true 80 mm readback returns."""
    trace = load_trace("examplesite_reconnect_blip.json")
    adapter, recorder = await _replay(hass, trace)

    heights = [s.blade_height_mm for s in recorder.snapshots]
    # unknown -> None; the non-numeric placeholder changes nothing (still
    # None, so no push); the true readback returns.
    assert heights == [None, 80]
    assert adapter.snapshot.blade_height_mm == 80
