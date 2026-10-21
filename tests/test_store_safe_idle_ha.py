"""HA integration: Store safe idle and discarded state with FakeMower (#31)."""

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.mowing_director.const import DOMAIN
from tests.test_reconcile_ha import (  # noqa: F401
    CURRENT,
    SCHEDULE,
    commands,
    mowing_run,
)
from tests.test_tracer_bullet import MOWING, add_plan, advance, director, state_of  # noqa: F401


def safe_idle_issue(hass, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"store_safe_idle_{entry.entry_id}")


async def restart_with(hass, hass_storage, entry, mutate):
    assert await hass.config_entries.async_unload(entry.entry_id)
    mutate(hass_storage[f"mowing_director.{entry.entry_id}"])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_invalid_config_is_safe_idle_never_commands_and_never_writes(
    hass, hass_storage, director, freezer
) -> None:
    entry, mower, _ = director
    await mowing_run(hass, mower, freezer)
    key = f"mowing_director.{entry.entry_id}"

    def corrupt(stored):
        stored["data"]["config"]["zones"] = "garbage"

    await restart_with(hass, hass_storage, entry, corrupt)
    before = repr(hass_storage[key])
    assert state_of(hass, SCHEDULE).state == "idle"
    assert state_of(hass, CURRENT).state == "none"
    assert safe_idle_issue(hass, entry) is not None
    assert safe_idle_issue(hass, entry).is_fixable
    with pytest.raises(HomeAssistantError):
        await add_plan(hass, start=True)
    await advance(hass, freezer, 600)
    assert commands(mower) == ["start_run"]  # only the pre-restart command
    assert repr(hass_storage[key]) == before


async def test_newer_store_version_is_safe_idle(hass, hass_storage, director) -> None:
    entry, mower, _ = director
    await restart_with(hass, hass_storage, entry, lambda s: s.update(version=2))
    assert safe_idle_issue(hass, entry) is not None
    assert state_of(hass, SCHEDULE).state == "idle"
    assert hass_storage[f"mowing_director.{entry.entry_id}"]["version"] == 2


async def test_start_fresh_repair_flow_unlocks_and_clears_the_issue(
    hass, hass_storage, director
) -> None:
    from custom_components.mowing_director.repairs import async_create_fix_flow

    entry, mower, _ = director
    await restart_with(
        hass, hass_storage, entry, lambda s: s["data"]["config"].update(zones="x")
    )
    issue = safe_idle_issue(hass, entry)
    flow = await async_create_fix_flow(hass, issue.issue_id, issue.data)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "form"  # confirm before wiping
    result = await flow.async_step_init({})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert safe_idle_issue(hass, entry) is None
    assert hass_storage[f"mowing_director.{entry.entry_id}"]["data"]["config"]["recurrences"] == []
    assert state_of(hass, SCHEDULE).state == "idle"
    await add_plan(hass)  # works again


async def test_only_state_invalid_keeps_config_and_never_adopts_the_live_job(
    hass, hass_storage, director, freezer
) -> None:
    entry, mower, _ = director
    await mowing_run(hass, mower, freezer)
    await restart_with(
        hass, hass_storage, entry, lambda s: s["data"]["state"].update(runs="garbage")
    )
    assert safe_idle_issue(hass, entry) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"store_unusable_{entry.entry_id}")
    assert state_of(hass, SCHEDULE).state == "idle"
    assert state_of(hass, CURRENT).state == "none"
    assert commands(mower) == ["start_run"]
    await add_plan(hass)  # Store is writable: config preserved and usable


async def test_corrupt_file_none_is_never_treated_as_nothing_in_flight(
    hass, hass_storage, director, freezer
) -> None:
    entry, mower, _ = director
    key = f"mowing_director.{entry.entry_id}"
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass_storage.pop(key)  # HA renamed the corrupt file away and returned None
    ir.async_create_issue(
        hass, "homeassistant", f"storage_corruption_{key}_2026-01-01T00:00:00",
        is_fixable=True, severity=ir.IssueSeverity.CRITICAL,
        translation_key="storage_corruption",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert safe_idle_issue(hass, entry) is not None
    assert key not in hass_storage
    assert commands(mower) == []
