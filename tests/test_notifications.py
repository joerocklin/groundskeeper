"""HA integration tests for phone notifications (spec §12, ticket #24).

`notify.mobile_app_*` services are fakes recording their calls; taps are
simulated `mobile_app_notification_action` events. Nothing real is called.
"""

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.core import ServiceCall
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_mock_service,
)

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_DEVICE

from custom_components.groundskeeper import adapters
from custom_components.groundskeeper.const import DOMAIN, EVENT_TYPE
from custom_components.groundskeeper.runtime import async_get_director
from tests.conftest import make_mammotion_mower
from tests.test_prompts import fail_a_run, list_prompts, types  # noqa: F401
from tests.test_on_hold_ha import hold_run  # noqa: F401
from tests.test_tracer_bullet import (  # noqa: F401
    MOWING,
    add_plan,
    advance,
    RecordingMower,
    director,
    state_of,
)
from custom_components.groundskeeper.adapter import NewError, ZoneTaskComplete

from homeassistant.components import persistent_notification


def _pn(hass):
    return persistent_notification._async_get_or_create_notifications(hass)


@pytest.fixture
def real_work_hours(monkeypatch):
    """Reminders follow the real default window, not conftest's whole-day one.
    Only for tests that pin the timezone and clock; the rest must not depend on
    the wall clock."""
    monkeypatch.setattr(
        "custom_components.groundskeeper.checks.DEFAULT_WINDOW", ("08:00", "00:00")
    )


PIXEL, IPHONE = "mobile_app_pixel_8", "mobile_app_iphone"


async def ws_call(hass, hass_ws_client, type_, **fields):
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"groundskeeper/{type_}", **fields})
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
    stored = hass_storage[f"groundskeeper.{entry.entry_id}"]["data"]["config"]
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
        assert re.fullmatch(r"mowdir_[0-9a-f]{10}_run_failed", data["tag"])
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
        assert data["channel"] == "Groundskeeper: Run failed"
        assert data["clickAction"].startswith("/groundskeeper")
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
        [pushed] = sent(calls)
        assert clear["data"]["tag"] == pushed["data"]["tag"]  # clears what it sent


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
    assert "/groundskeeper" in note["message"]
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
    assert re.fullmatch(r"mowdir_[0-9a-f]{10}_check_timeout_wet_grass", note["data"]["tag"])
    await ws_call(hass, hass_ws_client, "set_notify_settings", entry_id=entry.entry_id,
                  categories={"check_waits": False})
    await d.notifier.async_notice("check_waits", "check_timeout", "Check timed out", ["x"])
    assert len(sent(phones[PIXEL])) == 1


# -- reminders -----------------------------------------------------------------------


async def test_reminders_replace_by_tag_and_skip_the_night(
    hass, director, phones, freezer, real_work_hours
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


async def test_first_notification_goes_out_at_any_hour(hass, director, phones, freezer, real_work_hours) -> None:
    from custom_components.groundskeeper.core import RaisePrompt

    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc))
    entry, _, _ = director
    d = async_get_director(hass, entry.entry_id)
    await d.dispatch(lambda now: RaisePrompt(
        now, "run_failed", None, ["retry", "skip_plan", "abort"], {"reason": "error"}))
    assert len(sent(phones[PIXEL])) == 1


async def test_restart_resends_nothing_and_catches_up_with_one_reminder(
    hass, director, phones, freezer, real_work_hours
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
    assert re.fullmatch(r"mowdir_[0-9a-f]{10}_check_timeout_wet_grass", note["data"]["tag"])


@pytest.mark.parametrize(
    ("blocking", "status_line"),
    [
        (True, "The Mower still reads error, so the Schedule waits."),
        (False, "The Mower no longer reads error, so the Schedule goes on."),
    ],
    ids=["blocking", "not_blocking"],
)
async def test_mower_error_between_runs_notice_names_the_error_and_the_schedule(
    hass, director, phones, blocking, status_line
) -> None:
    """Issue #110: the notice says which error and what the Schedule does."""
    entry, _, _ = director
    d = async_get_director(hass, entry.entry_id)
    await d.notifier.async_handle(
        "check_timeout",
        {"check_id": "mower_ready", "name": "Mower ready", "policy": "notify",
         "reasons": ["new_error"], "blocking": blocking,
         "error": {"code": "2748", "message": "blocked",
                   "time": "2026-11-17T21:00:00+00:00"}},
    )
    [note] = sent(phones[PIXEL])
    assert note["title"] == "Example Mower: Mower reported an error"
    assert note["message"].splitlines() == ["Error 2748: blocked", status_line]
    assert re.fullmatch(r"mowdir_[0-9a-f]{10}_check_timeout_mower_ready", note["data"]["tag"])
    assert "actions" not in note["data"]  # a notice, never a Prompt


async def test_mower_error_between_runs_reaches_the_phone_from_the_mower(
    hass, director, phones
) -> None:
    """End to end: a Schedule waiting with the Mower idle gets one error notice."""
    entry, mower, _ = director
    await add_plan(hass, start=True)
    when = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)
    mower.emit_signal(NewError("2748", "blocked", when))
    await hass.async_block_till_done()
    [note] = sent(phones[PIXEL])
    assert note["title"] == "Example Mower: Mower reported an error"
    assert note["message"].splitlines() == [
        "Error 2748: blocked",
        "The Mower no longer reads error, so the Schedule goes on.",
    ]


