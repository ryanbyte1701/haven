"""SQLite history in the container: temp files, WAL fallback, disk full, damaged file, status wording."""
import asyncio
import os
import sqlite3
import time

from app import history as H
from app.hub import Hub


def run(coro):
    return asyncio.run(coro)


def test_temp_store_in_memory_and_wal_by_default(tmp_path):
    async def go():
        s = H.SqliteStore(tmp_path / "history.db")
        await s.start()
        assert s._db.execute("PRAGMA temp_store").fetchone()[0] == 2        # MEMORY: no temp file outside /data
        assert s.journal == "wal" and not s.journal_note
        assert s._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        await s.stop()
    run(go())


def test_falls_back_to_rollback_journal_when_wal_fails(tmp_path, monkeypatch):
    p = tmp_path / "history.db"

    async def first():                     # a database already in WAL mode (persisted in the file header)
        s = H.SqliteStore(p, sample_seconds=1)
        await s.start()
        s.record("d", "temperature", 20.0, time.time() - 30)
        await s.stop()
    run(first())

    def no_wal(db):                        # what a network share without shared memory does
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(H, "_enable_wal", no_wal)

    async def go():
        s = H.SqliteStore(p, sample_seconds=1)
        await s.start()
        assert s.journal == "delete" and "rollback journal" in s.journal_note
        assert s._db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert s._db.execute("PRAGMA locking_mode").fetchone()[0] == "normal"
        s.record("d", "temperature", 21.0, time.time())
        await s.flush_once()
        assert s.ok and "rollback journal" in s.detail
        pts = await s.query("d", "temperature", time.time() - 3600, time.time() + 1)
        assert [x["v"] for x in pts] == [20.0, 21.0]
        # another connection can read it (normal locking, not exclusive)
        other = sqlite3.connect(p)
        assert other.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2
        other.close()
        await s.stop()
    run(go())
    assert not os.path.exists(f"{p}-wal")


def test_disk_full_keeps_readings_and_says_what_to_do(tmp_path):
    async def go():
        s = H.SqliteStore(tmp_path / "history.db", sample_seconds=1)
        await s.start()
        pages = s._db.execute("PRAGMA page_count").fetchone()[0]
        s._db.execute(f"PRAGMA max_page_count={pages}")       # the disk is "full"
        now = time.time()
        for i in range(2000):
            s.record(f"dev{i}", "temperature", float(i), now)
        await s.flush_once()
        assert not s.ok
        assert "disk is full" in s.detail and "Free up space" in s.detail and "2000 waiting" in s.detail
        assert len(s._pending) == 2000
        h = H.History(Hub())
        h.store = s
        assert h.status.startswith("error: can't save readings: the disk is full")
        s._db.execute("PRAGMA max_page_count=1073741823")      # space freed
        await s.flush_once()
        assert s.ok and not s._pending
        assert s._db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2000
        await s.stop()
    run(go())


def test_damaged_file_is_moved_aside(tmp_path):
    p = tmp_path / "history.db"
    p.write_bytes(b"this is not a database at all" * 200)

    async def go():
        s = H.SqliteStore(p)
        await s.start()
        assert s.ok and "damaged" in s.journal_note
        assert [x.name for x in tmp_path.iterdir() if ".damaged-" in x.name]
        await s.stop()
    run(go())


def test_unwritable_folder_falls_back_to_memory_with_advice(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("a file where the data folder should be")

    async def go():
        h = H.History(Hub())
        await h.configure({"backend": "sqlite"}, data_dir=blocker / "sub")
        assert h.store.name == "memory" and not h.store.ok
        assert h.status.startswith("error: sqlite can't start:") and "last 24 hours in memory" in h.status
        await h.stop()
    run(go())


def test_explain_db_error_words():
    p = H.Path("/data/history.db")
    assert "Free up space" in H.explain_db_error(sqlite3.OperationalError("database or disk is full"), p)
    ro = H.explain_db_error(sqlite3.OperationalError("attempt to write a readonly database"), p)
    assert "can't write data/history.db" in ro and "0:0" in ro
    assert "network share" in H.explain_db_error(sqlite3.OperationalError("disk I/O error"), p)
    assert "rename or delete history.db" in H.explain_db_error(sqlite3.DatabaseError("database disk image is malformed"), p)
    assert "can't write" in H.explain_db_error(PermissionError(13, "Permission denied"), p)


def test_status_summary_and_live_retention_change(tmp_path):
    async def go():
        hub = Hub()
        h = H.History(hub)
        await h.configure({"backend": "sqlite", "sample_seconds": 1, "retention_days": 365}, data_dir=tmp_path)
        assert h.status.startswith("ok: SQLite · ") and h.status.endswith("no readings saved yet"), h.status
        now = time.time()
        h.record("a", "temperature", 1.0, now - 3 * 86400)
        h.record("b", "temperature", 2.0, now)
        await h.store.flush_once()
        h.store.refresh_stats()
        st = h.status
        assert st.startswith("ok: SQLite · ") and "3 days of history" in st and st.endswith("2 devices"), st
        info = h.info()
        assert info["devices"] == 2 and info["size_bytes"] > 0 and info["retention_days"] == 365 and "1y" in info["ranges"]
        assert "file" not in info                       # paths stay in "detail" (admins only)
        # a smaller retention applies at once (no restart): old readings go, ranges shrink
        await h.configure({"backend": "sqlite", "sample_seconds": 1, "retention_days": 2}, data_dir=tmp_path)
        assert "30d" not in h.info()["ranges"] and h.info()["devices"] == 1
        await h.stop()
    run(go())


def test_human_size_and_span():
    assert H.human_size(500) == "500 bytes"
    assert H.human_size(1300 * 1024) == "1.3 MB"
    assert H.human_span(60) == "less than an hour" and H.human_span(7200) == "2 hours"
    assert H.human_span(86400 * 14.5) == "14 days"
