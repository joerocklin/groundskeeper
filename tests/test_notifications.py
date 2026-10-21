"""HA integration tests for phone notifications (spec §12, ticket #24).

`notify.mobile_app_*` services are fakes recording their calls; taps are
simulated `mobile_app_notification_action` events. Nothing real is called.
"""

from datetime import datetime, timezone

import pytest
from homeassistant.core import ServiceCall
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_mock_service,
)

from custom_components.mowing_director.const import DOMAIN, EVENT_TYPE
from custom_components.mowing_director.runtime import async_get_director
from tests.test_prompts import fail_a_run, list_prompts, types  # noqa: F401
from tests.test_on_hold_ha import hold_run  # noqa: F401
from tests.test_tracer_bullet import (  # noqa: F401
    MOWING,
    add_plan,
    advance,
    director,
    state_of,
)
from custom_components.mowing_director.adapter import ZoneTaskComplete

from homeassistant.components import persistent_notification


def _pn(hass):
    return persistent_notification._async_get_or_create_notifications(hass)


@pytest.fixture(autouse=True)
def _real_work_hours(monkeypatch):
    """Reminders follow the real default window, not conftest's whole-day one."""
    monkeypatch.setattr(
        "custom_components.mowing_director.checks.DEFAULT_WINDOW", ("08:00", "00:00")
    )


PIXEL, IPHONE = "mobile_app_pixel_8", "mobile_app_iphone"


async def ws_call(hass, hass_ws_client, type_, **fields):
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"mowing_director/{type_}", **fields})
    return await ws.receive_json()


@pytest.fixture
async def phones(hass, director, hass_ws_client):
    """Two fake phones configured as notify targets."""
    entry, mower, _ = director
    calls = {PIXEL: async_mock_service(hass, "notify", PIXEL),
             IPHONE: async_mock_service(hass, "notify", IPHONE)}
    msg = await ws_call(hass, hass_ws_client, "set_notify_settings",
                        entry_id=entry.entry_id, targets=[PIXEL, IPHONE])
    assert msg["success"], msg
    return calls


def tap(hass, prompt_id, action):
    hass.bus.async_fire(
        "mobile_app_notification_action",
        {"action": f"MOWDIR_{prompt_id}_{action.upper()}"},
    )


def sent(calls):
    """Notifications (not clears) one phone received."""
    return [c.data for c in calls if c.data["message"] != "clear_notification"]


def cleared(calls):
    return [c.data for c in calls if c.data["message"] == "clear_notification"]


# -- targets -------------------------------------------------------------------------


async def test_notify_targets_are_stored_in_config_and_readable(
    hass, director, hass_ws_client, hass_storage
) -> None:
    entry, _, _ = director
    msg = await ws_call(hass, hass_ws_client, "set_notify_settings",
                        entry_id=entry.entry_id, targets=[PIXEL])
    assert msg["success"]
    msg = await ws_call(hass, hass_ws_client, "get_notify_settings", entry_id=entry.entry_id)
    assert msg["result"]["targets"] == [PIXEL]
    assert msg["result"]["categories"] == {
        "run_failed": True, "run_on_hold": True, "check_waits": True,
        "is_raining": True, "run_started": False, "run_done": False,
        "schedule_completed": True,
    }
    stored = hass_storage[f"mowing_director.{entry.entry_id}"]["data"]["config"]
    assert stored["notify_targets"] == [PIXEL]


async def test_failure_and_on_hold_toggles_cannot_be_turned_off(
    hass, director, hass_ws_client
) -> None:
    entry, _, _ = director
    msg = await ws_call(hass, hass_ws_client, "set_notify_settings",
                        entry_id=entry.entry_id, categories={"run_failed": False})
    assert not msg["success"]


# -- sending -------------------------------------------------------------------------


