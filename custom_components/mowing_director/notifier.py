"""Notifier: Prompts and notices to phones (spec §12, ticket #24).

The reducer decides *what happened* (`prompt_sent`, `prompt_reminder`,
`prompt_answered`, `run_status_changed`, `schedule_completed`); this module
turns those effect events into `notify.mobile_app_*` calls, `clear_notification`
by tag, and a `persistent_notification` fallback when no phone is configured.
It reads the Director's state but never changes it; a failed delivery is
logged and fires `notification_failed`, nothing retries (spec §12.10).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import EVENT_TYPE, PANEL_URL_PATH
from .prompts import async_prompt_strings, render_content

if TYPE_CHECKING:
    from .runtime import Director

_LOGGER = logging.getLogger(__name__)

CONFIG_TARGETS = "notify_targets"
CONFIG_CATEGORIES = "notify_categories"

# Spec §12.3. Failure and on-hold cannot be switched off.
CATEGORY_DEFAULTS: dict[str, bool] = {
    "run_failed": True,
    "run_on_hold": True,
    "check_waits": True,
    "rain_question": True,
    "run_started": False,
    "run_done": False,
    "schedule_completed": True,
}
LOCKED_CATEGORIES = frozenset({"run_failed", "run_on_hold"})

# Prompt kind -> category. Kinds that raise their own toggle (Gate, Anomaly)
# are always delivered here; their tickets gate them before raising.
PROMPT_CATEGORY = {"run_failed": "run_failed", "run_on_hold": "run_on_hold"}
# Spec §12.8: only these Prompts are time-sensitive / high priority.
URGENT_KINDS = frozenset({"run_failed", "run_on_hold"})
# Spec §12.8: iOS marks these actions destructive; no confirm on the phone.
DESTRUCTIVE_ACTIONS = frozenset({"abort", "end_run"})
# Where a tap lands in the panel.
PROMPT_TAB = {"run_failed": "now", "run_on_hold": "now"}

ACTION_EVENT = "mobile_app_notification_action"
ACTION_PREFIX = "MOWDIR_"


def action_id(prompt_id: str, action: str) -> str:
    return f"{ACTION_PREFIX}{prompt_id}_{action.upper()}"


def parse_action_id(value: str) -> tuple[str, str] | None:
    """`MOWDIR_<prompt_id>_<ACTION>` -> (prompt_id, action), else None."""
    if not value.startswith(ACTION_PREFIX):
        return None
    prompt_id, _, action = value.removeprefix(ACTION_PREFIX).partition("_")
    if not prompt_id or not action:
        return None
    return prompt_id, action.lower()


def notify_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Targets and category toggles with defaults filled in."""
    categories = {**CATEGORY_DEFAULTS, **config.get(CONFIG_CATEGORIES, {})}
    for locked in LOCKED_CATEGORIES:
        categories[locked] = True
    return {
        "targets": list(config.get(CONFIG_TARGETS, [])),
        "categories": {k: categories[k] for k in CATEGORY_DEFAULTS},
    }


