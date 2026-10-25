"""Mammotion Zone, Channel and native-task catalogue (spec §3.4, §4.6).

Everything here is discovered through the device and entity registries,
never by hard-coded entity ID: the Mower's `lawn_mower` entity is the
target of the response services, and Zone switches are the device's
`mammotion` switch entities that carry (or whose unique_id ends in) a
Zone `hash`.

Geometry comes from `mammotion.get_geojson`, which starts a 5-minute
report stream on the Mower, so it is fetched only at setup (first use),
after a Zone-list change (map update) or on demand via
`async_refresh_geometry` - never on a timer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.const import EVENT_STATE_CHANGED

from ..adapter import (
    Accepted,
    AdapterSignal,
    Channel,
    CommandResult,
    NativeTask,
    Refused,
    Unconfirmed,
    Zone,
    ZonesChanged,
)

MAMMOTION = "mammotion"
_TRAILING_INT = re.compile(r"(\d+)$")


def _line_string(geometry: dict[str, Any] | None) -> dict[str, Any] | None:
    """A usable Channel path: a GeoJSON LineString of at least two points."""
    if (
        geometry
        and geometry.get("type") == "LineString"
        and len(geometry.get("coordinates") or []) >= 2
    ):
        return geometry
    return None


class MammotionCatalogue:
    """Reads Zones, Channels and native tasks for one Mammotion Mower device."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        emit: Callable[[AdapterSignal], None],
    ) -> None:
        self._hass = hass
        self._device_id = device_id
        self._emit = emit
        self._features: list[dict[str, Any]] | None = None
        self._task_entities: dict[str, str] = {}
        self._zone_hashes: frozenset[str] | None = None
        self._unsubs: list[Callable[[], None]] = []

    # -- Zone-list change detection ----------------------------------------

    @callback
    def async_start(self) -> None:
        """Watch for Zone-list changes (map rebuild, re-keyed hashes)."""
        self._zone_hashes = frozenset(self._zone_switches())
        self._unsubs.append(
            self._hass.bus.async_listen(
                EVENT_STATE_CHANGED,
                self._on_event,
                event_filter=self._is_zone_state_event,
            )
        )
        self._unsubs.append(
            self._hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_event
            )
        )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _is_zone_state_event(self, data: Any) -> bool:
        entity_id = data["entity_id"]
        return entity_id.startswith("switch.") and any(
            e.entity_id == entity_id for e in self._device_entities("switch")
        )

    @callback
    def _on_event(self, _event: Event) -> None:
        hashes = frozenset(self._zone_switches())
        if hashes == self._zone_hashes:
            return
        self._zone_hashes = hashes
        self._features = None  # next list_* refetches geometry (map update)
        self._emit(ZonesChanged())

    # -- discovery ---------------------------------------------------------

    def _device_entities(self, domain: str) -> list[er.RegistryEntry]:
        reg = er.async_get(self._hass)
        return [
            e
            for e in er.async_entries_for_device(reg, self._device_id)
            if e.domain == domain and e.platform == MAMMOTION
        ]

    def _mower_entity_id(self) -> str:
        mowers = self._device_entities("lawn_mower")
        if not mowers:
            raise HomeAssistantError("Mammotion lawn_mower entity not found")
        return mowers[0].entity_id

    def _zone_switches(self) -> dict[str, er.RegistryEntry]:
        """Zone switches keyed by Zone hash (str)."""
        found: dict[str, er.RegistryEntry] = {}
        for entry in self._device_entities("switch"):
            state = self._hass.states.get(entry.entity_id)
            zone_hash = state.attributes.get("hash") if state else None
            if zone_hash is None:
                # Unavailable switches drop the attribute; fall back to the
                # unique_id, which ends in the hash for Zone switches.
                match = _TRAILING_INT.search(entry.unique_id)
                if match is None or len(match.group(1)) < 10:
                    continue
                zone_hash = match.group(1)
            found[str(zone_hash)] = entry
        return found

    def _switch_name(self, entry: er.RegistryEntry) -> str:
        state = self._hass.states.get(entry.entity_id)
        return (
            entry.name
            or entry.original_name
            or (state.attributes.get("friendly_name") if state else None)
            or entry.entity_id
        )

    # -- service plumbing --------------------------------------------------

    async def _call_response(self, service: str) -> dict[str, Any]:
        entity_id = self._mower_entity_id()
        response = await self._hass.services.async_call(
            MAMMOTION,
            service,
            {"entity_id": [entity_id]},
            blocking=True,
            return_response=True,
        )
        if response is not None and entity_id in response:
            response = response[entity_id]
        return response or {}

    # -- geometry ----------------------------------------------------------

    async def async_refresh_geometry(self) -> None:
        """Fetch geometry now (setup, map update or on demand)."""
        data = await self._call_response("get_geojson")
        self._features = list(data.get("features", []))

    async def _geometry(self) -> list[dict[str, Any]]:
        if self._features is None:
            await self.async_refresh_geometry()
        return self._features or []

    # -- catalogues --------------------------------------------------------

    async def list_zones(self) -> list[Zone]:
        """Zones as (native_ref, name, polygon or None)."""
        polygons: dict[str, dict[str, Any]] = {}
        names: dict[str, str] = {}
        try:
            for feat in await self._geometry():
                props = feat.get("properties", {})
                if props.get("type_name") == "area":
                    ref = str(props["hash"])
                    polygons[ref] = feat["geometry"]
                    # The map carries the name as the Mammotion app shows it;
                    # the zone switch's name is "Area {name}" (issue #55).
                    plain = props.get("Name") or props.get("title")
                    if plain:
                        names[ref] = plain
        except HomeAssistantError:
            pass  # geometry never blocks the Zone list (issue #11)
        return [
            Zone(ref, names.get(ref) or self._switch_name(entry), polygons.get(ref))
            for ref, entry in self._zone_switches().items()
        ]

    async def list_channels(self) -> list[Channel]:
        """Channels with their path; parent hash 0 is the dock."""

        def end(parent_hash: Any) -> str:
            return Channel.DOCK if not int(parent_hash) else str(parent_hash)

        return [
            Channel(
                str(feat["properties"]["hash"]),
                end(feat["properties"]["parent_hash_a"]),
                end(feat["properties"]["parent_hash_b"]),
                path=_line_string(feat.get("geometry")),
            )
            for feat in await self._geometry()
            if feat.get("properties", {}).get("type_name") == "path"
        ]

    async def list_native_tasks(self) -> list[NativeTask]:
        """The Mower's own scheduler, read-only (`mammotion.get_tasks`)."""
        data = await self._call_response("get_tasks")
        rows = data.get("tasks", [])
        self._task_entities = {
            str(r["task_id"]): r["entity_id"] for r in rows if r.get("entity_id")
        }
        return [
            NativeTask(str(r["task_id"]), r["name"], bool(r["enabled"]))
            for r in rows
        ]

    async def set_task_enabled(self, task: str, enabled: bool) -> CommandResult:
        """Enable/disable one native task (Repairs fix flow only)."""
        entity_id = self._task_entities.get(task)
        if entity_id is None:
            try:
                await self.list_native_tasks()
            except HomeAssistantError:
                return Unconfirmed()
            entity_id = self._task_entities.get(task)
        if entity_id is None:
            return Refused(f"unknown native task {task}")
        try:
            await self._hass.services.async_call(
                MAMMOTION,
                "set_task_enabled",
                {"entity_id": [entity_id], "enabled": enabled},
                blocking=True,
            )
        except ServiceValidationError as err:
            return Refused(str(err))
        except HomeAssistantError:
            return Unconfirmed()
        return Accepted()
