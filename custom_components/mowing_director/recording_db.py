"""SQLite writer behind the debug recording (spec §15). No HA imports.

One `RecordingWriter` per Mower owns one SQLite file and one writer thread.
The event loop only calls `record`, which enqueues and never raises or
blocks. Everything blocking (`open`, `flush`, `summary`, `prune`, `close`,
the backup hold) is for executor threads and tests.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
QUEUE_MAX = 10_000
BATCH_RECORDS = 200
FLUSH_INTERVAL_S = 2.0
MIN_FREE_BYTES = 1 << 30  # recording pauses below 1 GB free (spec §15.3)
DAY_MS = 86_400_000

_SCHEMA = """
create table if not exists meta (key text primary key, value text not null);
create table if not exists records (
    ts integer not null, seq integer not null, run_id text,
    kind text not null, payload text not null
);
create index if not exists records_ts on records (ts);
"""

type EventCallback = Callable[[str, dict[str, Any]], None]


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


class _Control:
    """A request the writer thread runs in queue order."""

    def __init__(self, fn: Callable[[RecordingWriter], Any] | None = None) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.stop = False


class RecordingWriter:
    """One Mower's recording file and writer thread."""

    def __init__(
        self,
        path: Path,
        meta: dict[str, str | None],
        *,
        on_event: EventCallback | None = None,
        queue_max: int = QUEUE_MAX,
        batch_size: int = BATCH_RECORDS,
        flush_interval: float = FLUSH_INTERVAL_S,
        min_free_bytes: int = MIN_FREE_BYTES,
        free_bytes: Callable[[], int] | None = None,
        total_bytes: Callable[[], int] | None = None,
    ) -> None:
        self.path = Path(path)
        self._meta = meta
        self._on_event = on_event or (lambda *_: None)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_max)
        self._queue_max = queue_max
        self._batch_size = batch_size
        self._interval = flush_interval
        self._min_free = min_free_bytes
        self._free = free_bytes or (lambda: shutil.disk_usage(self.path.parent).free)
        self._total = total_bytes or (lambda: shutil.disk_usage(self.path.parent).total)
        self._lock = threading.Lock()
        self._seq = 0
        self._enqueued = 0
        self._finished = 0
        self._dropped = 0
        self._dropped_pending = 0
        self._paused = False
        self._skipped = 0
        self._failed = False
        self._release = threading.Event()
        self._hold_ready = threading.Event()
        self._con: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None

    # -- state ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """False once a write error turned recording off."""
        return not self._failed

    @property
    def dropped(self) -> int:
        """Records dropped by queue overflow since start."""
        return self._dropped

    # -- event loop side ----------------------------------------------------------

    def record(
        self,
        kind: str,
        payload: Any,
        *,
        run_id: str | None = None,
        ts: int | None = None,
    ) -> None:
        """Enqueue one record. Never raises, never blocks."""
        try:
            if self._failed:
                return
            body = json.dumps(payload, default=_jsonable, separators=(",", ":"))
            with self._lock:
                if self._dropped_pending and self._queue.qsize() < self._queue_max - 1:
                    marker = {"event": "dropped", "count": self._dropped_pending}
                    if self._put(self._row("lifecycle", json.dumps(marker), None, ts)):
                        self._dropped_pending = 0
                if not self._put(self._row(kind, body, run_id, ts)):
                    self._dropped += 1
                    self._dropped_pending += 1
        except Exception:  # noqa: BLE001 - no recording failure reaches the core
            _LOGGER.debug("recording enqueue failed", exc_info=True)

    def _row(self, kind: str, body: str, run_id: str | None, ts: int | None) -> tuple:
        self._seq += 1
        return (
            int(time.time() * 1000) if ts is None else ts,
            self._seq,
            run_id,
            kind,
            body,
        )

    def _put(self, row: tuple) -> bool:
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            return False
        self._enqueued += 1
        return True

    # -- lifecycle (blocking) -------------------------------------------------------

    def open(self) -> None:
        """Create or open the file; a corrupt file is renamed and replaced."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._con = self._connect()
        except sqlite3.DatabaseError:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            corrupt = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            for suffix in ("-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            os.replace(self.path, corrupt)
            self._on_event("corrupt_renamed", {"path": str(corrupt)})
            self._con = self._connect()
        row = self._con.execute("select max(seq) from records").fetchone()
        self._seq = row[0] or 0

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, check_same_thread=False)
        try:
            con.execute("pragma journal_mode=wal")
            con.executescript(_SCHEMA)
            meta = {"schema_version": str(SCHEMA_VERSION)}
            meta.update({k: v for k, v in self._meta.items() if v is not None})
            con.executemany(
                "insert or replace into meta (key, value) values (?, ?)", meta.items()
            )
            con.commit()
            con.execute("select count(*) from meta").fetchone()
        except sqlite3.DatabaseError:
            con.close()
            raise
        return con

    def start(self) -> None:
        """Start the writer thread."""
        self._thread = threading.Thread(
            target=self._run, name="mowing_director_recording", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """Flush, stop the thread and close the file."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._release.set()
            ctl = _Control()
            ctl.stop = True
            try:
                self._queue.put(ctl, timeout=5)
                ctl.done.wait(10)
            except queue.Full:
                pass
            thread.join(10)
        self._thread = None
        if self._con is not None:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
            self._con = None

    def _alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _call(self, fn: Callable[[RecordingWriter], Any], timeout: float = 60) -> Any:
        if not self._alive():
            return None
        ctl = _Control(fn)
        self._queue.put(ctl, timeout=5)
        ctl.done.wait(timeout)
        return ctl.result

    def flush(self) -> None:
        """Commit everything queued so far."""
        self._call(lambda _w: None)

    def wait_idle(self, timeout: float = 5.0) -> None:
        """Wait until every enqueued record is committed or discarded."""
        end = time.monotonic() + timeout
        while self._finished < self._enqueued and self._alive():
            if time.monotonic() > end:
                break
            threading.Event().wait(0.005)

    # -- writer thread ----------------------------------------------------------------

    def _run(self) -> None:
        batch: list[tuple] = []
        began = time.monotonic()
        try:
            while True:
                try:
                    item = self._queue.get(timeout=self._interval)
                except queue.Empty:
                    self._commit(batch)
                    batch = []
                    continue
                if isinstance(item, _Control):
                    self._commit(batch)
                    batch = []
                    if item.fn is not None:
                        item.result = item.fn(self)
                    item.done.set()
                    if item.stop:
                        return
                    continue
                if not batch:
                    began = time.monotonic()
                batch.append(item)
                if (
                    len(batch) >= self._batch_size
                    or time.monotonic() - began >= self._interval
                ):
                    self._commit(batch)
                    batch = []
        except Exception as exc:  # noqa: BLE001 - recording goes off, never the core
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        if self._failed:
            return
        self._failed = True
        _LOGGER.error("Debug recording write failed; recording is off: %s", exc)
        self._on_event("error", {"error": repr(exc)})

    def _commit(self, batch: list[tuple]) -> None:
        con = self._con
        assert con is not None
        free = self._free()
        if free < self._min_free:
            if not self._paused:
                self._paused = True
                self._on_event("paused", {"free": free})
            self._skipped += len(batch)
            self._finished += len(batch)
            return
        if not batch and not self._paused:
            return
        rows = list(batch)
        if self._paused:
            self._paused = False
            marker = {"event": "recording_resumed", "skipped": self._skipped}
            self._skipped = 0
            with self._lock:
                rows.insert(0, self._row("lifecycle", json.dumps(marker), None, None))
            self._on_event("resumed", {"free": free})
        con.executemany("insert into records values (?, ?, ?, ?, ?)", rows)
        con.commit()
        self._finished += len(batch)
        self._on_event(
            "status", {"size": self._size(), "free": free, "total": self._total()}
        )

    def _size(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(f"{self.path}{suffix}")
            except OSError:
                pass
        return total

    # -- reading, backup, pruning (blocking) ---------------------------------------------

    def summary(self) -> dict[str, Any]:
        """On/off, size, record count, oldest and newest, dropped count."""
        out: dict[str, Any] = {
            "enabled": self.enabled,
            "size_bytes": self._size(),
            "records": 0,
            "oldest": None,
            "newest": None,
            "dropped": self._dropped,
        }
        try:
            con = sqlite3.connect(self.path)
            try:
                count, lo, hi = con.execute(
                    "select count(*), min(ts), max(ts) from records"
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return out
        out["records"] = count
        for key, ts in (("oldest", lo), ("newest", hi)):
            if ts is not None:
                out[key] = datetime.fromtimestamp(ts / 1000, UTC).isoformat()
        return out

    def hold_for_backup(self) -> None:
        """Flush, checkpoint the WAL and stop writing until `release_backup`."""
        if not self._alive():
            return
        self._release.clear()
        self._hold_ready.clear()

        def _hold(w: RecordingWriter) -> None:
            assert w._con is not None
            w._con.execute("pragma wal_checkpoint(truncate)")
            w._hold_ready.set()
            w._release.wait(600)

        self._queue.put(_Control(_hold), timeout=5)
        self._hold_ready.wait(30)

    def release_backup(self) -> None:
        """Resume writing after a backup."""
        self._release.set()

    def prune(
        self,
        *,
        max_age_days: int | None = None,
        max_bytes: int | None = None,
        protect_run_id: str | None = None,
    ) -> int:
        """Delete the oldest whole days, then VACUUM. The active Run is kept."""
        now_ms = int(time.time() * 1000)  # read on the caller's thread
        return (
            self._call(
                lambda w: w._prune(max_age_days, max_bytes, protect_run_id, now_ms),
                timeout=600,
            )
            or 0
        )

    def _prune(
        self,
        max_age_days: int | None,
        max_bytes: int | None,
        protect: str | None,
        now_ms: int,
    ) -> int:
        con = self._con
        assert con is not None
        deleted = 0

        def delete_before(cutoff: int) -> int:
            cur = con.execute(
                "delete from records where ts < ? and (run_id is null or run_id != ?)",
                (cutoff, protect or ""),
            )
            con.commit()
            return cur.rowcount

        if max_age_days is not None:
            today = now_ms // DAY_MS * DAY_MS
            deleted += delete_before(today - max_age_days * DAY_MS)
            if deleted:
                con.execute("vacuum")
        if max_bytes is not None:
            while True:
                con.execute("pragma wal_checkpoint(truncate)")
                if self._size() <= max_bytes:
                    break
                row = con.execute(
                    "select min(ts) from records where run_id is null or run_id != ?",
                    (protect or "",),
                ).fetchone()
                if row[0] is None:
                    break
                n = delete_before(row[0] // DAY_MS * DAY_MS + DAY_MS)
                if not n:
                    break
                deleted += n
                con.execute("vacuum")
        return deleted
