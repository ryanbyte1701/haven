"""History storage and Prometheus metrics.

Backends (Settings > History & metrics, or `history:` in config.yaml):
  memory      last 24 h in RAM only (the original behavior; lost on restart)
  sqlite      /data/history.db, kept for `retention_days`; no extra containers needed
  prometheus  reads history from a Prometheus server that scrapes Haven's /metrics endpoint

The in-memory 24 h buffer (Hub.history) is always kept for instant sparklines and as a fallback.
Binary states are stored as numbers so they can be charted: see STATE_NUMBERS.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR

log = logging.getLogger("history")

RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "1y": 365 * 86400}
MAX_POINTS = 300
MAX_CHANGES = 5000          # state changes returned per capability; more are merged into buckets
HEARTBEAT = 3600            # an unchanged on/off state is still written once an hour (shows "still closed")
MAX_PENDING = 200_000       # samples buffered while the database can't be written (oldest dropped beyond)
CACHE_MIN_TTL = 5.0
CACHE_MAX_ENTRIES = 2000

# Text states -> numbers (so locks, doors, valves and UPS status can be graphed and alerted on)
STATE_NUMBERS: dict[str, dict[str, float]] = {
    "contact": {"open": 1, "closed": 0},
    "lock": {"unlocked": 1, "locked": 0, "jammed": 2},
    "valve": {"open": 1, "closed": 0, "opening": 1, "closing": 0},
    "ups_status": {"online": 0, "on_battery": 1, "low_battery": 2, "offline": 3},
    "alarm_panel": {"disarmed": 0, "armed_home": 1, "armed_away": 2, "armed_night": 3, "arming": 4,
                    "pending": 5, "triggered": 6},
}
BINARY_CAPS = set(STATE_NUMBERS)


def to_number(cap: str, value: Any) -> tuple[float, bool] | None:
    """Return (number, is_binary) or None if the value can't be stored."""
    if isinstance(value, bool):
        return (1.0 if value else 0.0), True
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (OverflowError, ValueError):     # e.g. a 400-digit integer from a broken MQTT payload
            return None
        return (f, False) if math.isfinite(f) else None
    if isinstance(value, str) and cap in STATE_NUMBERS:
        n = STATE_NUMBERS[cap].get(value.lower())
        return (float(n), True) if n is not None else None
    return None


# One query result for one capability: (points, kind "state"|"number"|None if unknown, truncated)
Result = tuple[list[dict], "str | None", bool]


# ------------------------------------------------------------------ stores
class MemoryStore:
    name = "memory"

    def __init__(self, hub) -> None:
        self.hub = hub
        self.ok, self.detail = True, "Last 24 hours, kept in memory (cleared on restart)"

    def ranges(self) -> list[str]:
        return ["1h", "24h"]

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def record(self, device_id: str, cap: str, value: Any, ts: float) -> None:
        pass   # Hub keeps the deque itself

    def _query(self, device_id: str, cap: str, start: float, end: float, max_points: int = MAX_POINTS) -> Result:
        states = getattr(self.hub, "state_history", {}).get((device_id, cap))
        if states:
            pts, trunc = compact_changes(state_changes(list(states), start, end), start, end)
            return pts, "state", trunc
        pts = [(t, v) for t, v in self.hub.history.get((device_id, cap), []) if start <= t <= end]
        binary = cap in BINARY_CAPS
        kind = "state" if binary else ("number" if (device_id, cap) in self.hub.history else None)
        return downsample(pts, start, end, max_points, binary=binary), kind, False

    async def query(self, device_id: str, cap: str, start: float, end: float, max_points: int = MAX_POINTS) -> list[dict]:
        return self._query(device_id, cap, start, end, max_points)[0]

    async def query_many(self, device_id: str, caps: list[str], start: float, end: float,
                         max_points: int = MAX_POINTS) -> dict[str, Result]:
        return {c: self._query(device_id, c, start, end, max_points) for c in caps}


def state_changes(rows: list[tuple[float, float]], start: float, end: float) -> list[dict]:
    """On/off and text states: every change (not averaged), starting with the state in force at `start`.
    Not truncated: pass the result through compact_changes() to bound its size."""
    out: list[dict] = []
    before = None
    for t, v in rows:
        if t < start:
            before = v
            continue
        if t > end:
            break
        if not out and before is not None and before != v:
            out.append({"t": round(start, 3), "v": before})
        if not out or out[-1]["v"] != v:
            out.append({"t": round(t, 3), "v": v})
    if not out and before is not None:
        out.append({"t": round(start, 3), "v": before})
    return out


