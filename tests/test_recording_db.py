"""Debug recording writer: SQLite file, writer thread, overflow, errors (#35)."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from custom_components.groundskeeper.recording_db import (
    BATCH_RECORDS,
    FLUSH_INTERVAL_S,
    QUEUE_MAX,
    RecordingWriter,
)

META = {
    "integration_version": "0.1.0",
    "model": "Luba 2",
    "firmware_version": "1.2.3",
    "adapter_domain": "mammotion",
}


def rows(path, sql="select ts, seq, run_id, kind, payload from records order by seq"):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


@pytest.fixture
def make(tmp_path):
    made = []
    events = []

    def _make(name="rec.db", **kw):
        kw.setdefault("on_event", lambda n, d: events.append((n, d)))
        w = RecordingWriter(tmp_path / name, META, **kw)
        made.append(w)
        w.open()
        return w

    _make.events = events
    yield _make
    for w in made:
        w.close()


def test_spec_constants() -> None:
    assert QUEUE_MAX == 10_000
    assert BATCH_RECORDS == 200
    assert FLUSH_INTERVAL_S == 2.0


def test_file_has_meta_and_records_tables(make, tmp_path, freezer) -> None:
    freezer.move_to("2026-05-01 12:00:00+00:00")
    w = make()
    w.start()
    w.record("lifecycle", {"event": "start"}, run_id="r1")
    w.flush()
    con = sqlite3.connect(tmp_path / "rec.db")
    meta = dict(con.execute("select key, value from meta"))
    con.close()
    assert meta["schema_version"] == "1"
    assert meta["integration_version"] == "0.1.0"
    assert meta["model"] == "Luba 2"
    assert meta["firmware_version"] == "1.2.3"
    assert meta["adapter_domain"] == "mammotion"
    [(ts, seq, run_id, kind, payload)] = rows(tmp_path / "rec.db")
    assert ts == 1777636800000  # UTC ms
    assert (seq, run_id, kind) == (1, "r1", "lifecycle")
    assert json.loads(payload) == {"event": "start"}


def test_batch_commits_at_200_records_without_flush(make, tmp_path) -> None:
    w = make(flush_interval=3600)
    w.start()
    for i in range(BATCH_RECORDS):
        w.record("snapshot", {"i": i})
    w.wait_idle()
    assert len(rows(tmp_path / "rec.db")) == BATCH_RECORDS


def test_commits_after_flush_interval(make, tmp_path) -> None:
    w = make(flush_interval=0.05)
    w.start()
    w.record("snapshot", {"i": 1})
    w.wait_idle()
    assert len(rows(tmp_path / "rec.db")) == 1


def test_overflow_drops_newest_and_records_dropped_count(make, tmp_path) -> None:
    w = make(queue_max=5)
    for i in range(8):  # writer not started: queue fills at 5
        w.record("snapshot", {"i": i})
    assert w.dropped == 3
    w.start()
    w.flush()
    w.record("snapshot", {"i": 99})  # space freed: marker, then the record
    w.flush()
    got = rows(tmp_path / "rec.db")
    assert [json.loads(r[4]).get("i") for r in got if r[3] == "snapshot"] == [0, 1, 2, 3, 4, 99]
    [marker] = [r for r in got if r[3] == "lifecycle"]
    assert json.loads(marker[4]) == {"event": "dropped", "count": 3}


def test_record_never_raises_and_write_error_turns_recording_off(make, tmp_path) -> None:
    w = make()
    w.start()
    w.record("snapshot", {"i": 1})
    w.flush()
    w._con.close()  # sabotage the connection under the writer
    w.record("snapshot", {"i": 2})
    w.wait_idle()
    w.record("snapshot", {"i": 3})  # still silent
    errors = [e for e in make.events if e[0] == "error"]
    assert len(errors) == 1  # reported once
    assert w.enabled is False
    assert len(rows(tmp_path / "rec.db")) == 1


def test_unserialisable_payload_never_raises(make) -> None:
    w = make()
    w.start()
    w.record("snapshot", {"x": object(), "s": {1, 2}})
    w.flush()
    assert w.enabled is True


def test_corrupt_file_is_renamed_and_new_one_started(make, tmp_path, freezer) -> None:
    freezer.move_to("2026-05-01 12:00:00+00:00")
    (tmp_path / "rec.db").write_bytes(b"this is not a sqlite file" * 100)
    w = make()
    w.start()
    w.record("snapshot", {"i": 1})
    w.flush()
    [corrupt] = list(tmp_path.glob("rec.db.corrupt-*"))
    assert corrupt.read_bytes().startswith(b"this is not")
    assert len(rows(tmp_path / "rec.db")) == 1
    assert ("corrupt_renamed", {"path": str(corrupt)}) in make.events


def test_pauses_below_min_free_disk_and_resumes(make, tmp_path) -> None:
    free = {"v": 10}
    w = make(min_free_bytes=100, free_bytes=lambda: free["v"])
    w.start()
    w.record("snapshot", {"i": 1})  # paused: nothing deleted, nothing written
    w.flush()
    assert rows(tmp_path / "rec.db") == []
    assert ("paused", {"free": 10}) in make.events
    free["v"] = 1000
    w.record("snapshot", {"i": 2})
    w.flush()
    kinds = [(r[3], json.loads(r[4])) for r in rows(tmp_path / "rec.db")]
    assert ("snapshot", {"i": 2}) in kinds
    assert ("lifecycle", {"event": "recording_resumed", "skipped": 1}) in kinds
    assert any(e[0] == "resumed" for e in make.events)
    assert w.enabled is True


def test_status_event_reports_size_and_free(make) -> None:
    w = make(free_bytes=lambda: 5 * 2**30, total_bytes=lambda: 10 * 2**30)
    w.start()
    w.record("snapshot", {"i": 1})
    w.flush()
    [(_, status)] = [e for e in make.events if e[0] == "status"][-1:]
    assert status["size"] > 0 and status["free"] == 5 * 2**30 and status["total"] == 10 * 2**30


def test_summary(make, freezer) -> None:
    freezer.move_to("2026-05-01 12:00:00+00:00")
    w = make()
    w.start()
    w.record("snapshot", {})
    freezer.move_to("2026-05-02 12:00:00+00:00")
    w.record("snapshot", {})
    w.flush()
    s = w.summary()
    assert s["enabled"] is True and s["records"] == 2 and s["dropped"] == 0
    assert s["oldest"] == "2026-05-01T12:00:00+00:00"
    assert s["newest"] == "2026-05-02T12:00:00+00:00"
    assert s["size_bytes"] > 0


def test_backup_hold_checkpoints_and_pauses_writes(make, tmp_path) -> None:
    w = make()
    w.start()
    w.record("snapshot", {"i": 1})
    w.hold_for_backup()
    assert not (tmp_path / "rec.db-wal").exists() or (tmp_path / "rec.db-wal").stat().st_size == 0
    assert len(rows(tmp_path / "rec.db")) == 1
    w.record("snapshot", {"i": 2})
    t = threading.Event()
    t.wait(0.1)
    assert len(rows(tmp_path / "rec.db")) == 1  # held
    w.release_backup()
    w.flush()
    assert len(rows(tmp_path / "rec.db")) == 2


def test_prune_oldest_whole_days_keeps_active_run(make, tmp_path, freezer) -> None:
    w = make()
    w.start()
    freezer.move_to("2026-05-01 12:00:00+00:00")
    w.record("snapshot", {"d": 1})
    w.record("transition", {"d": 1}, run_id="active")
    freezer.move_to("2026-05-10 12:00:00+00:00")
    w.record("snapshot", {"d": 10})
    w.flush()
    deleted = w.prune(max_age_days=5, protect_run_id="active")
    assert deleted == 1
    left = [(r[3], json.loads(r[4])["d"]) for r in rows(tmp_path / "rec.db")]
    assert left == [("transition", 1), ("snapshot", 10)]


def test_prune_by_size_removes_oldest_days_until_under_limit(make, tmp_path, freezer) -> None:
    w = make()
    w.start()
    for day in (1, 2, 3):
        freezer.move_to(f"2026-05-0{day} 12:00:00+00:00")
        for _ in range(50):
            w.record("snapshot", {"pad": "x" * 2000, "day": day})
    w.flush()
    full = w.summary()["size_bytes"]
    w.prune(max_bytes=int(full * 0.7))
    days = {json.loads(r[4])["day"] for r in rows(tmp_path / "rec.db")}
    assert 1 not in days and 3 in days
