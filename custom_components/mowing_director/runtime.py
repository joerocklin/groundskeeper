"""Effect runner and per-Mower director (spec §2.2, §6.6, ADR 0004).

`Director` owns one Mower's in-memory core state. Every input is reduced,
the new state is saved (awaited) and only then are its effects performed, so
a restart can always tell that a command may already have gone out. Inputs
are serialised by one lock; timers are in-memory only and re-armed from the
persisted absolute deadlines at load.

Loading, unloading and reloading never command the Mower: the only commands
the reducer emits are `start_run` (once, on `pending` to `starting`) and
`abort_run`, and neither is emitted by `async_load` or `async_unload`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .prompts import async_prompt_strings, render_content
from .adapter import (
    AdapterSignal,
    CommandResult,
    MowerAdapter,
    Snapshot,
    Unconfirmed,
)
from .const import DOMAIN, EVENT_TYPE
from .core import (
    DEADLINE_STABILITY,
    AbortRunEffect,
    AddPlan,
    AnswerPrompt,
    ArmTimer,
    CancelSchedule,
    CancelTimer,
    CommandOutcome,
    EndRun,
    ResumeRun,
    ResumeRunEffect,
    RetryRun,
    SkipPlan,
    CoreInput,
    EmitEvent,
    Env,
    SignalReceived,
    SnapshotReceived,
    StartRunEffect,
    StartSchedule,
    TimerFired,
    ZoneView,
    normalise_core_state,
    parse,
    reduce,
    active_run,
)
from .housekeeping import async_set_run_active_provider
from .store import MowerStore

_LOGGER = logging.getLogger(__name__)

DELAYED_SAVE_S = 60  # spec §14.4
_CORE_KEYS = ("state", "learned", "history")
# Changes to only these never need an awaited save (spec §14.4).


def _only_soft_changes(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when only the snapshot summary and/or `learned` differ."""
    if old["history"] != new["history"]:
        return False
    strip = lambda s: {k: v for k, v in s.items() if k != "snapshot"}  # noqa: E731
    return strip(old["state"]) == strip(new["state"])


class _Sink:
    """The adapter's push target for one Director."""

    def __init__(self, director: Director) -> None:
        self._director = director

    @callback
    def on_snapshot(self, snapshot: Snapshot) -> None:
        self._director.submit(lambda now: SnapshotReceived(now, snapshot))

    @callback
    def on_signal(self, signal: AdapterSignal) -> None:
        self._director.submit(lambda now: SignalReceived(now, signal))


