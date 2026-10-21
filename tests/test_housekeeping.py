"""Setup and housekeeping Repairs issues (spec §4.8) against FakeMower."""

from dataclasses import replace

from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director.adapter import NativeTask
from custom_components.mowing_director.const import DOMAIN
from custom_components.mowing_director.housekeeping import (
    ISSUE_NATIVE_SCHEDULE,
    async_attach_housekeeping,
)
from tests.fake_mower import FakeMower


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def _loaded(hass, mower, device_id="d"):
    entry = MockConfigEntry(domain=DOMAIN, data={"device_id": device_id})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hk = async_attach_housekeeping(hass, entry, mower, entry.runtime_data)
    await hk.async_check_all()
    return entry, hk


async def test_enabled_native_task_raises_fixable_issue(hass) -> None:
    entry, _ = await _loaded(hass, FakeMower())
    issue = _issue(hass, ISSUE_NATIVE_SCHEDULE.format(entry.entry_id))
    assert issue is not None and issue.is_fixable
    assert issue.is_persistent is False
    assert issue.translation_placeholders == {"tasks": "Weekday mow"}


async def test_native_schedule_issue_clears_when_no_task_enabled(hass) -> None:
    mower = FakeMower()
    entry, hk = await _loaded(hass, mower)
    mower.native_tasks = tuple(
        replace(t, enabled=False) for t in mower.native_tasks
    )
    await hk.async_check_native_schedule()
    assert _issue(hass, ISSUE_NATIVE_SCHEDULE.format(entry.entry_id)) is None
