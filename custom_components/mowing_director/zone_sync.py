"""Zone and Channel import and sync (spec §4.4, §4.6, §4.8).

`reconcile_zones` and `reconcile_channels` are pure; `ZoneSync` is the
per-Mower orchestrator that reads the adapter catalogue, persists the
result in the Mower's Store (`config.zones`, `config.channels`) and keeps
the per-Mower "missing Zones" Repairs issue current.

A Director Zone is a plain dict (JSON for the Store):

    id, native_ref, name, hidden, missing, polygon, bearing_override

`id` is a ULID and never changes; `native_ref` and `name` follow the Mower.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import ulid as ulid_util

from .adapter import AdapterSignal, MowerAdapter, Zone, ZonesChanged
from .const import DOMAIN

ISSUE_MISSING_ZONES = "missing_zones_{}"

type ZoneRecord = dict[str, Any]
type ChannelRecord = dict[str, Any]


def reconcile_zones(
    known: list[ZoneRecord],
    listed: list[Zone],
    new_id: Callable[[], str] = ulid_util.ulid,
) -> list[ZoneRecord]:
    """Match the Mower's Zone list onto the Director's Zones (spec §4.4).

    Match native_ref first, then exact name among Zones the ref pass did
    not claim. Anything unmatched on either side is a new Zone (listed) or
    `missing` (known). Never deletes. Returns the new Zone list: known
    Zones in their existing order, then new ones in listed order.
    """
    by_ref = {z["native_ref"]: z for z in known}
    claimed: dict[str, Zone] = {}  # director id -> listed zone
    rest: list[Zone] = []
    for lz in listed:
        match = by_ref.get(lz.native_ref)
        if match is not None and match["id"] not in claimed:
            claimed[match["id"]] = lz
        else:
            rest.append(lz)

    new_listed: list[Zone] = []
    for lz in rest:
        match = next(
            (z for z in known if z["id"] not in claimed and z["name"] == lz.name),
            None,
        )
        if match is None:
            new_listed.append(lz)
            continue
        claimed[match["id"]] = lz
        match["_rebind"] = True

    result: list[ZoneRecord] = []
    for z in known:
        z = dict(z)
        rebind = z.pop("_rebind", False)
        lz = claimed.get(z["id"])
        if lz is None:
            z["missing"] = True
        else:
            z["missing"] = False
            z["name"] = lz.name  # same ref, new name -> rename
            if rebind:
                z["native_ref"] = lz.native_ref
                z["polygon"] = None  # changed ref: refetch the polygon
            if lz.polygon is not None:
                z["polygon"] = lz.polygon
        result.append(z)
    for lz in new_listed:
        result.append(
            {
                "id": new_id(),
                "native_ref": lz.native_ref,
                "name": lz.name,
                "hidden": False,
                "missing": False,
                "polygon": lz.polygon,
                "bearing_override": None,
            }
        )
    return result


def reconcile_channels(
    known: list[ChannelRecord],
    listed: list[Any],
    zones: list[ZoneRecord],
    new_id: Callable[[], str] = ulid_util.ulid,
) -> list[ChannelRecord]:
    """Reconcile Channels by their end Zones (spec §4.6).

    Hash stability across map rebuilds is unknown, so a Channel keeps its
    Director id when its unordered end Zones match, even if its native_ref
    changed. Ends are Director Zone ids or "dock". Channels whose ends are
    not known Zones are skipped; Channels that vanish are dropped.
    """
    ref_to_id = {z["native_ref"]: z["id"] for z in zones if not z["missing"]}

    def end(ref: str) -> str | None:
        return "dock" if ref == "dock" else ref_to_id.get(ref)

    pool = list(known)
    result: list[ChannelRecord] = []
    for ch in listed:
        a, b = end(ch.end_a), end(ch.end_b)
        if a is None or b is None:
            continue
        ends = frozenset((a, b))
        match = next(
            (k for k in pool if k["native_ref"] == ch.native_ref), None
        ) or next(
            (k for k in pool if frozenset((k["end_a"], k["end_b"])) == ends), None
        )
        if match is not None:
            pool.remove(match)
        result.append(
            {
                "id": match["id"] if match else new_id(),
                "native_ref": ch.native_ref,
                "end_a": a,
                "end_b": b,
            }
        )
    return result


class ZoneSync:
    """Per-Mower Zone and Channel sync against the adapter catalogue."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        adapter: MowerAdapter,
        store: Any,
    ) -> None:
        """Bind to one Mower's adapter and Store."""
        self._hass = hass
        self._entry_id = entry_id
        self._adapter = adapter
        self._store = store
        self._lock = asyncio.Lock()
        self._unsub: CALLBACK_TYPE | None = None
        self._task: asyncio.Task | None = None
        self._data: dict | None = None

    # -- lifecycle ----------------------------------------------------------

    def async_start(self) -> None:
        """Subscribe to Zone-list changes and run the first sync in the
        background, so geometry never blocks setup."""
        self._unsub = self._adapter.subscribe(_Sink(self))
        self._task = self._hass.async_create_background_task(
            self.async_sync(), f"{DOMAIN} zone sync {self._entry_id}"
        )

    def async_stop(self) -> None:
        """Unsubscribe and cancel any in-flight first sync."""
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._task and not self._task.done():
            self._task.cancel()

    @callback
    def _on_signal(self, signal: AdapterSignal) -> None:
        if isinstance(signal, ZonesChanged):
            self._hass.async_create_background_task(
                self.async_sync(), f"{DOMAIN} zone resync {self._entry_id}"
            )

    # -- state --------------------------------------------------------------

    async def _config(self) -> dict:
        if self._data is None:
            loaded = await self._store.async_load()
            if loaded is None:
                from .store import empty_store_data

                loaded = empty_store_data()
            self._data = loaded
        cfg = self._data.setdefault("config", {})
        cfg.setdefault("zones", [])
        cfg.setdefault("channels", [])
        return cfg

    async def async_zones(self) -> list[ZoneRecord]:
        """Current Director Zones."""
        return list((await self._config())["zones"])

    async def async_channels(self) -> list[ChannelRecord]:
        """Current Director Channels."""
        return list((await self._config())["channels"])

    # -- operations ---------------------------------------------------------

    async def async_sync(self) -> None:
        """Run one sync: Zones, then Channels, then the Repairs issue."""
        async with self._lock:
            cfg = await self._config()
            listed = await self._adapter.list_zones()
            cfg["zones"] = reconcile_zones(cfg["zones"], listed)
            channels = await self._adapter.list_channels()
            cfg["channels"] = reconcile_channels(
                cfg["channels"], channels, cfg["zones"]
            )
            await self._store.async_save(self._data)
            self._update_issue(cfg["zones"])

    async def async_set_hidden(self, zone_id: str, hidden: bool) -> None:
        """Hide or unhide a Zone (panel action)."""
        async with self._lock:
            cfg = await self._config()
            for z in cfg["zones"]:
                if z["id"] == zone_id:
                    z["hidden"] = hidden
                    break
            else:
                raise KeyError(zone_id)
            await self._store.async_save(self._data)

    async def async_delete_missing(self) -> None:
        """Repairs fix: delete every missing Zone and its Plans."""
        async with self._lock:
            cfg = await self._config()
            gone = {z["id"] for z in cfg["zones"] if z["missing"]}
            cfg["zones"] = [z for z in cfg["zones"] if z["id"] not in gone]
            cfg["plans"] = [
                p for p in cfg.get("plans", []) if p.get("zone_id") not in gone
            ]
            cfg["channels"] = [
                c
                for c in cfg["channels"]
                if c["end_a"] not in gone and c["end_b"] not in gone
            ]
            await self._store.async_save(self._data)
            self._update_issue(cfg["zones"])

    def _update_issue(self, zones: list[ZoneRecord]) -> None:
        issue_id = ISSUE_MISSING_ZONES.format(self._entry_id)
        missing = sorted(z["name"] for z in zones if z["missing"])
        if not missing:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="missing_zones",
            translation_placeholders={"zones": ", ".join(missing)},
            data={"entry_id": self._entry_id},
        )


