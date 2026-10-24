"""Adapter registry keyed by source integration domain (spec §3.1).

A factory takes the HomeAssistant instance and the Groundskeeper
config entry and returns a live (unstarted) adapter for that Mower.
Phase 1 registers only `mammotion` (read-only observations from ticket
#8; commands land in #10). `const.ADAPTER_DOMAINS` remains the
eligibility list for the config flow.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from ..adapter import MowerAdapter

from .mammotion import adapter_factory as _mammotion_factory

if TYPE_CHECKING:
    # Imported for typing only: `__init__.py` will look adapters up at entry
    # setup, so a runtime import here would be circular.
    from .. import GroundskeeperConfigEntry

type AdapterFactory = Callable[[HomeAssistant, GroundskeeperConfigEntry], MowerAdapter]

ADAPTER_REGISTRY: dict[str, AdapterFactory] = {"mammotion": _mammotion_factory}


def get_adapter_factory(source_domain: str) -> AdapterFactory | None:
    """Return the adapter factory for a source domain, or None if unsupported."""
    return ADAPTER_REGISTRY.get(source_domain)