class Director:
    """One Mower's core, effect runner and timers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        adapter: MowerAdapter,
        store: MowerStore,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.adapter = adapter
        self._store = store
        self._data: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._listeners: list[Callable[[], None]] = []
        self._unsub_adapter: CALLBACK_TYPE | None = None
        self._closed = False

    # -- read access ----------------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        """The shared Store dict (config, state, learned, history)."""
        return self._data

    @property
    def state(self) -> dict[str, Any]:
        """The reducer's `state` section (read only for callers)."""
        return self._data["state"]

    @property
    def core_state(self) -> dict[str, Any]:
        return {key: self._data[key] for key in _CORE_KEYS}

    @property
    def device_id(self) -> str | None:
        return self.entry.data.get(CONF_DEVICE)

    def run_active(self) -> bool:
        """Whether a Run is `starting`, `mowing` or `on hold` (blocks rebind)."""
        return active_run(self.state) is not None

    def env(self) -> Env:
        """Read-only facts for the reducer from config and the adapter."""
        cfg = self._data.get("config", {})
        zones = {
            z["id"]: ZoneView(
                native_ref=z["native_ref"],
                name=z["name"],
                missing=bool(z.get("missing")),
                bearing=(z.get("bearing") or {}).get("value"),
            )
            for z in cfg.get("zones", [])
        }
        from datetime import timedelta

        window = timedelta(seconds=cfg.get("start_window_s", 300))
        return Env(
            zones=zones,
            min_start_battery_pct=self.adapter.limits.min_start_battery_pct,
            start_window=window,
            stall_window=timedelta(seconds=cfg.get("stall_window_s", 3600)),
            entry_id=self.entry.entry_id,
        )

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call `listener` after every state change (entities)."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    # -- lifecycle --------------------------------------------------------------

    async def async_load(self) -> None:
        """Load state, re-arm timers from it, observe the adapter. No commands."""
        data = await self._store.async_data()
        self._data = data
        data.update(normalise_core_state(data))
        # The stability window is state-derived evidence: downtime does not
        # count towards it, so it restarts after a load.
        data["state"]["deadlines"].pop(DEADLINE_STABILITY, None)
        for name, at in data["state"]["deadlines"].items():
            self._arm(name, parse(at))
        async_set_run_active_provider(self.hass, self.entry.entry_id, self.run_active)
        self._unsub_adapter = self.adapter.subscribe(_Sink(self))
        await self.adapter.async_start()
        await self.dispatch(
            lambda now: SnapshotReceived(now, self.adapter.snapshot)
        )

    @callback
    def async_unload(self) -> None:
        """Stop timers and observation. Never commands the Mower."""
        self._closed = True
        if self._unsub_adapter:
            self._unsub_adapter()
            self._unsub_adapter = None
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    # -- inputs ------------------------------------------------------------------

    @callback
    def submit(self, make_input: Callable[[datetime], CoreInput]) -> None:
        """Queue an input from a sync callback (adapter push, timer)."""
        if not self._closed:
            self.entry.async_create_task(
                self.hass, self.dispatch(make_input), "mowing_director input"
            )

    async def dispatch(self, make_input: Callable[[datetime], CoreInput]) -> None:
        """Reduce, save, then perform effects; follow-up results loop here."""
        async with self._lock:
            pending: list[Callable[[datetime], CoreInput]] = [make_input]
            while pending:
                event = pending.pop(0)(dt_util.utcnow())
                effects = await self._reduce_and_save(event)
                pending.extend(await self._perform(effects))

    async def _reduce_and_save(self, event: CoreInput) -> tuple[Any, ...]:
        old = self.core_state
        reduction = reduce(old, event, self.env())
        new = reduction.state
        if new != old:
            self._data.update(new)
            if _only_soft_changes(old, new) and not reduction.effects:
                self._store.async_delay_save_shared(DELAYED_SAVE_S)
            else:
                await self._store.async_save(self._data)
            for listener in list(self._listeners):
                listener()
        return reduction.effects

    # -- effects (only ever after the save above) ---------------------------------

    async def _perform(self, effects: tuple[Any, ...]) -> list[Callable[[datetime], CoreInput]]:
        follow_ups: list[Callable[[datetime], CoreInput]] = []
        for effect in effects:
            match effect:
                case EmitEvent():
                    self._fire(effect)
                case ArmTimer():
                    self._arm(effect.name, effect.at)
                case CancelTimer():
                    if (cancel := self._timers.pop(effect.name, None)) is not None:
                        cancel()
                case StartRunEffect():
                    result = await self._command(
                        self.adapter.start_run(effect.zone_ref, effect.settings)
                    )
                    run_id = effect.run_id
                    follow_ups.append(
                        lambda now, r=result, i=run_id: CommandOutcome(
                            now, i, "start_run", r
                        )
                    )
                case AbortRunEffect():
                    await self._command(self.adapter.abort_run())
                case ResumeRunEffect():
                    result = await self._command(self.adapter.resume_run())
                    follow_ups.append(
                        lambda now, r=result, i=effect.run_id: CommandOutcome(
                            now, i, "resume_run", r
                        )
                    )
        return follow_ups

    async def _command(self, awaitable: Any) -> CommandResult:
        try:
            return await awaitable
        except Exception:  # noqa: BLE001 - adapters should not raise; never re-issue
            _LOGGER.exception("Mower command raised; treating as unconfirmed")
            return Unconfirmed()

    def _fire(self, effect: EmitEvent) -> None:
        payload = {
            "type": effect.type,
            "config_entry_id": self.entry.entry_id,
            "device_id": self.device_id,
            **effect.payload,
        }
        self.hass.bus.async_fire(EVENT_TYPE, payload)

    def _arm(self, name: str, at: datetime) -> None:
        if (old := self._timers.pop(name, None)) is not None:
            old()

        @callback
        def _fired(_now: datetime) -> None:
            self._timers.pop(name, None)
            self.submit(TimerFired)

        self._timers[name] = async_track_point_in_utc_time(self.hass, _fired, at)

    # -- user-facing operations -----------------------------------------------------

    async def async_add_plan(
        self, plan: dict[str, Any], runs: list[dict[str, Any]], start: bool
    ) -> None:
        await self.dispatch(lambda now: AddPlan(now, plan, runs, start))

    async def async_start(self) -> None:
        await self.dispatch(StartSchedule)

    async def async_cancel(self) -> None:
        await self.dispatch(CancelSchedule)

    async def async_retry_run(self) -> None:
        await self.dispatch(RetryRun)

    async def async_skip_plan(self) -> None:
        await self.dispatch(SkipPlan)

    async def async_resume_run(self) -> None:
        await self.dispatch(ResumeRun)

    async def async_end_run(self) -> None:
        await self.dispatch(EndRun)

    # -- Prompts (spec §12) -----------------------------------------------------

    def find_prompt(self, prompt_id: str) -> dict[str, Any] | None:
        """The pending Prompt with this id, or None (answered or stale)."""
        return next(
            (p for p in self.state["prompts"] if p["prompt_id"] == prompt_id), None
        )

    async def async_answer_prompt(self, prompt_id: str, action: str) -> None:
        await self.dispatch(lambda now: AnswerPrompt(now, prompt_id, action))

    async def async_prompt_tap(self, prompt_id: str, action: str) -> bool:
        """A notification action tap: answers if still pending, else the quiet
        `prompt_ignored` path (spec §12.5). Returns whether it answered."""
        prompt = self.find_prompt(prompt_id)
        if prompt is None or action not in prompt["actions"]:
            self._fire(
                EmitEvent("prompt_ignored", {"prompt_id": prompt_id, "action": action})
            )
            return False
        await self.async_answer_prompt(prompt_id, action)
        return True

    async def async_prompt_views(self) -> list[dict[str, Any]]:
        """Pending Prompts with their channel-independent title and body."""
        strings = await async_prompt_strings(self.hass)
        return [
            {**prompt, **render_content(prompt, self.entry.title, strings)}
            for prompt in self.state["prompts"]
        ]


@callback
def async_get_director(hass: HomeAssistant, entry_id: str) -> Director | None:
    """The running Director for a Mower entry, if any."""
    return hass.data.get(DOMAIN, {}).get("director", {}).get(entry_id)


@callback
def async_register_director(hass: HomeAssistant, director: Director) -> None:
    hass.data.setdefault(DOMAIN, {}).setdefault("director", {})[
        director.entry.entry_id
    ] = director


@callback
def async_unregister_director(hass: HomeAssistant, entry_id: str) -> None:
    director = hass.data.get(DOMAIN, {}).get("director", {}).pop(entry_id, None)
    if director:
        director.async_unload()