class _Sink:
    """AdapterSink that only cares about Zone-list changes."""

    def __init__(self, sync: ZoneSync) -> None:
        self._sync = sync

    @callback
    def on_snapshot(self, snapshot: Any) -> None:
        """Snapshots are not Zone sync's concern."""

    @callback
    def on_signal(self, signal: AdapterSignal) -> None:
        """Forward Zone-list changes."""
        self._sync._on_signal(signal)


@callback
def async_get_zone_sync(hass: HomeAssistant, entry_id: str) -> ZoneSync | None:
    """The running ZoneSync for a Mower entry, if any."""
    return hass.data.get(DOMAIN, {}).get("zone_sync", {}).get(entry_id)


@callback
def async_attach_zone_sync(
    hass: HomeAssistant, entry_id: str, adapter: MowerAdapter, store: Any
) -> ZoneSync:
    """Create, register and start the ZoneSync for a Mower entry."""
    sync = ZoneSync(hass, entry_id, adapter, store)
    hass.data.setdefault(DOMAIN, {}).setdefault("zone_sync", {})[entry_id] = sync
    sync.async_start()
    return sync


@callback
def async_detach_zone_sync(hass: HomeAssistant, entry_id: str) -> None:
    """Stop and forget a Mower's ZoneSync (unload)."""
    sync = hass.data.get(DOMAIN, {}).get("zone_sync", {}).pop(entry_id, None)
    if sync:
        sync.async_stop()
