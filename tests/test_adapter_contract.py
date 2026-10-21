"""Run the contract suite against FakeMower (issue #2 AC)."""

from __future__ import annotations

import pytest

from custom_components.mowing_director.adapter import (
    AdapterSignal,
    CommandResult,
    MowerAdapter,
    NewError,
    RainStop,
    Snapshot,
)
from tests.adapter_contract import _AdapterContract
from tests.fake_mower import FakeMower


class TestFakeMowerContract(_AdapterContract):
    """FakeMower must satisfy the full adapter contract."""

    @pytest.fixture
    def mower(self) -> MowerAdapter:
        """A fresh FakeMower with default catalogues."""
        return FakeMower()

    async def inject_error(self, mower: MowerAdapter, error: NewError) -> None:
        """FakeMower takes signals directly."""
        assert isinstance(mower, FakeMower)
        mower.emit_signal(error)

    async def inject_rain_stop(self, mower: MowerAdapter) -> None:
        """FakeMower takes signals directly."""
        assert isinstance(mower, FakeMower)
        mower.emit_signal(RainStop())

    async def push_snapshot(self, mower: MowerAdapter, snapshot: Snapshot) -> None:
        """FakeMower takes snapshots directly."""
        assert isinstance(mower, FakeMower)
        mower.emit_snapshot(snapshot)

    def script_result(
        self, mower: MowerAdapter, command: str, result: CommandResult
    ) -> None:
        """FakeMower scripts per-command results."""
        assert isinstance(mower, FakeMower)
        mower.script_result(command, result)