async def test_error_while_still_mowing_is_a_plain_notice_and_the_run_goes_on(
    hass, director, phones, freezer
) -> None:
    """Issue #96: the Mower logged an error and kept mowing (Stage 3, 2026-11-16)."""
    entry, mower, _ = director
    events = async_capture_events(hass, EVENT_TYPE)
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    when = datetime(2026, 11, 16, 21, 7, 58, tzinfo=timezone.utc)
    mower.emit_signal(NewError("11132", "Blade stopped: wheel slip detected while reversing", when))
    await hass.async_block_till_done()

    d = async_get_director(hass, entry.entry_id)
    assert [r["status"] for r in d.data["state"]["runs"]] == ["mowing"]
    assert d.state["prompts"] == []
    [tolerated] = [e.data for e in events if e.data["type"] == "error_tolerated"]
    assert tolerated["code"] == "11132"
    assert tolerated["zone_name"] == "Front Lawn"
    assert tolerated["time"] == when.isoformat()
    for calls in phones.values():
        [note] = sent(calls)
        assert note["title"] == "Example Mower: Mower reported an error"
        assert note["message"].splitlines() == [
            "Zone: Front Lawn",
            "Error 11132: Blade stopped: wheel slip detected while reversing",
            "The Mower is still mowing, so the Run goes on.",
        ]
        assert "actions" not in note["data"]  # a notice, never a Prompt
        assert "push" not in note["data"]
        assert re.fullmatch(r"mowdir_[0-9a-f]{10}_error_tolerated_[0-9a-f]{10}", note["data"]["tag"])


async def test_error_after_the_mower_stopped_still_fails_the_run_with_a_prompt(
    hass, director, phones, freezer
) -> None:
    entry, mower, _ = director
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)
    mower.emit_snapshot(MOWING)
    await hass.async_block_till_done()
    mower.emit_snapshot(replace(MOWING, activity=LawnMowerActivity.ERROR))
    await hass.async_block_till_done()
    # Fresh: stamped after the last reading of mowing (an older one is tolerated, #135).
    stamped = datetime.now(timezone.utc) + timedelta(seconds=1)
    mower.emit_signal(NewError("1008", "Robot is locked", stamped))
    await hass.async_block_till_done()
    d = async_get_director(hass, entry.entry_id)
    assert [r["status"] for r in d.data["state"]["runs"]] == ["failed"]
    assert [p["kind"] for p in d.state["prompts"]] == ["run_failed"]
    [note] = sent(phones[PIXEL])
    assert note["title"] == "Example Mower: Run failed"


def test_logbook_describes_a_tolerated_error() -> None:
    from custom_components.groundskeeper.logbook import describe

    text = describe({"type": "error_tolerated", "code": "11132", "message": "wheel slip",
                     "zone_name": "Front Lawn"})
    assert text == "Mower reported error 11132 (wheel slip) but is still mowing Front Lawn"


async def test_gate_urgent_resend_is_time_sensitive_but_the_first_warning_is_not(
    hass, director, phones
) -> None:
    from custom_components.groundskeeper.core import RaisePrompt

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


async def test_the_close_prompt_of_a_manual_gate_has_a_left_open_button_that_answers_it(
    hass, director, phones
) -> None:
    from custom_components.groundskeeper.core import RaisePrompt

    from custom_components.groundskeeper.prompts import async_prompt_strings

    # The button falls back to a capitalised action name, so check the real string.
    assert (await async_prompt_strings(hass))["prompt_action_left_open"] == "Left open"
    entry, _, _ = director
    d = async_get_director(hass, entry.entry_id)
    ctx = {"gate_id": "g1", "gate_name": "Fence gate"}
    await d.dispatch(lambda now: RaisePrompt(now, "gate_close", "g1", ["closed", "left_open"], ctx))
    await hass.async_block_till_done()
    [note] = sent(phones[PIXEL])
    assert note["title"] == "Example Mower: Through, close Fence gate"
    [prompt] = d.state["prompts"]
    assert [a["title"] for a in note["data"]["actions"]] == ["Closed", "Left open"]
    assert [a["action"] for a in note["data"]["actions"]] == [
        f"MOWDIR_{prompt['prompt_id']}_CLOSED", f"MOWDIR_{prompt['prompt_id']}_LEFT_OPEN"]
    events = async_capture_events(hass, EVENT_TYPE)
    tap(hass, prompt["prompt_id"], "left_open")
    await hass.async_block_till_done()
    [answered] = types(events, "prompt_answered")
    assert answered["action"] == "left_open"
    assert d.state["prompts"] == []
    assert [c["data"]["tag"] for c in cleared(phones[PIXEL])] == [prompt["tag"]]