async def test_failure_prompt_goes_to_every_target_with_actions(
    hass, director, phones, freezer
) -> None:
    entry, mower, _ = director
    await fail_a_run(hass, mower, freezer)
    [prompt] = async_get_director(hass, entry.entry_id).state["prompts"]
    for calls in phones.values():
        [note] = sent(calls)
        assert note["title"] == "Example Mower: Run failed"
        assert note["message"].splitlines()[0] == "Zone: Front Lawn"
        data = note["data"]
        assert data["tag"] == f"mowdir_{entry.entry_id}_run_failed"
        assert [a["action"] for a in data["actions"]] == [
            f"MOWDIR_{prompt['prompt_id']}_RETRY",
            f"MOWDIR_{prompt['prompt_id']}_SKIP_PLAN",
            f"MOWDIR_{prompt['prompt_id']}_ABORT",
        ]
        assert [a["title"] for a in data["actions"]] == ["Retry", "Skip Plan", "Abort"]
        assert data["actions"][2]["destructive"] is True
        assert "destructive" not in data["actions"][0]
        assert "authenticationRequired" not in str(data)
        assert data["push"]["interruption-level"] == "time-sensitive"
        assert data["priority"] == "high"
        assert data["channel"] == "Mowing Director: Run failed"
        assert data["clickAction"].startswith("/mowing-director")
        assert data["url"] == data["clickAction"]


async def test_on_hold_prompt_is_urgent_too(hass, director, phones, freezer) -> None:
    _, mower, _ = director
    await hold_run(hass, mower, freezer)
    [note] = sent(phones[PIXEL])
    assert note["title"] == "Example Mower: Run on hold"
    assert note["data"]["push"]["interruption-level"] == "time-sensitive"
    assert [a["title"] for a in note["data"]["actions"]] == ["Resume", "End Run"]
    assert note["data"]["actions"][1]["destructive"] is True


# -- taps ----------------------------------------------------------------------------


async def test_first_tap_decides_and_every_copy_is_cleared_by_tag(
    hass, director, phones, freezer
) -> None:
    entry, mower, _ = director
    await fail_a_run(hass, mower, freezer)
    [prompt] = async_get_director(hass, entry.entry_id).state["prompts"]
    tap(hass, prompt["prompt_id"], "skip_plan")
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.example_mower_schedule").state == "idle"
    for calls in phones.values():
        [clear] = cleared(calls)
        assert clear["data"] == {"tag": prompt["tag"]}


async def test_duplicate_and_stale_taps_are_ignored_and_send_nothing(
    hass, director, phones, freezer
) -> None:
    entry, mower, _ = director
    await fail_a_run(hass, mower, freezer)
    [prompt] = async_get_director(hass, entry.entry_id).state["prompts"]
    tap(hass, prompt["prompt_id"], "retry")
    await hass.async_block_till_done()
    before = {k: len(v) for k, v in phones.items()}
    events = async_capture_events(hass, EVENT_TYPE)
    tap(hass, prompt["prompt_id"], "abort")  # duplicate from the second phone
    tap(hass, "01NOSUCHPROMPT", "retry")  # stale
    await hass.async_block_till_done()
    assert len(types(events, "prompt_ignored")) == 2
    assert {k: len(v) for k, v in phones.items()} == before
    assert state_of(hass, "sensor.example_mower_schedule").state == "waiting"  # retry stood


async def test_unrelated_action_events_are_left_alone(hass, director, phones) -> None:
    events = async_capture_events(hass, EVENT_TYPE)
    hass.bus.async_fire("mobile_app_notification_action", {"action": "OTHER_THING"})
    await hass.async_block_till_done()
    assert types(events, "prompt_ignored") == []


@pytest.mark.parametrize("path", ["service", "answer_prompt"])
async def test_answering_by_any_path_clears_phones(
    hass, director, phones, freezer, path
) -> None:
    entry, mower, _ = director
    await fail_a_run(hass, mower, freezer)
    [prompt] = async_get_director(hass, entry.entry_id).state["prompts"]
    if path == "service":
        await hass.services.async_call(DOMAIN, "retry_run", {}, blocking=True)
    else:
        await hass.services.async_call(
            DOMAIN, "answer_prompt",
            {"prompt_id": prompt["prompt_id"], "action": "retry"}, blocking=True)
    for calls in phones.values():
        assert [c["data"] for c in cleared(calls)] == [{"tag": prompt["tag"]}]


