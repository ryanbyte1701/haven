"""Review round 3: history scale (H4), flush race (M9), device history kinds (M4), overflow guard (L8)."""
import asyncio
import os
import sqlite3
import stat
import time
import uuid

import pytest

from app import history as H
from app.hub import Hub
from app.models import Device, cap


def run(coro):
    return asyncio.run(coro)


def test_binary_values_only_on_change_plus_hourly_heartbeat(tmp_path):
    async def go():
        s = H.SqliteStore(tmp_path / "h.db", sample_seconds=60)
        await s.start()
        t0 = time.time() - 7200
        for i in range(0, 3600, 60):                     # an hour of "closed" every minute: one row
            s.record("d", "contact", "closed", t0 + i)
        s.record("d", "contact", "closed", t0 + 3600)    # hourly heartbeat
        s.record("d", "contact", "open", t0 + 3610)      # change: always
        for i in range(0, 600, 10):                      # numbers still sampled every sample_seconds
            s.record("d", "temperature", 20.0, t0 + i)
        s.flush()
        n = dict(s._db.execute("SELECT cap, COUNT(*) FROM samples GROUP BY cap").fetchall())
        assert n == {"contact": 3, "temperature": 10}
        await s.stop()
    run(go())


def test_migration_covering_index_own_executor_and_private_file(tmp_path):
    p = tmp_path / "h.db"
    db = sqlite3.connect(p)          # a database from the previous version
    db.execute("CREATE TABLE samples (device TEXT NOT NULL, cap TEXT NOT NULL, ts REAL NOT NULL, "
               "v REAL NOT NULL, binary INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE INDEX samples_dct ON samples(device, cap, ts)")
    db.execute("INSERT INTO samples VALUES ('d','temperature',?,21,0)", (time.time() - 10,))
    db.commit()
    db.close()

    async def go():
        s = H.SqliteStore(p)
        await s.start()
        idx = {r[1] for r in s._db.execute("PRAGMA index_list(samples)")}
        assert "samples_cover" in idx and "samples_dct" not in idx
        plan = " ".join(str(r) for r in s._db.execute(
            "EXPLAIN QUERY PLAN SELECT ts, v FROM samples WHERE device='d' AND cap='x' AND ts>=0"))
        assert "COVERING INDEX samples_cover" in plan
        assert s._ex._max_workers == 1
        threads = set()
        await s._run(lambda: threads.add(__import__("threading").current_thread().name))
        assert all(t.startswith("history-db") for t in threads)
        assert (await s.query("d", "temperature", time.time() - 60, time.time()))[0]["v"] == 21
        await s.stop()
    run(go())

    async def fresh():
        s = H.SqliteStore(tmp_path / "new.db")
        await s.start()
        await s.stop()
    run(fresh())
    assert stat.S_IMODE(os.stat(tmp_path / "new.db").st_mode) == 0o600


def test_flush_failure_requeues_batch(tmp_path):
    async def go():
        s = H.SqliteStore(tmp_path / "h.db", sample_seconds=1)
        await s.start()
        real = s._db

        class Broken:
            def executemany(self, *a):
                raise sqlite3.OperationalError("disk I/O error")

            def rollback(self):
                pass

        s.record("d", "temperature", 1.0, 1000.0)
        s._db = Broken()
        try:
            s.flush()
            raise AssertionError("flush should fail")
        except sqlite3.OperationalError:
            pass
        s.record("d", "temperature", 2.0, 1002.0)        # recorded while the batch was out
        assert [r[3] for r in s._pending] == [1.0, 2.0]   # failed batch back in front, nothing lost
        s._db = real
        s.flush()
        assert [r[0] for r in real.execute("SELECT v FROM samples ORDER BY ts")] == [1.0, 2.0]
        await s.stop()
    run(go())


