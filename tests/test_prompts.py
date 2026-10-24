"""HA integration tests for the Prompt model (spec §12, §13.2, §13.4, ticket #17)."""

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.groundskeeper.adapter import Refused
from custom_components.groundskeeper.const import DOMAIN, EVENT_TYPE
from custom_components.groundskeeper.runtime import async_get_director
from tests.test_tracer_bullet import add_plan, advance, director, state_of  # noqa: F401

PENDING = "sensor.example_mower_pending_prompts"


async def fail_a_run(hass, mower, freezer):
    mower.script_result("start_run", Refused("no"))
    await add_plan(hass, start=True)
    await advance(hass, freezer, 61)


def types(events, type_):
    return [e.data for e in events if e.data["type"] == type_]


async def list_prompts(hass, hass_ws_client, entry):
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {"id": 1, "type": "groundskeeper/list_prompts", "entry_id": entry.entry_id}
    )
    msg = await ws.receive_json()
    assert msg["success"]
    return msg["result"]["prompts"]


async def test_run_failure_raises_failure_prompt_visible_everywhere(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    events = async_capture_events(hass, EVENT_TYPE)
    assert state_of(hass, PENDING).state == "0"

    await fail_a_run(hass, mower, freezer)

    assert state_of(hass, PENDING).state == "1"
    [sent] = types(events, "prompt_sent")
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    assert sent["prompt_id"] == prompt["prompt_id"]
    assert prompt["kind"] == "run_failed"
    assert prompt["tag"] == f"mowdir_{entry.entry_id}_run_failed"
    assert prompt["actions"] == ["retry", "skip_plan", "abort"]
    assert prompt["status"] == "pending"
    assert prompt["next_reminder_at"] is not None
    assert prompt["title"] == "Example Mower: Run failed"
    assert prompt["body"].splitlines() == [
        "Zone: Front Lawn",
        "Plan: 70 mm",
        "Run 1 of 1 at 70 mm",
        "The Mower did not start the Run.",
    ]


@pytest.mark.parametrize(
    ("service", "action", "status"),
    [("retry_run", "retry", "waiting"), ("skip_plan", "skip_plan", "idle"),
     ("cancel", "abort", "idle")],
)
async def test_dedicated_services_answer_the_failure_prompt(
    hass, director, freezer, service, action, status
) -> None:
    _, mower, _ = director
    events = async_capture_events(hass, EVENT_TYPE)
    await fail_a_run(hass, mower, freezer)

    await hass.services.async_call(DOMAIN, service, {}, blocking=True)

    assert state_of(hass, PENDING).state == "0"
    assert state_of(hass, "sensor.example_mower_schedule").state == status
    [answered] = types(events, "prompt_answered")
    assert answered["action"] == action


async def test_answer_prompt_service_answers_once_then_raises(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    events = async_capture_events(hass, EVENT_TYPE)
    await fail_a_run(hass, mower, freezer)
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    call = {"prompt_id": prompt["prompt_id"], "action": "skip_plan"}

    await hass.services.async_call(DOMAIN, "answer_prompt", call, blocking=True)
    assert state_of(hass, "sensor.example_mower_schedule").state == "idle"
    assert len(types(events, "prompt_answered")) == 1

    with pytest.raises(ServiceValidationError):  # already answered
        await hass.services.async_call(DOMAIN, "answer_prompt", call, blocking=True)
    assert len(types(events, "prompt_answered")) == 1


async def test_answer_prompt_rejects_an_action_the_prompt_does_not_offer(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    await fail_a_run(hass, mower, freezer)
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "answer_prompt",
            {"prompt_id": prompt["prompt_id"], "action": "resume"},
            blocking=True,
        )
    assert state_of(hass, PENDING).state == "1"


async def test_stale_notification_tap_is_ignored_quietly(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    events = async_capture_events(hass, EVENT_TYPE)
    await fail_a_run(hass, mower, freezer)
    [prompt] = await list_prompts(hass, hass_ws_client, entry)
    d = async_get_director(hass, entry.entry_id)

    assert await d.async_prompt_tap(prompt["prompt_id"], "retry") is True
    assert await d.async_prompt_tap(prompt["prompt_id"], "retry") is False  # duplicate

    assert len(types(events, "prompt_answered")) == 1
    [ignored] = types(events, "prompt_ignored")
    assert ignored["prompt_id"] == prompt["prompt_id"]
    assert ignored["action"] == "retry"


async def test_prompts_survive_in_the_store_until_answered(
    hass, hass_storage, director, freezer
) -> None:
    entry, mower, _ = director
    key = f"groundskeeper.{entry.entry_id}"
    await fail_a_run(hass, mower, freezer)
    assert [p["kind"] for p in hass_storage[key]["data"]["state"]["prompts"]] == ["run_failed"]
    await hass.services.async_call(DOMAIN, "skip_plan", {}, blocking=True)
    assert hass_storage[key]["data"]["state"]["prompts"] == []


async def test_websocket_subscription_pushes_prompt_list_changes(
    hass, hass_ws_client, director, freezer
) -> None:
    entry, mower, _ = director
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {"id": 1, "type": "groundskeeper/subscribe_prompts", "entry_id": entry.entry_id}
    )
    assert (await ws.receive_json())["success"]
    assert (await ws.receive_json())["event"]["prompts"] == []

    await fail_a_run(hass, mower, freezer)
    pushed = await ws.receive_json()
    assert [p["kind"] for p in pushed["event"]["prompts"]] == ["run_failed"]

    await hass.services.async_call(DOMAIN, "skip_plan", {}, blocking=True)
    assert (await ws.receive_json())["event"]["prompts"] == []


async def test_list_prompts_unknown_mower_is_not_found(hass, hass_ws_client, director) -> None:
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "groundskeeper/list_prompts", "entry_id": "nope"})
    msg = await ws.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        ({"type": "prompt_sent", "kind": "run_failed"}, "Prompt sent: run_failed"),
        ({"type": "prompt_answered", "kind": "run_failed", "action": "retry"},
         "Prompt run_failed answered: retry"),
        ({"type": "prompt_ignored", "action": "retry"}, "Ignored a stale Prompt answer: retry"),
    ],
)
def test_prompt_events_have_logbook_descriptions(payload, text) -> None:
    from custom_components.groundskeeper.logbook import describe

    assert describe(payload) == text
