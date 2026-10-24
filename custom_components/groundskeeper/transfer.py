"""Export, import with undo, and Broken references on HA (spec §14.8, §4.2).

The pure planning is in `config_transfer`; this module reads the entity
registry and service registry, owns the undo snapshot and the one per-Mower
Repairs issue. Nothing here commands the Mower.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.const import EVENT_SERVICE_REGISTERED, EVENT_SERVICE_REMOVED
from homeassistant.util import dt as dt_util

from .config_transfer import (
    ImportError_,
    broken_references,
    build_export,
    plan_import,
)
from .const import DOMAIN
from .store_validation import validate_store

if TYPE_CHECKING:
    from .runtime import Director

UNDO_KEY = "undo"


def _fail(key: str) -> HomeAssistantError:
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=key)


def entity_id_of(hass: HomeAssistant, ref: str) -> str | None:
    """Current entity_id for a stored reference, or None if it does not resolve."""
    registry = er.async_get(hass)
    if (entry := registry.async_get(ref)) is not None:
        return entry.entity_id
    return None


def entity_exists(hass: HomeAssistant, ref: str) -> bool:
    """Whether a stored entity reference points at something that exists."""
    registry = er.async_get(hass)
    entity_id = er.async_resolve_entity_id(registry, ref)
    if entity_id is None:
        return False
    return registry.async_get(entity_id) is not None or hass.states.get(entity_id) is not None


def notify_exists(hass: HomeAssistant, name: str) -> bool:
    return hass.services.has_service("notify", name)


def async_export(director: Director, include_history: bool) -> dict[str, Any]:
    """The export file for one Mower."""
    data = director.data
    return build_export(
        data.get("config", {}),
        data.get("history", {}),
        entity_id_of=lambda ref: entity_id_of(director.hass, ref),
        include_history=include_history,
    )


async def async_import(director: Director, export: Any) -> dict[str, Any]:
    """Replace `config` from an export; returns the import report."""
    hass = director.hass
    store = director.store
    safe_idle = bool(store.safe_idle)
    if not safe_idle and director.state["schedule"]["status"] != "idle":
        raise _fail("import_not_idle")
    registry = er.async_get(hass)
    target_zones = (
        None if safe_idle else copy.deepcopy(director.data.get("config", {}).get("zones", []))
    )
    try:
        plan = plan_import(
            export,
            target_zones=target_zones,
            registry_id_of=lambda ref: entry.id if (entry := registry.async_get(ref)) else None,
        )
        if not validate_store({"config": plan.config}).config_ok:
            raise ImportError_("invalid_config")
    except ImportError_ as err:
        raise _fail("import_invalid") from err
    plan.report["unresolved_notify"] = [
        t for t in plan.config.get("notify_targets", []) if not notify_exists(hass, t)
    ]
    director.fire_event("config_imported", report=plan.report)
    if safe_idle:
        await store.async_restore(plan.config, export.get("history"))
        hass.config_entries.async_schedule_reload(director.entry.entry_id)
        return plan.report
    async with director.lock:
        data = director.data
        data[UNDO_KEY] = {
            "taken_at": dt_util.utcnow().isoformat(),
            "config": copy.deepcopy(data.get("config", {})),
        }
        data["config"] = plan.config
        await store.async_save(data)
    await director.config_replaced()
    return plan.report


async def async_undo_import(director: Director) -> None:
    """Put back the config the last import replaced."""
    if director.state["schedule"]["status"] != "idle":
        raise _fail("import_not_idle")
    async with director.lock:
        snapshot = director.data.pop(UNDO_KEY, None)
        if snapshot is None:
            raise _fail("nothing_to_undo")
        director.data["config"] = snapshot["config"]
        await director.store.async_save(director.data)
    await director.config_replaced()


class BrokenReferences:
    """The per-Mower Repairs issue listing every reference that does not resolve."""

    def __init__(self, director: Director) -> None:
        self._director = director
        self._unsubs: list[CALLBACK_TYPE] = []
        self._tracked: tuple[str, ...] = ()
        self._unsub_state: CALLBACK_TYPE | None = None
        self._issue_id = f"broken_references_{director.entry.entry_id}"

    @callback
    def start(self) -> None:
        hass = self._director.hass
        self._unsubs += [
            hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._changed),
            hass.bus.async_listen(EVENT_SERVICE_REGISTERED, self._service_changed),
            hass.bus.async_listen(EVENT_SERVICE_REMOVED, self._service_changed),
        ]
        self.refresh()

    @callback
    def close(self) -> None:
        for cancel in self._unsubs:
            cancel()
        self._unsubs.clear()
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None

    @callback
    def _changed(self, _event: Event) -> None:
        self.refresh()

    @callback
    def _service_changed(self, event: Event) -> None:
        if event.data.get("domain") == "notify":
            self.refresh()

    @callback
    def refresh(self) -> None:
        """Raise, update or clear the issue to match the config as it is now."""
        hass = self._director.hass
        config = self._director.data.get("config", {})
        broken = broken_references(
            config,
            entity_exists=lambda ref: entity_exists(hass, ref),
            notify_exists=lambda name: notify_exists(hass, name),
        )
        watch = tuple(sorted({b.ref for b in broken if b.kind == "entity"}))
        if watch != self._tracked:
            self._tracked = watch
            if self._unsub_state:
                self._unsub_state()
                self._unsub_state = None
            ids = [e for e in watch if "." in e]
            if ids:
                self._unsub_state = async_track_state_change_event(hass, ids, self._changed)
        if not broken:
            ir.async_delete_issue(hass, DOMAIN, self._issue_id)
            return
        lines = "\n".join(
            f"- {b.ref}: {b.where}" if b.kind == "entity" else f"- notify.{b.ref}: {b.where}"
            for b in broken
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="broken_references",
            translation_placeholders={
                "mower": self._director.entry.title,
                "references": lines,
            },
        )
