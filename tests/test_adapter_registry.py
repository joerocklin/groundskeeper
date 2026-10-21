"""Adapter registry tests (spec §3.1).

The core obtains adapters from a registry keyed by source integration
domain. Phase 1 registers `mammotion` (read-only observations from
ticket #8), and the lookup helper is the contract.
"""

from __future__ import annotations

from custom_components.mowing_director.adapter import MowerAdapter
from custom_components.mowing_director.adapters import (
    ADAPTER_REGISTRY,
    get_adapter_factory,
)


def test_registry_has_mammotion() -> None:
    """Phase 1 registers the `mammotion` adapter (spec §3.1)."""
    assert set(ADAPTER_REGISTRY) == {"mammotion"}


def test_unknown_domain_has_no_factory() -> None:
    """A domain with no adapter returns None (config flow eligibility)."""
    assert get_adapter_factory("nonexistent") is None


def test_registered_factory_is_returned() -> None:
    """A factory registered under a domain is found by lookup."""

    def factory(hass, entry) -> MowerAdapter:  # type: ignore[no-untyped-def]
        raise NotImplementedError

    ADAPTER_REGISTRY["fake"] = factory
    try:
        assert get_adapter_factory("fake") is factory
    finally:
        del ADAPTER_REGISTRY["fake"]
