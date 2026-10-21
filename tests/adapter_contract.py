"""Reusable adapter CONTRACT suite (issue #2 AC, spec §3).

`_AdapterContract` asserts the protocol invariants every MowerAdapter
must satisfy. It is written against a single `mower` fixture plus three
injection hooks a real adapter must provide to run the push/error tests;
a new adapter reuses the whole suite by subclassing:

    class TestMammotionAdapterContract(_AdapterContract):
        @pytest.fixture
        async def mower(self, hass):
            return await make_mammotion_adapter(hass, ...)

        async def inject_error(self, mower, error):
            <drive the fake mammotion entity state that yields this error>

FakeMower implements the hooks trivially; the Mammotion adapter
(tickets #8-#10) inherits the same assertions with state-driven hooks.
The base class is underscore-prefixed so pytest skips it on its own.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from custom_components.mowing_director.adapter import (
    Accepted,
    AdapterSignal,
    Channel,
    CommandResult,
    Limits,
    MowerAdapter,
    NativeTask,
    NewError,
    RainStop,
    Refused,
    RunSettings,
    Snapshot,
    Unconfirmed,
    Zone,
)

SPEC_SNAPSHOT_FIELDS = {
    "activity",
    "available",
    "recharging",
    "job_active",
    "battery",
    "progress",
    "zone",
    "blade_height_mm",
    "movement",
    "job_ref",
    "rain",
    "self_check",
}

ERROR_TIME = datetime(2026, 10, 21, tzinfo=UTC)


class _AdapterContract:
    """Protocol invariants. Subclasses supply `mower` and the hooks.

    Underscore-prefixed so pytest does not collect the base (its `mower`
    fixture raises); real suites subclass it as `Test*`.
    """

    @pytest.fixture
    def mower(self) -> MowerAdapter:
        """The adapter under test. Must be overridden by a subclass."""
        raise NotImplementedError

    async def inject_error(self, mower: MowerAdapter, error: NewError) -> None:
        """Make the adapter observe `error`. Override per adapter."""
        pytest.skip("adapter does not support error injection")

    async def push_snapshot(self, mower: MowerAdapter, snapshot: Snapshot) -> None:
        """Make the adapter publish `snapshot`. Override per adapter."""
        pytest.skip("adapter does not support snapshot injection")

    def script_result(self, mower: MowerAdapter, command: str, result: CommandResult) -> None:
        """Force `command` to return `result`. Override per adapter."""
        pytest.skip("adapter does not support scripted results")

    async def test_implements_the_full_protocol_surface(self, mower: MowerAdapter) -> None:
        """Every protocol member exists - the seam's shape, not its behaviour."""
        assert isinstance(mower, MowerAdapter)

    async def test_snapshot_has_exactly_the_spec_fields(self, mower: MowerAdapter) -> None:
        """Snapshot carries exactly the spec §3.2 fields."""
        snapshot = mower.snapshot
        assert isinstance(snapshot, Snapshot)
        assert {f.name for f in dataclasses.fields(snapshot)} == SPEC_SNAPSHOT_FIELDS

    async def test_limits_descriptor_is_reported(self, mower: MowerAdapter) -> None:
        """Limits are reported per Mower (spec §3.6)."""
        assert isinstance(mower.limits, Limits)

    async def test_commands_return_the_closed_union(self, mower: MowerAdapter) -> None:
        """Every command returns accepted, refused(reason) or unconfirmed (§3.3)."""
        results: list[CommandResult] = [
            await mower.start_run("zone-1", _run_settings(mower)),
            await mower.abort_run(),
            await mower.dock(),
            await mower.resume_run(),
            await mower.refresh(),
            await mower.set_task_enabled("task-1", False),
        ]
        for result in results:
            assert isinstance(result, Accepted | Refused | Unconfirmed)
            assert result.kind is type(result).kind
            assert result.kind.value == type(result).__name__.lower()

    async def test_refused_carries_a_reason(self, mower: MowerAdapter) -> None:
        """refused is a definite no and always names a reason (§3.3)."""
        self.script_result(mower, "dock", Refused(reason="not_ready"))
        result = await mower.dock()
        assert isinstance(result, Refused)
        assert result.reason

    async def test_unconfirmed_is_returned_not_raised(self, mower: MowerAdapter) -> None:
        """A lost outcome comes back as unconfirmed, never an exception (§3.3)."""
        self.script_result(mower, "start_run", Unconfirmed())
        assert isinstance(await mower.start_run("zone-1", _run_settings(mower)), Unconfirmed)

    async def test_new_error_reaches_the_sink(self, mower: MowerAdapter) -> None:
        """A new error is pushed as NewError(code, message, time) (§3.2)."""
        received: list[AdapterSignal] = []
        unsubscribe = mower.subscribe(_Sink(received, []))
        try:
            await self.inject_error(
                mower, NewError(code="1003", message="rain sensor", time=ERROR_TIME)
            )
        finally:
            unsubscribe()
        errors = [s for s in received if isinstance(s, NewError)]
        assert errors == [NewError(code="1003", message="rain sensor", time=ERROR_TIME)]

    async def test_errors_are_deduped_by_time(self, mower: MowerAdapter) -> None:
        """The same error time is reported once (spec §3.2)."""
        received: list[AdapterSignal] = []
        unsubscribe = mower.subscribe(_Sink(received, []))
        try:
            await self.inject_error(mower, NewError(code="1003", message="rain", time=ERROR_TIME))
            await self.inject_error(mower, NewError(code="1003", message="rain", time=ERROR_TIME))
        finally:
            unsubscribe()
        errors = [s for s in received if isinstance(s, NewError)]
        assert len(errors) == 1

    async def test_rain_stop_reaches_the_sink(self, mower: MowerAdapter) -> None:
        """rain_stop is a signal of its own (§3.2)."""
        received: list[AdapterSignal] = []
        unsubscribe = mower.subscribe(_Sink(received, []))
        try:
            await self.inject_rain_stop(mower)
        finally:
            unsubscribe()
        assert any(isinstance(s, RainStop) for s in received)

    async def inject_rain_stop(self, mower: MowerAdapter) -> None:
        """Make the adapter report a rain stop. Override per adapter."""
        pytest.skip("adapter does not support rain-stop injection")

    async def test_snapshot_changes_reach_the_sink(self, mower: MowerAdapter) -> None:
        """Snapshot changes are pushed; `snapshot` also serves pull-at-load (A1)."""
        received: list[Snapshot] = []
        unsubscribe = mower.subscribe(_Sink([], received))
        try:
            await self.push_snapshot(mower, dataclasses.replace(mower.snapshot, battery=42))
        finally:
            unsubscribe()
        assert received
        assert received[-1] == mower.snapshot

    async def test_unsubscribe_stops_delivery(self, mower: MowerAdapter) -> None:
        """After unsubscribe the sink is silent."""
        signals: list[AdapterSignal] = []
        unsubscribe = mower.subscribe(_Sink(signals, []))
        unsubscribe()
        await self.inject_error(mower, NewError(code="1", message="x", time=ERROR_TIME))
        assert signals == []

    async def test_catalogues_have_neutral_shapes(self, mower: MowerAdapter) -> None:
        """Zones, Channels and native tasks use neutral refs only (§3.4, §3.1)."""
        zones = await mower.list_zones()
        assert zones and all(isinstance(z, Zone) for z in zones)
        assert all(z.native_ref and z.name for z in zones)
        assert all(z.polygon is None or isinstance(z.polygon, dict) for z in zones)

        channels = await mower.list_channels()
        assert channels and all(isinstance(c, Channel) for c in channels)
        ends = {z.native_ref for z in zones} | {Channel.DOCK}
        for channel in channels:
            assert channel.end_a in ends
            assert channel.end_b in ends

        tasks = await mower.list_native_tasks()
        assert tasks and all(isinstance(t, NativeTask) for t in tasks)
        assert all(t.task_id and t.name for t in tasks)

    async def test_lifecycle_and_metadata(self, mower: MowerAdapter) -> None:
        """start/close run; metadata is strings or None; redact is a list."""
        await mower.async_start()
        await mower.async_close()
        assert isinstance(mower.source_domain, str) and mower.source_domain
        for value in (mower.firmware_version, mower.model, mower.source_version):
            assert value is None or isinstance(value, str)
        assert isinstance(mower.redact, list)
        assert all(isinstance(item, str) for item in mower.redact)


def _run_settings(mower: MowerAdapter) -> RunSettings:
    """A RunSettings valid for this adapter's declared extras."""
    return RunSettings(
        blade_height_mm=70,
        bearing_deg=135.0,
        spacing_cm=22.0,
        speed_m_s=0.6,
        perimeter_laps=1,
        extras={field.key: field.default for field in mower.limits.extras_schema},
    )


class _Sink:
    """Sink that records pushes."""

    def __init__(self, signals: list[AdapterSignal], snapshots: list[Snapshot]) -> None:
        self._signals = signals
        self._snapshots = snapshots

    def on_snapshot(self, snapshot: Snapshot) -> None:
        """Receive a normalised snapshot push."""
        self._snapshots.append(snapshot)

    def on_signal(self, signal: AdapterSignal) -> None:
        """Receive a signal push."""
        self._signals.append(signal)
