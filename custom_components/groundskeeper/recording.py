"""Debug recording glue for one Mower entry (spec §15).

`DebugRecording` owns the entry's `RecordingWriter`, turns writer events
into Repairs issues, and is the single place `Director` records through.
Every method here swallows its own errors: no recording failure ever
reaches the core.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration

from .adapter import MowerAdapter
from .const import DOMAIN
from .recording_db import RecordingWriter

_LOGGER = logging.getLogger(__name__)

OPT_ENABLED = "debug_recording"
OPT_SIZE_WARN = "recording_size_warn_bytes"
DEFAULT_SIZE_WARN_BYTES = 1 << 30  # spec §15.3
LOW_DISK_FRACTION = 0.10
RECORDING_DIR = "groundskeeper"

_KEY = "recordings"


def recording_path(hass: HomeAssistant, entry_id: str) -> Path:
    """`/config/groundskeeper/recording_<entry_id>.db`."""
    return Path(hass.config.path(RECORDING_DIR, f"recording_{entry_id}.db"))


def redact(data: Any, terms: list[str]) -> Any:
    """Replace every serial-bearing term inside string values."""
    if isinstance(data, str):
        for term in terms:
            if term:
                data = data.replace(term, "**REDACTED**")
        return data
    if isinstance(data, dict):
        return {key: redact(value, terms) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact(value, terms) for value in data]
    return data


@callback
def async_get_recording(hass: HomeAssistant, entry_id: str) -> DebugRecording | None:
    """The running recording for a Mower entry, if it has one."""
    return hass.data.get(DOMAIN, {}).get(_KEY, {}).get(entry_id)


@callback
def async_all_recordings(hass: HomeAssistant) -> list[DebugRecording]:
    return list(hass.data.get(DOMAIN, {}).get(_KEY, {}).values())


def recording_enabled(entry: ConfigEntry) -> bool:
    """The explicit per-entry flag; entries from before the feature are off."""
    return bool(entry.options.get(OPT_ENABLED, False))


class DebugRecording:
    """One Mower's debug recording, from the event loop's side."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, adapter: MowerAdapter
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.adapter = adapter
        self.writer: RecordingWriter | None = None
        self._unsub_stop: CALLBACK_TYPE | None = None
        self._last_check: dict[str, tuple[str, str | None]] = {}

    # -- lifecycle -----------------------------------------------------------------

    async def async_start(self) -> None:
        """Open the file off the loop and start the writer thread."""
        try:
            integration = await async_get_integration(self.hass, DOMAIN)
            terms = list(self.adapter.redact)
            meta = redact(
                {
                    "integration_version": str(integration.version),
                    "model": self.adapter.model,
                    "firmware_version": self.adapter.firmware_version,
                    "adapter_domain": self.adapter.source_domain,
                },
                terms,
            )
            writer = RecordingWriter(
                recording_path(self.hass, self.entry.entry_id),
                meta,
                on_event=self._from_thread,
            )
            await self.hass.async_add_executor_job(writer.open)
            writer.start()
        except Exception as exc:  # noqa: BLE001 - recording must not break setup
            _LOGGER.error("Debug recording could not start: %s", exc)
            self._raise_failed(repr(exc))
            return
        self.writer = writer
        self.hass.data.setdefault(DOMAIN, {}).setdefault(_KEY, {})[
            self.entry.entry_id
        ] = self
        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._on_stop
        )

    async def _on_stop(self, _event: Event) -> None:
        await self.async_stop()

    async def async_stop(self) -> None:
        """Record the unload, flush and close the file."""
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None
        self.hass.data.get(DOMAIN, {}).get(_KEY, {}).pop(self.entry.entry_id, None)
        writer, self.writer = self.writer, None
        if writer is None:
            return
        writer.record("lifecycle", {"event": "unload"})
        try:
            await self.hass.async_add_executor_job(writer.close)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("recording close failed", exc_info=True)

    # -- recording -----------------------------------------------------------------

    def record(self, kind: str, payload: Any, run_id: str | None = None) -> None:
        """Enqueue one record (redacted); never raises."""
        writer = self.writer
        if writer is None:
            return
        try:
            writer.record(kind, redact(payload, list(self.adapter.redact)), run_id=run_id)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("recording failed", exc_info=True)

    def record_check(
        self,
        check_id: str,
        result: str,
        reason: str | None,
        run_id: str | None = None,
    ) -> None:
        """A `check` record, only when the Check's result or reason changed."""
        state = (result, reason)
        if self._last_check.get(check_id) == state:
            return
        self._last_check[check_id] = state
        self.record(
            "check", {"check": check_id, "result": result, "reason": reason}, run_id
        )

    def record_detector(
        self,
        detector: str,
        phase: str,
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        """A `detector` record: `estimate` at Run start, then `fire` and `clear`."""
        self.record(
            "detector", {"detector": detector, "phase": phase, **(details or {})}, run_id
        )

    def summary(self) -> dict[str, Any]:
        """Blocking; run in the executor."""
        if self.writer is None:
            return {"enabled": False}
        return self.writer.summary()

    # -- writer events (Repairs) -----------------------------------------------------

    def _from_thread(self, name: str, data: dict[str, Any]) -> None:
        try:
            self.hass.loop.call_soon_threadsafe(self.handle_event, name, data)
        except RuntimeError:  # loop already closed
            pass

    @callback
    def handle_event(self, name: str, data: dict[str, Any]) -> None:
        """Turn a writer event into logs and Repairs issues."""
        try:
            if name == "error":
                self._raise_failed(data.get("error", ""))
            elif name == "corrupt_renamed":
                _LOGGER.warning(
                    "Debug recording file was corrupt; renamed to %s", data["path"]
                )
            elif name in ("status", "paused", "resumed"):
                self._check_thresholds(data)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("recording event failed", exc_info=True)

    def _raise_failed(self, error: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"recording_failed_{self.entry.entry_id}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="recording_failed",
            translation_placeholders={"mower": self.entry.title, "error": error},
        )

    def _check_thresholds(self, data: dict[str, Any]) -> None:
        entry_id = self.entry.entry_id
        placeholders = {
            "mower": self.entry.title,
            "path": str(recording_path(self.hass, entry_id)),
        }
        if "size" in data:
            limit = self.entry.options.get(OPT_SIZE_WARN, DEFAULT_SIZE_WARN_BYTES)
            self._issue(
                f"recording_size_{entry_id}",
                "recording_size",
                data["size"] > limit,
                {**placeholders, "size_mb": f"{data['size'] / 2**20:.0f}"},
            )
        free, total = data.get("free"), data.get("total")
        if free is not None and total:
            self._issue(
                f"recording_low_disk_{entry_id}",
                "recording_low_disk",
                free / total < LOW_DISK_FRACTION,
                {**placeholders, "free_mb": f"{free / 2**20:.0f}"},
            )

    def _issue(self, issue_id: str, key: str, active: bool, placeholders: dict) -> None:
        if active:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=key,
                translation_placeholders=placeholders,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)


@callback
def async_notify_files_kept(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry removal keeps the recording; tell the user where it is (spec §15.8)."""
    path = recording_path(hass, entry.entry_id)
    if not path.exists():
        return
    persistent_notification.async_create(
        hass,
        f"The debug recording for {entry.title} was kept at `{path}`. "
        "Delete it yourself when you no longer need it.",
        title="Groundskeeper recording kept",
        notification_id=f"{DOMAIN}_recording_kept_{entry.entry_id}",
    )


async def async_pre_backup(hass: HomeAssistant) -> None:
    """HA backup is starting: checkpoint every WAL and pause writes."""
    for rec in async_all_recordings(hass):
        if rec.writer is not None:
            await hass.async_add_executor_job(rec.writer.hold_for_backup)


async def async_post_backup(hass: HomeAssistant) -> None:
    """HA backup finished: resume writes."""
    for rec in async_all_recordings(hass):
        if rec.writer is not None:
            rec.writer.release_backup()
