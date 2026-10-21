"""Setup and housekeeping Repairs issues (spec §4.8) against FakeMower."""

from dataclasses import replace

from homeassistant.const import CONF_DEVICE
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mowing_director.adapter import NativeTask
from custom_components.mowing_director.const import DOMAIN
from custom_components.mowing_director.housekeeping import (
    ISSUE_CONFLICTING_AUTOMATIONS,
    ISSUE_DEVICE_NOT_FOUND,
    ISSUE_SOURCE_TOO_OLD,
    ISSUE_NATIVE_SCHEDULE,
    async_attach_housekeeping,
)
from tests.conftest import make_mammotion_mower
from tests.fake_mower import FakeMower


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def _loaded(hass, mower, device_id="d"):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_DEVICE: device_id})
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


async def _flow(hass, entry):
    from custom_components.mowing_director.repairs import async_create_fix_flow

    issue_id = ISSUE_NATIVE_SCHEDULE.format(entry.entry_id)
    flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
    flow.hass = hass
    return flow


async def test_native_fix_flow_disable_needs_explicit_confirmation(hass) -> None:
    mower = FakeMower()
    entry, _ = await _loaded(hass, mower)
    flow = await _flow(hass, entry)

    menu = await flow.async_step_init()
    assert menu["type"].value == "menu"
    assert menu["menu_options"] == ["disable", "ignore"]

    # Choosing "disable" only asks; nothing is sent until it is confirmed.
    form = await flow.async_step_disable()
    assert form["type"].value == "form"
    assert mower.commands == []

    done = await flow.async_step_disable({})
    assert done["type"].value == "create_entry"
    assert mower.commands == [("set_task_enabled", ("t-1", False))]
    assert _issue(hass, ISSUE_NATIVE_SCHEDULE.format(entry.entry_id)) is None


async def test_native_fix_flow_ignore_sends_nothing(hass) -> None:
    mower = FakeMower()
    entry, _ = await _loaded(hass, mower)
    flow = await _flow(hass, entry)
    result = await flow.async_step_ignore()
    assert result["type"].value == "abort"
    assert mower.commands == []
    assert _issue(hass, ISSUE_NATIVE_SCHEDULE.format(entry.entry_id)) is not None


async def _automation(hass, *automations):
    assert await async_setup_component(hass, "automation", {"automation": list(automations)})
    await hass.async_block_till_done()


def _watching(alias, entity_id):
    return {
        "alias": alias,
        "id": alias,
        "trigger": {"platform": "state", "entity_id": entity_id},
        "action": {"event": "noop"},
    }


async def test_conflicting_automation_raises_informational_issue(hass) -> None:
    _, mower_dev, _ = await make_mammotion_mower(hass)
    ent = er.async_entries_for_device(er.async_get(hass), mower_dev.id)[0]
    await _automation(hass, _watching("a1", ent.entity_id))
    entry, _ = await _loaded(hass, FakeMower(), mower_dev.id)
    issue = _issue(hass, ISSUE_CONFLICTING_AUTOMATIONS.format(entry.entry_id))
    assert issue is not None and not issue.is_fixable
    # Informational: it does not name any specific automation.
    assert "a1" not in str(issue.translation_placeholders)


async def test_no_automations_no_issue(hass) -> None:
    _, mower_dev, _ = await make_mammotion_mower(hass)
    entry, _ = await _loaded(hass, FakeMower(), mower_dev.id)
    assert _issue(hass, ISSUE_CONFLICTING_AUTOMATIONS.format(entry.entry_id)) is None


async def test_dismissed_issue_is_re_raised_only_when_the_set_changes(hass) -> None:
    _, mower_dev, _ = await make_mammotion_mower(hass)
    ent = er.async_entries_for_device(er.async_get(hass), mower_dev.id)[0]
    await _automation(hass, _watching("a1", ent.entity_id))
    entry, hk = await _loaded(hass, FakeMower(), mower_dev.id)
    issue_id = ISSUE_CONFLICTING_AUTOMATIONS.format(entry.entry_id)
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)

    await hk.async_check_automations()  # same set, e.g. next restart
    assert _issue(hass, issue_id).dismissed_version is not None

    # A second automation appears (automation reload is not under test).
    hk._conflicting_automations = lambda: ["automation.a1", "automation.a2"]
    await hk.async_check_automations()  # set changed
    assert _issue(hass, issue_id).dismissed_version is None


async def test_conflicting_set_is_stored_in_learned(hass) -> None:
    _, mower_dev, _ = await make_mammotion_mower(hass)
    ent = er.async_entries_for_device(er.async_get(hass), mower_dev.id)[0]
    await _automation(hass, _watching("a1", ent.entity_id))
    entry, _ = await _loaded(hass, FakeMower(), mower_dev.id)
    stored = await entry.runtime_data.async_load()
    assert len(stored["learned"]["conflicting_automations"]) == 1


async def test_missing_device_raises_issue_and_keeps_entry_and_store(hass) -> None:
    _, mower_dev, _ = await make_mammotion_mower(hass)
    entry, hk = await _loaded(hass, FakeMower(), mower_dev.id)
    issue_id = ISSUE_DEVICE_NOT_FOUND.format(entry.entry_id)
    assert _issue(hass, issue_id) is None

    dr.async_get(hass).async_remove_device(mower_dev.id)
    await hk.async_check_device()
    issue = _issue(hass, issue_id)
    assert issue is not None and issue.is_persistent is False
    assert hass.config_entries.async_get_entry(entry.entry_id) is not None
    assert await entry.runtime_data.async_load() is not None


async def test_old_mammotion_raises_non_blocking_issue(hass) -> None:
    entry, _ = await _loaded(hass, FakeMower(source_ver="0.6.17"))
    issue = _issue(hass, ISSUE_SOURCE_TOO_OLD.format(entry.entry_id))
    assert issue is not None and not issue.is_fixable
    assert issue.translation_placeholders["minimum"] == "0.6.18"


async def test_supported_or_unknown_version_raises_nothing(hass) -> None:
    entry, _ = await _loaded(hass, FakeMower(source_ver="0.6.18"))
    entry2, _ = await _loaded(hass, FakeMower(source_ver=None))
    assert _issue(hass, ISSUE_SOURCE_TOO_OLD.format(entry.entry_id)) is None
    assert _issue(hass, ISSUE_SOURCE_TOO_OLD.format(entry2.entry_id)) is None


async def test_issues_are_re_created_by_the_load_checks_after_restart(hass) -> None:
    mower = FakeMower(source_ver="0.6.10")
    entry, hk = await _loaded(hass, mower)
    ids = [
        ISSUE_NATIVE_SCHEDULE.format(entry.entry_id),
        ISSUE_SOURCE_TOO_OLD.format(entry.entry_id),
    ]
    # Non-persistent: a restart leaves nothing behind.
    for issue_id in ids:
        assert _issue(hass, issue_id).is_persistent is False
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    await hk.async_check_all()  # what a load runs
    assert all(_issue(hass, i) is not None for i in ids)