# -- push tags (#89) -----------------------------------------------------------------

REAL_ID = "01M5QSXDJJG7AWR520WCC3Z314"  # a 26-character ULID, like real Gate, Run and Check ids


async def test_every_push_tag_fits_the_64_character_limit(
    hass, director, phones, freezer, hass_ws_client
) -> None:
    """Gate Prompts and the Run notices went out with 65 to 72 character tags and never
    arrived on a real phone: measured live, 64 arrives and 65 does not. Fixtures with
    short ids like "g1" hid it, so every kind is sent here with real-length ids."""
    from custom_components.groundskeeper.core import RaisePrompt

    entry, mower, _ = director
    assert len(entry.entry_id) == 26  # a real-length entry id
    d = async_get_director(hass, entry.entry_id)
    msg = await ws_call(hass, hass_ws_client, "set_notify_settings", entry_id=entry.entry_id,
                        categories={"run_started": True, "run_done": True})
    assert msg["success"]

    await run_to_done(hass, mower, freezer)  # run started, run done, schedule completed
    await d.notifier.async_handle(
        "check_timeout",
        {"check_id": REAL_ID, "name": "Custom", "policy": "notify", "reasons": ["x"]},
    )
    for kind, actions in (
        ("gate", ("opened", "closed")),
        ("gate_close", ("closed",)),
        ("run_failed", ("retry", "skip_plan", "abort")),
        ("run_on_hold", ("resume", "end_run")),
        ("is_raining", ("no", "yes")),
        ("anomaly", ("dismiss",)),
    ):
        await d.dispatch(lambda now, k=kind, a=actions: RaisePrompt(
            now, k, REAL_ID, list(a), {"gate_name": "Fence gate", "crossing": "out"}))
        await hass.async_block_till_done()

    titles = [n["title"] for n in sent(phones[PIXEL])]
    assert len(titles) == 10, titles  # 3 run notices, 1 check wait, 6 prompts
    too_long = {n["title"]: len(n["data"]["tag"]) for n in sent(phones[PIXEL])
                if len(n["data"]["tag"]) > 64}
    assert too_long == {}


@pytest.fixture
async def two_directors(hass, hass_storage, monkeypatch):
    """Two loaded Mowers, each driven by its own RecordingMower."""
    mowers = {}
    monkeypatch.setitem(
        adapters.ADAPTER_REGISTRY, "mammotion", lambda h, e: mowers[e.data[CONF_DEVICE]]
    )
    entries = []
    for name, nick in (("Luba-A", "First"), ("Luba-B", "Second")):
        _, device, _ = await make_mammotion_mower(hass, name=name, nick=nick)
        holder = {}
        mowers[device.id] = RecordingMower(hass_storage, lambda h=holder: h["key"])
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: device.id})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        entry = result["result"]
        holder["key"] = f"groundskeeper.{entry.entry_id}"
        entries.append(entry)
    await hass.async_block_till_done()
    return entries


async def test_a_tap_on_a_gone_prompt_reaches_its_own_mower_by_tag(hass, two_directors) -> None:
    """With two Mowers, a tap on a Prompt that no longer exists is routed by the tag
    the phone sends back: the quiet `prompt_ignored` event carries that Mower's entry."""
    from custom_components.groundskeeper.core import RaisePrompt

    first, second = two_directors
    tags = {}
    for entry in (first, second):
        d = async_get_director(hass, entry.entry_id)
        await d.dispatch(lambda now: RaisePrompt(
            now, "run_failed", None, ["retry", "skip_plan", "abort"], {"reason": "error"}))
        [prompt] = d.state["prompts"]
        tags[entry.entry_id] = prompt["tag"]
    assert tags[first.entry_id] != tags[second.entry_id]

    events = async_capture_events(hass, EVENT_TYPE)
    for entry in (second, first):  # the later Mower first, so list order cannot explain it
        hass.bus.async_fire("mobile_app_notification_action", {
            "action": "MOWDIR_01NOSUCHPROMPT_RETRY", "tag": tags[entry.entry_id]})
        await hass.async_block_till_done()
        assert types(events, "prompt_ignored")[-1]["config_entry_id"] == entry.entry_id

    # a tag that belongs to neither Mower finds no owner
    hass.bus.async_fire("mobile_app_notification_action", {
        "action": "MOWDIR_01NOSUCHPROMPT_RETRY", "tag": "mowdir_nobody_run_failed"})
    await hass.async_block_till_done()
    assert "config_entry_id" not in types(events, "prompt_ignored")[-1]