# -- no phone ------------------------------------------------------------------------


async def test_without_targets_failure_raises_a_persistent_notification(
    hass, director, freezer
) -> None:
    entry, mower, _ = director
    await fail_a_run(hass, mower, freezer)
    [prompt] = async_get_director(hass, entry.entry_id).state["prompts"]
    note = _pn(hass)[prompt["tag"]]
    assert note["title"] == "Example Mower: Run failed"
    assert "/mowing-director" in note["message"]
    await hass.services.async_call(DOMAIN, "skip_plan", {}, blocking=True)
    assert prompt["tag"] not in _pn(hass)


async def test_with_a_phone_no_persistent_notification(hass, director, phones, freezer) -> None:
    entry, mower, _ = director
    await fail_a_run(hass, mower, freezer)
    assert not _pn(hass)


# -- notices -------------------------------------------------------------------------


async def run_to_done(hass, mower, freezer):
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    freezer.tick(900)
    mower.emit_signal(ZoneTaskComplete("11111111"))
    await hass.async_block_till_done()


async def test_schedule_completed_summary_is_on_by_default_and_run_notices_are_off(
    hass, director, phones, freezer
) -> None:
    _, mower, _ = director
    await run_to_done(hass, mower, freezer)
    [note] = sent(phones[PIXEL])
    assert note["title"] == "Example Mower: Schedule completed"
    assert note["message"].splitlines() == [
        "Plans: 1",
        "Runs done: 1, skipped: 0",
        "Mowing time: 15 min",
        "Anomalies: 0",
    ]
    assert "actions" not in note["data"]
    assert "push" not in note["data"]


async def test_run_started_and_done_notices_follow_their_toggles(
    hass, director, phones, freezer, hass_ws_client
) -> None:
    entry, mower, _ = director
    msg = await ws_call(hass, hass_ws_client, "set_notify_settings", entry_id=entry.entry_id,
                        categories={"run_started": True, "run_done": True,
                                    "schedule_completed": False})
    assert msg["success"]
    await run_to_done(hass, mower, freezer)
    titles = [n["title"] for n in sent(phones[PIXEL])]
    assert titles == ["Example Mower: Run started", "Example Mower: Run done"]


async def test_check_wait_notice_respects_its_toggle(
    hass, director, phones, hass_ws_client
) -> None:
    entry, _, _ = director
    d = async_get_director(hass, entry.entry_id)
    await d.notifier.async_notice("check_waits", "check_timeout", "Check timed out",
                                  ["Wet grass has been failing for 12 h."], subject="wet_grass")
    [note] = sent(phones[PIXEL])
    assert note["data"]["tag"] == f"mowdir_{entry.entry_id}_check_timeout_wet_grass"
    await ws_call(hass, hass_ws_client, "set_notify_settings", entry_id=entry.entry_id,
                  categories={"check_waits": False})
    await d.notifier.async_notice("check_waits", "check_timeout", "Check timed out", ["x"])
    assert len(sent(phones[PIXEL])) == 1


# -- reminders -----------------------------------------------------------------------


async def test_reminders_replace_by_tag_and_skip_the_night(
    hass, director, phones, freezer
) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc))
    entry, mower, _ = director
    # a docked battery flat for hours is a separate Anomaly; keep it out of the count
    async_get_director(hass, entry.entry_id).data["config"]["anomaly_actions"] = {
        "charge_not_rising": "off"}
    await fail_a_run(hass, mower, freezer)
    [prompt] = async_get_director(hass, entry.entry_id).state["prompts"]
    assert len(sent(phones[PIXEL])) == 1

    await advance(hass, freezer, 3600)  # +1 h: 21:01
    notes = sent(phones[PIXEL])
    assert len(notes) == 2
    assert notes[1]["data"]["tag"] == notes[0]["data"]["tag"]
    assert notes[1]["data"]["actions"] == notes[0]["data"]["actions"]  # same prompt_id
    assert notes[1]["data"]["push"]["interruption-level"] == "time-sensitive"

    await advance(hass, freezer, 6 * 3600)  # 03:01, overnight
    assert len(sent(phones[PIXEL])) == 2
    await advance(hass, freezer, 5 * 3600)  # 08:01: morning
    assert len(sent(phones[PIXEL])) == 3
    await advance(hass, freezer, 24 * 3600)
    assert len(sent(phones[PIXEL])) == 4