class Notifier:
    """One Mower's phone notifications."""

    def __init__(self, director: Director) -> None:
        self._director = director

    # -- helpers ----------------------------------------------------------------

    @property
    def _hass(self):
        return self._director.hass

    def settings(self) -> dict[str, Any]:
        return notify_settings(self._director.data.get("config", {}))

    def _panel_url(self, tab: str) -> str:
        return f"/{PANEL_URL_PATH}?tab={tab}&entry_id={self._director.entry.entry_id}"

    async def _deliver(self, target: str, payload: dict[str, Any]) -> None:
        try:
            await self._hass.services.async_call("notify", target, payload, blocking=True)
        except HomeAssistantError as err:  # includes ServiceNotFound
            _LOGGER.warning("Notification to notify.%s failed: %s", target, err)
            self._director.fire_event("notification_failed", target=target, error=str(err))
        except Exception as err:  # noqa: BLE001 - a phone must never break the Run logic
            _LOGGER.exception("Notification to notify.%s raised", target)
            self._director.fire_event("notification_failed", target=target, error=str(err))

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        for target in self.settings()["targets"]:
            await self._deliver(target, payload)

    # -- event entry point ----------------------------------------------------------

    async def async_handle(self, type_: str, payload: dict[str, Any]) -> None:
        match type_:
            case "prompt_sent":
                await self._send_prompt(payload["prompt_id"], reminder=False)
            case "prompt_reminder":
                await self._send_prompt(payload["prompt_id"], reminder=True)
            case "prompt_answered":
                await self._clear(payload["tag"])
            case "run_status_changed":
                await self._run_notice(payload)
            case "schedule_completed":
                await self._schedule_completed(payload)

    # -- Prompts ----------------------------------------------------------------------

    async def _send_prompt(self, prompt_id: str, *, reminder: bool) -> None:
        prompt = self._director.find_prompt(prompt_id)
        if prompt is None:
            return
        category = PROMPT_CATEGORY.get(prompt["kind"], prompt["kind"])
        if not self.settings()["categories"].get(category, True):
            return
        strings = await async_prompt_strings(self._hass)
        content = render_content(prompt, self._director.entry.title, strings)
        url = self._panel_url(PROMPT_TAB.get(prompt["kind"], "now"))
        targets = self.settings()["targets"]
        if not targets:
            if prompt["kind"] in URGENT_KINDS:  # failure and on-hold only (§12.2)
                persistent_notification.async_create(
                    self._hass,
                    f"{content['body']}\n\n[{strings['prompt_open_panel']}]({url})",
                    content["title"],
                    prompt["tag"],
                )
            return
        data = self._data(category, prompt["tag"], url, strings,
                          urgent=prompt["kind"] in URGENT_KINDS)
        data["actions"] = [
            {
                "action": action_id(prompt["prompt_id"], action),
                "title": strings.get(
                    f"prompt_action_{action}", action.replace("_", " ").capitalize()
                ),
                **({"destructive": True} if action in DESTRUCTIVE_ACTIONS else {}),
            }
            for action in prompt["actions"]
        ]
        await self._broadcast(
            {"title": content["title"], "message": content["body"], "data": data}
        )

    def _data(
        self, category: str, tag: str, url: str, strings: dict[str, str], *, urgent: bool
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "tag": tag,
            "channel": strings.get(f"prompt_channel_{category}", category),
            "clickAction": url,
            "url": url,
        }
        if urgent:
            data["push"] = {"interruption-level": "time-sensitive"}
            data["priority"] = "high"
        return data

    async def _clear(self, tag: str) -> None:
        persistent_notification.async_dismiss(self._hass, tag)
        await self._broadcast({"message": "clear_notification", "data": {"tag": tag}})

    # -- notices ----------------------------------------------------------------------

    async def async_notice(
        self,
        category: str,
        kind: str,
        headline: str,
        lines: list[str],
        *,
        subject: str | None = None,
    ) -> None:
        """A plain notice (no actions): for Check waits and other tickets' notices."""
        if not self.settings()["categories"].get(category, True):
            return
        strings = await async_prompt_strings(self._hass)
        entry = self._director.entry
        tag = f"mowdir_{entry.entry_id}_{kind}" + (f"_{subject}" if subject else "")
        data = self._data(category, tag, self._panel_url("now"), strings, urgent=False)
        await self._broadcast(
            {"title": f"{entry.title}: {headline}", "message": "\n".join(lines), "data": data}
        )

    async def _run_notice(self, payload: dict[str, Any]) -> None:
        if payload.get("to") == "mowing" and payload.get("from") == "starting":
            category = "run_started"
        elif payload.get("to") == "done":
            category = "run_done"
        else:
            return
        strings = await async_prompt_strings(self._hass)
        run = self._find_run(payload["run_id"])
        lines = [strings["prompt_zone"].format(zone=payload.get("zone_name"))]
        if run is not None:
            lines.append(
                strings["prompt_run"].format(
                    n=run["index"], m=run["of"], height=f"{run['height_mm']:g}"
                )
            )
        await self.async_notice(
            category,
            category,
            strings[f"prompt_headline_{category}"],
            lines,
            subject=payload["run_id"],
        )

    def _find_run(self, run_id: str) -> dict[str, Any] | None:
        data = self._director.data
        for run in [*data["state"]["runs"], *data["history"]["runs"]]:
            if run["id"] == run_id:
                return run
        return None

    async def _schedule_completed(self, payload: dict[str, Any]) -> None:
        summary = payload.get("summary") or {}
        strings = await async_prompt_strings(self._hass)
        await self.async_notice(
            "schedule_completed",
            "schedule_completed",
            strings["prompt_headline_schedule_completed"],
            [
                strings["prompt_summary_plans"].format(plans=summary.get("plans", 0)),
                strings["prompt_summary_runs"].format(
                    done=summary.get("runs_done", 0), skipped=summary.get("runs_skipped", 0)
                ),
                strings["prompt_summary_time"].format(
                    minutes=round(summary.get("mowing_s", 0) / 60)
                ),
                strings["prompt_summary_anomalies"].format(count=summary.get("anomalies", 0)),
            ],
        )


# -- tapping an action button (spec §12.5) ----------------------------------------


async def async_handle_action_event(hass: HomeAssistant, event: Event) -> None:
    """A `mobile_app_notification_action` tap, from any phone of any Mower.

    The `prompt_id` in the action id finds the Mower. A tap on a Prompt that is
    no longer pending (answered elsewhere, superseded, duplicate) takes the
    quiet `prompt_ignored` path: nothing is sent to any phone.
    """
    from .runtime import async_all_directors

    parsed = parse_action_id(str(event.data.get("action", "")))
    if parsed is None:
        return  # someone else's notification action
    prompt_id, action = parsed
    directors = async_all_directors(hass)
    for director in directors:
        if director.find_prompt(prompt_id) is not None:
            await director.async_prompt_tap(prompt_id, action)
            return
    tag = str(event.data.get("tag", ""))
    owner = next(
        (d for d in directors if tag.startswith(f"mowdir_{d.entry.entry_id}_")),
        directors[0] if len(directors) == 1 else None,
    )
    if owner is not None:
        await owner.async_prompt_tap(prompt_id, action)
    else:
        hass.bus.async_fire(
            EVENT_TYPE, {"type": "prompt_ignored", "prompt_id": prompt_id, "action": action}
        )
