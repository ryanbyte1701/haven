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
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR

log = logging.getLogger("history")

RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "1y": 365 * 86400}
MAX_POINTS = 300

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
        f = float(value)
        return (f, False) if math.isfinite(f) else None
    if isinstance(value, str) and cap in STATE_NUMBERS:
        n = STATE_NUMBERS[cap].get(value.lower())
        return (float(n), True) if n is not None else None
    return None


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

    async def query(self, device_id: str, cap: str, start: float, end: float, max_points: int = MAX_POINTS) -> list[dict]:
        pts = [(t, v) for t, v in self.hub.history.get((device_id, cap), []) if start <= t <= end]
        return downsample(pts, start, end, max_points, binary=cap in BINARY_CAPS)


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
    name = "sqlite"

    def __init__(self, path: Path, sample_seconds: float = 60, retention_days: float = 365) -> None:
        self.path = Path(path)
        self.sample = max(1.0, float(sample_seconds))
        self.retention = max(1.0, float(retention_days))
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._last: dict[tuple[str, str], tuple[float, float]] = {}
        self._pending: list[tuple[str, str, float, float, int]] = []
        self._tasks: list[asyncio.Task] = []
        self.ok, self.detail = False, "starting"

    def ranges(self) -> list[str]:
        return [r for r, secs in RANGES.items() if secs <= self.retention * 86400 + 1] or ["1h"]

    # -- lifecycle
    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("CREATE TABLE IF NOT EXISTS samples (device TEXT NOT NULL, cap TEXT NOT NULL, ts REAL NOT NULL, "
                   "v REAL NOT NULL, binary INTEGER NOT NULL DEFAULT 0)")
        db.execute("CREATE INDEX IF NOT EXISTS samples_dct ON samples(device, cap, ts)")
        db.execute("CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts)")
        db.commit()
        self._db = db

    async def start(self) -> None:
        await asyncio.to_thread(self._open)
        await asyncio.to_thread(self.prune)
        self.ok, self.detail = True, f"Saved to {self.path.name}, kept {self.retention:g} days"
        self._tasks = [asyncio.create_task(self._flush_loop()), asyncio.create_task(self._prune_loop())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.to_thread(self.flush)
        with self._lock:
            if self._db:
                self._db.close()
                self._db = None

    # -- writing
    def record(self, device_id: str, cap: str, value: Any, ts: float) -> None:
        conv = to_number(cap, value)
        if conv is None:
            return
        v, binary = conv
        key = (device_id, cap)
        last = self._last.get(key)
        if last is not None:
            due = ts - last[0] >= self.sample
            changed = binary and v != last[1]
            if not (due or changed):
                return
        self._last[key] = (ts, v)
        self._pending.append((device_id, cap, ts, v, int(binary)))

    def flush(self) -> None:
        batch, self._pending = self._pending, []
        if not batch:
            return
        with self._lock:
            if not self._db:
                return
            self._db.executemany("INSERT INTO samples(device, cap, ts, v, binary) VALUES (?,?,?,?,?)", batch)
            self._db.commit()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await asyncio.to_thread(self.flush)
                self.ok, self.detail = True, f"Saved to {self.path.name}, kept {self.retention:g} days"
            except Exception as e:  # noqa: BLE001
                self.ok, self.detail = False, f"write failed: {e}"
                log.warning("history write failed: %s", e)

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
                await asyncio.to_thread(self.prune)
            except Exception as e:  # noqa: BLE001
                log.warning("history prune failed: %s", e)

    # -- reading
    def _query(self, device_id: str, cap: str, start: float, end: float, max_points: int) -> list[dict]:
        width = max((end - start) / max(1, max_points), 1e-6)
        with self._lock:
            if not self._db:
                return []
            rows = self._db.execute(
                "SELECT AVG(ts), CASE WHEN MAX(binary)=1 THEN MAX(v) ELSE AVG(v) END "
                "FROM samples WHERE device=? AND cap=? AND ts>=? AND ts<=? "
                "GROUP BY CAST((ts - ?) / ? AS INTEGER) ORDER BY 1",
                (device_id, cap, start, end, start, width)).fetchall()
        return [{"t": round(t, 3), "v": round(v, 4)} for t, v in rows]

    async def query(self, device_id: str, cap: str, start: float, end: float, max_points: int = MAX_POINTS) -> list[dict]:
        await asyncio.to_thread(self.flush)   # include the newest samples
        return await asyncio.to_thread(self._query, device_id, cap, start, end, max_points)


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


# ------------------------------------------------------------------ manager
class History:
    """Owns the active store; reconfigured live when the Settings page saves `history`."""

    def __init__(self, hub) -> None:
        self.hub = hub
        self.cfg: dict = {}
        self.store: MemoryStore | SqliteStore | PrometheusStore = MemoryStore(hub)
        self._lock = asyncio.Lock()

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
            try:
                if backend == "sqlite":
                    new = SqliteStore((data_dir or DATA_DIR) / "history.db",
                                      self.cfg.get("sample_seconds") or 60, self.cfg.get("retention_days") or 365)
                elif backend == "prometheus":
                    new = PrometheusStore(self.hub, self.cfg.get("prometheus_url") or "http://prometheus:9090")
                else:
                    new = MemoryStore(self.hub)
                await old.stop()
                await new.start()
                self.store = new
                log.info("history backend: %s", new.name)
            except Exception as e:  # noqa: BLE001
                log.error("history backend %s failed (%s); using memory", backend, e)
                self.store = MemoryStore(self.hub)
                self.store.ok, self.store.detail = False, f"{backend} failed: {e}. Using memory."

    async def stop(self) -> None:
        await self.store.stop()

    def record(self, device_id: str, cap: str, value: Any, ts: float) -> None:
        try:
            self.store.record(device_id, cap, value, ts)
        except Exception as e:  # noqa: BLE001
            log.debug("history record failed: %s", e)

    async def query(self, device_id: str, cap: str, range_name: str = "24h") -> list[dict]:
        secs = RANGES.get(range_name, RANGES["24h"])
        end = time.time()
        pts = await self.store.query(device_id, cap, end - secs, end, MAX_POINTS)
        if secs <= RANGES["24h"] and len(pts) < 2 and self.store.name != "memory":
            # store still filling up (just enabled / fresh install): use the in-memory 24 h buffer
            mem = await MemoryStore(self.hub).query(device_id, cap, end - secs, end, MAX_POINTS)
            if len(mem) > len(pts):
                return mem
        return pts

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