async def test_first_notification_goes_out_at_any_hour(hass, director, phones, freezer) -> None:
    from custom_components.mowing_director.core import RaisePrompt

    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc))
    entry, _, _ = director
    d = async_get_director(hass, entry.entry_id)
    await d.dispatch(lambda now: RaisePrompt(
        now, "run_failed", None, ["retry", "skip_plan", "abort"], {"reason": "error"}))
    assert len(sent(phones[PIXEL])) == 1


async def test_restart_resends_nothing_and_catches_up_with_one_reminder(
    hass, director, phones, freezer
) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))
    entry, mower, _ = director
    await fail_a_run(hass, mower, freezer)
    assert len(sent(phones[PIXEL])) == 1
    await hass.config_entries.async_unload(entry.entry_id)
    freezer.tick(5 * 3600)  # reminder fell due while down, still in the window
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    notes = sent(phones[PIXEL])
    assert len(notes) == 2  # exactly one catch-up reminder, no re-send of the first
    assert notes[1]["data"]["tag"] == notes[0]["data"]["tag"]


# -- delivery failure ----------------------------------------------------------------


async def test_delivery_failure_fires_an_event_and_does_not_retry(
    hass, director, hass_ws_client, freezer
) -> None:
    entry, mower, _ = director
    await ws_call(hass, hass_ws_client, "set_notify_settings", entry_id=entry.entry_id,
                  targets=["mobile_app_gone", PIXEL])
    good = async_mock_service(hass, "notify", PIXEL)
    events = async_capture_events(hass, EVENT_TYPE)
    await fail_a_run(hass, mower, freezer)
    [failure] = types(events, "notification_failed")
    assert failure["target"] == "mobile_app_gone"
    assert len(sent(good)) == 1  # the other phone still got it
    [prompt] = await list_prompts(hass, hass_ws_client, entry)  # panel card unaffected
    assert prompt["kind"] == "run_failed"


async def test_check_timeout_event_becomes_a_check_wait_notice(
    hass, director, phones, hass_ws_client
) -> None:
    entry, _, _ = director
    d = async_get_director(hass, entry.entry_id)
    await d.notifier.async_handle(
        "check_timeout",
        {"check_id": "wet_grass", "name": "Wet grass", "policy": "notify",
         "reasons": ["rain_detected"]},
    )
    [note] = sent(phones[PIXEL])
    assert note["title"] == "Example Mower: Check timed out"
    assert note["message"] == "Wet grass is blocking the Schedule: rain_detected"
    assert note["data"]["tag"] == f"mowdir_{entry.entry_id}_check_timeout_wet_grass"


async def test_gate_urgent_resend_is_time_sensitive_but_the_first_warning_is_not(
    hass, director, phones
) -> None:
    from custom_components.mowing_director.core import RaisePrompt

    entry, _, _ = director
    d = async_get_director(hass, entry.entry_id)
    ctx = {"gate_id": "g1", "gate_name": "Fence gate", "crossing": "back"}
    for urgent in (False, True):
        await d.dispatch(lambda now, u=urgent: RaisePrompt(
            now, "gate", "g1", ("opened", "closed"), {**ctx, "urgent": u}))
        await hass.async_block_till_done()
    first, second = sent(phones[PIXEL])
    assert "push" not in first["data"]
    assert second["data"]["push"]["interruption-level"] == "time-sensitive"
    assert second["data"]["priority"] == "high"
    assert "come back through this Gate" in second["message"]
    assert first["data"]["tag"] == second["data"]["tag"]  # replaces the earlier one