def compact_changes(out: list[dict], start: float, end: float, limit: int = MAX_CHANGES) -> tuple[list[dict], bool]:
    """More than `limit` changes: merge them into time buckets keeping the highest state per bucket (an
    "open" or "unlocked" blip stays visible), instead of silently dropping the oldest. Returns (points, truncated)."""
    if len(out) <= limit:
        return out, False
    width = max((end - start) / max(1, limit // 2), 1e-9)
    merged: list[dict] = []
    cur_b = None
    for p in out:
        b = int((p["t"] - start) // width)
        if b == cur_b:
            if p["v"] > merged[-1]["v"]:
                merged[-1]["v"] = p["v"]
        else:
            merged.append({"t": p["t"], "v": p["v"]})
            cur_b = b
    collapsed: list[dict] = []
    for p in merged:
        if not collapsed or collapsed[-1]["v"] != p["v"]:
            collapsed.append(p)
    return collapsed[:limit], True


def downsample(pts: list[tuple[float, float]], start: float, end: float, max_points: int, binary: bool) -> list[dict]:
    if len(pts) <= max_points:
        return [{"t": t, "v": v} for t, v in pts]
    width = max((end - start) / max_points, 1e-9)
    buckets: dict[int, list] = {}
    for t, v in pts:
        buckets.setdefault(int((t - start) // width), []).append((t, v))
    out = []
    for b in sorted(buckets):
        rows = buckets[b]
        v = max(r[1] for r in rows) if binary else sum(r[1] for r in rows) / len(rows)
        out.append({"t": sum(r[0] for r in rows) / len(rows), "v": round(v, 4)})
    return out


class SqliteStore:
    """Samples in SQLite. Every database call runs on the store's own single thread, so slow history queries
    queue up behind each other instead of occupying asyncio's shared default executor (DNS lookups, file I/O)."""
    name = "sqlite"

    def __init__(self, path: Path, sample_seconds: float = 60, retention_days: float = 365) -> None:
        self.path = Path(path)
        self.sample = max(1.0, float(sample_seconds))
        self.retention = max(1.0, float(retention_days))
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()          # the connection
        self._plock = threading.Lock()         # the pending list (filled on the event loop, drained on the db thread)
        self._last: dict[tuple[str, str], tuple[float, float]] = {}
        self._pending: list[tuple[str, str, float, float, int]] = []
        self._tasks: list[asyncio.Task] = []
        self._ex: concurrent.futures.ThreadPoolExecutor | None = None
        self.ok, self.detail = False, "starting"

    def ranges(self) -> list[str]:
        return [r for r, secs in RANGES.items() if secs <= self.retention * 86400 + 1] or ["1h"]

    async def _run(self, fn, *args):
        if self._ex is None:
            self._ex = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="history-db")
        return await asyncio.get_running_loop().run_in_executor(self._ex, fn, *args)

    # -- lifecycle
    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            # created owner-only (sensor history reveals when people are home); umask is process-wide, so
            # create the file explicitly instead of changing it
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("CREATE TABLE IF NOT EXISTS samples (device TEXT NOT NULL, cap TEXT NOT NULL, ts REAL NOT NULL, "
                   "v REAL NOT NULL, binary INTEGER NOT NULL DEFAULT 0)")
        # covering index: every history query is answered from the index alone (no table lookups)
        db.execute("CREATE INDEX IF NOT EXISTS samples_cover ON samples(device, cap, ts, v, binary)")
        db.execute("DROP INDEX IF EXISTS samples_dct")       # superseded by samples_cover
        db.execute("CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts)")
        db.commit()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(f"{self.path}{suffix}", 0o600)
            except OSError:
                pass
        self._db = db

    async def start(self) -> None:
        await self._run(self._open)
        await self._run(self.prune)
        self.ok, self.detail = True, f"Saved to {self.path.name}, kept {self.retention:g} days"
        self._tasks = [asyncio.create_task(self._flush_loop()), asyncio.create_task(self._prune_loop())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        try:
            await self._run(self.flush)
        except Exception as e:  # noqa: BLE001
            log.warning("history: final write failed (%d samples kept in memory): %s", len(self._pending), e)

        def close():
            with self._lock:
                if self._db:
                    self._db.close()
                    self._db = None
        await self._run(close)
        if self._ex is not None:
            self._ex.shutdown(wait=False)
            self._ex = None

    # -- writing
    def record(self, device_id: str, cap: str, value: Any, ts: float) -> None:
        conv = to_number(cap, value)
        if conv is None:
            return
        v, binary = conv
        key = (device_id, cap)
        last = self._last.get(key)
        if last is not None:
            if binary:     # on/off and states: only changes, plus an hourly heartbeat
                if v == last[1] and ts - last[0] < HEARTBEAT:
                    return
            elif ts - last[0] < self.sample:
                return
        self._last[key] = (ts, v)
        with self._plock:
            self._pending.append((device_id, cap, ts, v, int(binary)))
            if len(self._pending) > MAX_PENDING:
                del self._pending[:len(self._pending) - MAX_PENDING]

    def flush(self) -> None:
        with self._plock:
            batch, self._pending = self._pending, []
        if not batch:
            return
        try:
            with self._lock:
                if not self._db:
                    raise _NotOpen()
                try:
                    self._db.executemany("INSERT INTO samples(device, cap, ts, v, binary) VALUES (?,?,?,?,?)", batch)
                    self._db.commit()
                except Exception:
                    self._db.rollback()
                    raise
        except BaseException as e:
            with self._plock:          # put the batch back in front of anything recorded meanwhile
                self._pending[:0] = batch
                if len(self._pending) > MAX_PENDING:
                    del self._pending[:len(self._pending) - MAX_PENDING]
            if isinstance(e, _NotOpen):
                return
            raise

    def take_pending(self) -> list[tuple[str, str, float, float, int]]:
        with self._plock:
            batch, self._pending = self._pending, []
        return batch

    def requeue(self, batch: list[tuple[str, str, float, float, int]]) -> None:
        with self._plock:
            self._pending[:0] = batch

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self._run(self.flush)
                self.ok, self.detail = True, f"Saved to {self.path.name}, kept {self.retention:g} days"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.ok, self.detail = False, f"write failed: {e}"
                log.warning("history write failed (%d samples waiting): %s", len(self._pending), e)

    def prune(self, now: float | None = None) -> int:
        cutoff = (now or time.time()) - self.retention * 86400
        with self._lock:
            if not self._db:
                return 0
            n = self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
            self._db.commit()
        return n

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(86400)
            try:
                await self._run(self.prune)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("history prune failed: %s", e)

    # -- reading
    def _query(self, device_id: str, cap: str, start: float, end: float, max_points: int) -> Result:
        width = max((end - start) / max(1, max_points), 1e-6)
        with self._lock:
            if not self._db:
                return [], None, False
            kind = self._db.execute("SELECT binary FROM samples WHERE device=? AND cap=? ORDER BY ts DESC LIMIT 1",
                                    (device_id, cap)).fetchone()
            if kind is None:
                return [], None, False
            if kind[0]:
                # states: every change in the range (plus the state in force when it starts), never averaged
                prev = self._db.execute("SELECT ts, v FROM samples WHERE device=? AND cap=? AND ts<? ORDER BY ts DESC LIMIT 1",
                                        (device_id, cap, start)).fetchall()
                rows = self._db.execute(
                    "SELECT ts, v FROM (SELECT ts, v, LAG(v) OVER (ORDER BY ts) AS pv FROM samples "
                    "WHERE device=? AND cap=? AND ts>=? AND ts<=?) WHERE pv IS NULL OR pv != v ORDER BY ts",
                    (device_id, cap, start, end)).fetchall()
                pts, trunc = compact_changes(state_changes(prev + rows, start, end), start, end)
                return pts, "state", trunc
            rows = self._db.execute(
                "SELECT AVG(ts), CASE WHEN MAX(binary)=1 THEN MAX(v) ELSE AVG(v) END "
                "FROM samples WHERE device=? AND cap=? AND ts>=? AND ts<=? "
                "GROUP BY CAST((ts - ?) / ? AS INTEGER) ORDER BY 1",
                (device_id, cap, start, end, start, width)).fetchall()
        return [{"t": round(t, 3), "v": round(v, 4)} for t, v in rows], "number", False

    def _query_many(self, device_id: str, caps: list[str], start: float, end: float, max_points: int,
                    flush: bool) -> dict[str, Result]:
        if flush:
            try:
                self.flush()                    # once per request, so the newest samples are included
            except Exception as e:  # noqa: BLE001 - reading still works; the flush loop reports the error
                log.debug("history flush before query failed: %s", e)
        return {c: self._query(device_id, c, start, end, max_points) for c in caps}

    async def query_many(self, device_id: str, caps: list[str], start: float, end: float,
                         max_points: int = MAX_POINTS, flush: bool = True) -> dict[str, Result]:
        return await self._run(self._query_many, device_id, list(caps), start, end, max_points, flush)

    async def query(self, device_id: str, cap: str, start: float, end: float, max_points: int = MAX_POINTS) -> list[dict]:
        return (await self.query_many(device_id, [cap], start, end, max_points))[cap][0]


class _NotOpen(Exception):
    pass


def _promql_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class PrometheusStore:
    name = "prometheus"

    def __init__(self, hub, url: str, token: str | None = None) -> None:
        self.hub = hub
        self.url = (url or "http://prometheus:9090").rstrip("/")
        self.token = token
        self.memory = MemoryStore(hub)
        self.ok, self.detail = True, f"Reading from {self.url}"
        self._http = None

    def ranges(self) -> list[str]:
        return list(RANGES)

    async def start(self) -> None:
        import httpx
        self._http = httpx.AsyncClient(timeout=10)
        try:
            r = await self._http.get(f"{self.url}/-/ready")
            self.ok = r.status_code == 200
            self.detail = f"Reading from {self.url}" if self.ok else f"{self.url} not ready ({r.status_code})"
        except Exception as e:  # noqa: BLE001
            self.ok, self.detail = False, f"can't reach {self.url}: {e}. Showing the last 24 h from memory."

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()

    def record(self, device_id: str, cap: str, value: Any, ts: float) -> None:
        pass   # Prometheus scrapes /metrics

    async def query(self, device_id: str, cap: str, start: float, end: float, max_points: int = MAX_POINTS) -> list[dict]:
        step = max(15, int((end - start) / max_points))
        q = f'haven_value{{device_id="{_promql_escape(device_id)}",capability="{_promql_escape(cap)}"}}'
        try:
            r = await self._http.get(f"{self.url}/api/v1/query_range",
                                     params={"query": q, "start": start, "end": end, "step": step})
            r.raise_for_status()
            res = r.json()["data"]["result"]
            self.ok, self.detail = True, f"Reading from {self.url}"
            if not res:
                return []
            return [{"t": float(t), "v": float(v)} for t, v in res[0]["values"] if v not in ("NaN", "+Inf", "-Inf")]
        except Exception as e:  # noqa: BLE001
            self.ok, self.detail = False, f"can't reach {self.url}: {e}. Showing the last 24 h from memory."
            return await self.memory.query(device_id, cap, start, end, max_points)

    async def query_many(self, device_id: str, caps: list[str], start: float, end: float,
                         max_points: int = MAX_POINTS) -> dict[str, Result]:
        out: dict[str, Result] = {}
        for c in caps:      # one at a time: a device page must not fire 40 parallel requests at Prometheus
            kind = self.memory._query(device_id, c, start, start, 1)[1]   # Prometheus has no binary flag
            out[c] = (await self.query(device_id, c, start, end, max_points), kind, False)
        return out


# ------------------------------------------------------------------ manager
class History:
    """Owns the active store; reconfigured live when the Settings page saves `history`."""

    def __init__(self, hub) -> None:
        self.hub = hub
        self.cfg: dict = {}
        self.store: MemoryStore | SqliteStore | PrometheusStore = MemoryStore(hub)
        self._lock = asyncio.Lock()
        self._swap_buf: list[tuple[str, str, Any, float]] | None = None   # samples arriving during configure()
        self._cache: dict[tuple, tuple[float, Result]] = {}

    @property
    def enabled(self) -> bool:
        return self.cfg.get("enabled", True)

    @property
    def metrics_enabled(self) -> bool:
        return bool(self.cfg.get("metrics_enabled", True)) and self.enabled

    @property
    def metrics_token(self) -> str:
        return str(self.cfg.get("metrics_token") or "")

    @property
    def status(self) -> str:
        return ("ok" if self.store.ok else "error") + f": {self.store.name}"

    async def configure(self, cfg: dict | None, data_dir: Path | None = None) -> None:
        async with self._lock:
            self.cfg = dict(cfg or {})
            backend = self.cfg.get("backend", "sqlite") if self.enabled else "memory"
            old, new = self.store, None
            self._swap_buf = []         # from here until the new store is in place, record() buffers here
            try:
                try:
                    await old.stop()
                except Exception as e:  # noqa: BLE001
                    log.warning("history: stopping %s failed: %s", old.name, e)
                leftover = old.take_pending() if isinstance(old, SqliteStore) else []
                if backend == "sqlite":
                    new = SqliteStore((data_dir or DATA_DIR) / "history.db",
                                      self.cfg.get("sample_seconds") or 60, self.cfg.get("retention_days") or 365)
                    if leftover and new.path == old.path:
                        new.requeue(leftover)    # samples the old store could not write: same file, keep them
                elif backend == "prometheus":
                    new = PrometheusStore(self.hub, self.cfg.get("prometheus_url") or "http://prometheus:9090")
                else:
                    new = MemoryStore(self.hub)
                await new.start()
                self.store = new
                log.info("history backend: %s", new.name)
            except Exception as e:  # noqa: BLE001
                log.error("history backend %s failed (%s); using memory", backend, e)
                self.store = MemoryStore(self.hub)
                self.store.ok, self.store.detail = False, f"{backend} failed: {e}. Using memory."
            finally:
                buf, self._swap_buf = self._swap_buf or [], None
                self._cache.clear()
                for args in buf:
                    self.record(*args)

    async def stop(self) -> None:
        await self.store.stop()

    def record(self, device_id: str, cap: str, value: Any, ts: float) -> None:
        if self._swap_buf is not None:
            if len(self._swap_buf) < MAX_PENDING:
                self._swap_buf.append((device_id, cap, value, ts))
            return
        try:
            self.store.record(device_id, cap, value, ts)
        except Exception as e:  # noqa: BLE001
            log.debug("history record failed: %s", e)

    async def query(self, device_id: str, cap: str, range_name: str = "24h") -> list[dict]:
        return (await self.query_many(device_id, [cap], range_name))["series"][cap]

    async def query_many(self, device_id: str, caps: list[str], range_name: str = "24h") -> dict:
        """Several capabilities of one device, one after another on the store's own thread (one flush).
        Returns {"series", "kinds", "truncated", "start", "end"}; results are cached for half a chart bucket."""
        if range_name not in RANGES:
            range_name = "24h"
        secs = RANGES[range_name]
        end = time.time()
        start = end - secs
        ttl = max(CACHE_MIN_TTL, secs / MAX_POINTS / 2)
        store = self.store
        results: dict[str, Result] = {}
        todo = []
        for c in caps:
            hit = self._cache.get((id(store), device_id, c, range_name))
            if hit and hit[0] > end:
                results[c] = hit[1]
            else:
                todo.append(c)
        if todo:
            try:
                got = await store.query_many(device_id, todo, start, end, MAX_POINTS)
            except Exception as e:  # noqa: BLE001 - e.g. the store was swapped out mid-request
                log.warning("history query failed: %s", e)
                got = {c: ([], None, False) for c in todo}
            mem = MemoryStore(self.hub)
            for c in todo:
                pts, kind, trunc = got.get(c, ([], None, False))
                if secs <= RANGES["24h"] and len(pts) < 2 and store.name != "memory":
                    # store still filling up (just enabled / fresh install): use the in-memory 24 h buffer
                    m = mem._query(device_id, c, start, end, MAX_POINTS)
                    if len(m[0]) > len(pts):
                        pts, kind, trunc = m[0], kind or m[1], m[2]
                results[c] = (pts, kind, trunc)
                if len(self._cache) >= CACHE_MAX_ENTRIES:
                    now = time.time()
                    self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
                    if len(self._cache) >= CACHE_MAX_ENTRIES:
                        self._cache.clear()
                self._cache[(id(store), device_id, c, range_name)] = (end + ttl, results[c])
        return {"series": {c: results[c][0] for c in caps},
                "kinds": {c: self._kind(device_id, c, results[c][1]) for c in caps},
                "truncated": {c: True for c in caps if results[c][2]},
                "start": round(start, 3), "end": round(end, 3)}

    async def device_history(self, dev, range_name: str = "24h", limit: int = 40) -> dict:
        """Every chartable capability of one device (the device page's History tab): numbers, on/off and known
        text states, including capabilities whose current value is unknown (None after a restart) so their
        stored history still shows. Shape: {range, series, states, kinds, truncated, start, end}."""
        caps = [k for k, c in dev.capabilities.items()
                if c.value is None or isinstance(c.value, (bool, int, float)) or k in STATE_NUMBERS][:limit]
        res = await self.query_many(dev.id, caps, range_name)
        series = {k: pts for k, pts in res["series"].items()
                  if pts or dev.capabilities[k].value is not None}      # unknown value and no history: nothing to draw
        return {"range": range_name, "series": series,
                "states": {k: v for k, v in STATE_NUMBERS.items() if k in series},
                "kinds": {k: res["kinds"][k] for k in series},
                "truncated": {k: True for k in res["truncated"] if k in series},
                "start": res["start"], "end": res["end"]}

    def _kind(self, device_id: str, cap: str, stored: str | None) -> str:
        """"state" (timeline) or "number" (line chart): from the stored binary flag, else the current value."""
        if stored:
            return stored
        if cap in BINARY_CAPS or (device_id, cap) in getattr(self.hub, "state_history", {}):
            return "state"
        dev = getattr(self.hub, "devices", {}).get(device_id)
        c = dev.capabilities.get(cap) if dev else None
        return "state" if c is not None and isinstance(c.value, bool) else "number"

    def info(self) -> dict:
        return {"backend": self.store.name, "ranges": self.store.ranges(), "ok": self.store.ok,
                "detail": self.store.detail, "metrics": self.metrics_enabled,
                "metrics_auth": bool(self.metrics_token)}


# ------------------------------------------------------------------ /metrics exporter
def _esc(v: Any) -> str:
    return str(v if v is not None else "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**kw) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in kw.items()) + "}"


def _fmt(v: float) -> str:
    return repr(float(v)) if not float(v).is_integer() else f"{float(v):.1f}"


def render_metrics(hub) -> str:
    """Prometheus text exposition format 0.0.4."""
    fams: dict[str, tuple[str, list[str]]] = {
        "haven_value": ("Current value of a device capability (states mapped to numbers)", []),
        "haven_device_online": ("1 if the device is reachable", []),
        "haven_battery_percent": ("Battery level in percent", []),
        "haven_last_update_timestamp_seconds": ("Unix time of the device's most recent update", []),
        "haven_ups_charge_percent": ("UPS battery charge", []),
        "haven_ups_runtime_minutes": ("UPS estimated runtime", []),
        "haven_ups_load_percent": ("UPS load", []),
        "haven_ups_input_voltage": ("UPS input voltage", []),
        "haven_ups_status": ("UPS status: 0 online, 1 on battery, 2 low battery, 3 offline", []),
    }
    for d in list(hub.devices.values()):
        base = dict(device_id=d.id, device=d.name, room=d.room or "", source=d.source)
        fams["haven_device_online"][1].append(f"haven_device_online{_labels(**base)} {1 if d.online else 0}")
        last = max([c.updated or 0 for c in d.capabilities.values()] + [0])
        if last:
            fams["haven_last_update_timestamp_seconds"][1].append(
                f"haven_last_update_timestamp_seconds{_labels(**base)} {_fmt(round(last, 3))}")
        for name, c in d.capabilities.items():
            conv = to_number(name, c.value)
            if conv is None:
                continue
            fams["haven_value"][1].append(
                f"haven_value{_labels(**base, capability=name, unit=c.unit or '')} {_fmt(conv[0])}")
        bat = d.capabilities.get("battery")
        if bat and to_number("battery", bat.value):
            fams["haven_battery_percent"][1].append(f"haven_battery_percent{_labels(**base)} {_fmt(bat.value)}")
        if "ups_status" in d.capabilities:
            ul = _labels(device_id=d.id, device=d.name, room=d.room or "")
            for cap_name, fam in (("battery", "haven_ups_charge_percent"), ("runtime", "haven_ups_runtime_minutes"),
                                  ("load", "haven_ups_load_percent"), ("input_voltage", "haven_ups_input_voltage"),
                                  ("ups_status", "haven_ups_status")):
                c = d.capabilities.get(cap_name)
                conv = to_number(cap_name, c.value) if c else None
                if conv:
                    fams[fam][1].append(f"{fam}{ul} {_fmt(conv[0])}")
    out = []
    for name, (help_, lines) in fams.items():
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} gauge")
        out.extend(lines)
    return "\n".join(out) + "\n"