def test_samples_during_reconfigure_are_kept(tmp_path):
    async def go():
        hub = Hub()
        h = H.History(hub)
        await h.configure({"backend": "sqlite", "sample_seconds": 1}, data_dir=tmp_path)
        old = h.store
        real_stop = old.stop
        now = time.time()

        async def slow_stop():
            h.record("d", "temperature", 30.0, now)       # arrives while the old store is shutting down
            await real_stop()
        old.stop = slow_stop
        await h.configure({"backend": "sqlite", "sample_seconds": 1, "retention_days": 30}, data_dir=tmp_path)
        assert h.store is not old
        assert (await h.query("d", "temperature", "1h"))[-1]["v"] == 30.0
        await h.stop()
    run(go())


def test_compact_changes_marks_truncated_and_keeps_blips():
    rows = [(float(i), float(i % 2)) for i in range(20000)]       # flapping every second
    pts = H.state_changes(rows, 0, 20000)
    out, trunc = H.compact_changes(pts, 0, 20000)
    assert trunc and len(out) <= H.MAX_CHANGES and out[0]["t"] == 0
    assert any(p["v"] == 1 for p in out)
    assert H.compact_changes(pts[:10], 0, 20000) == (pts[:10], False)


def test_query_many_kinds_cache_and_overflow(tmp_path):
    async def go():
        hub = Hub()
        h = H.History(hub)
        await h.configure({"backend": "sqlite", "sample_seconds": 1}, data_dir=tmp_path)
        now = time.time()
        h.record("d", "temperature", 20.0, now - 100)
        h.record("d", "temperature", 21.0, now - 50)
        h.record("d", "lock", "locked", now - 100)
        h.record("d", "lock", "unlocked", now - 50)
        h.record("d", "light", True, now - 40)
        r = await h.query_many("d", ["temperature", "lock", "light", "nothing"], "1h")
        assert r["kinds"] == {"temperature": "number", "lock": "state", "light": "state", "nothing": "number"}
        assert [p["v"] for p in r["series"]["lock"]] == [0, 1] and r["truncated"] == {}
        assert r["end"] - r["start"] == 3600
        h.record("d", "temperature", 99.0, now)                   # within the cache time: same answer
        again = await h.query_many("d", ["temperature"], "1h")
        assert again["series"]["temperature"] == r["series"]["temperature"]
        h._cache.clear()
        assert (await h.query("d", "temperature", "1h"))[-1]["v"] == 99.0
        assert H.to_number("x", 10 ** 400) is None             # OverflowError guarded
        await h.stop()
    run(go())
    hub = Hub()
    hub.upsert_device(Device(id="o:1", name="o", source="t", capabilities={"count": cap("count")}))
    hub.set_values("o:1", {"count": 10 ** 400})                  # neither crashes nor enters the chart buffer
    assert ("o:1", "count") not in hub.history


def test_device_history_endpoint_kinds_and_unknown_current_value(client_main):
    c, m = client_main
    did = f"k:{uuid.uuid4().hex[:8]}"
    m.hub.upsert_device(Device(id=did, name="Lock", source="t", capabilities={
        "lock": cap("lock"), "temperature": cap("temperature"), "battery": cap("battery"), "mode": cap("mode")}))
    now = time.time()
    m.history_store.record(did, "lock", "locked", now - 300)
    m.history_store.record(did, "lock", "unlocked", now - 200)
    m.history_store.record(did, "battery", 80, now - 300)    # stored, but no current value (as after a restart)
    m.hub.set_values(did, {"temperature": 20.5, "mode": "auto"})
    r = c.get(f"/api/devices/{did}/history?range=1h").json()
    assert set(r) >= {"range", "series", "states", "kinds", "start", "end", "truncated"}
    assert r["kinds"]["lock"] == "state" and r["kinds"]["temperature"] == "number"
    assert "battery" in r["series"] and r["kinds"]["battery"] == "number"
    assert "mode" not in r["series"]                     # text value: nothing to chart
    assert r["states"]["lock"]["unlocked"] == 1 and r["start"] < r["end"] <= time.time() + 1
    m.hub.devices.pop(did, None)



@pytest.fixture
def client_main():
    from fastapi.testclient import TestClient
    import app.main as m
    with TestClient(m.app, headers={"authorization": f"Bearer {m.DASH_TOKEN}"}) as c:
        yield c, m
