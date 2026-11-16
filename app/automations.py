"""Automations: "when <trigger>, if <conditions>, then <actions>".

Triggers   device   {device_id, capability, op: changes|becomes|above|below|equals, value}
           time     {at: "HH:MM", days: ["mon", ...]}      (empty days = every day)
           interval {minutes: N}
Conditions device   {device_id, capability, op: becomes|equals|above|below|changes(=any)}  current state
           time     {after: "HH:MM", before: "HH:MM"}      (wraps past midnight when after > before)
           days     {days: ["sat", "sun"]}
Actions    command  {device_id, capability, value}
           notify   {message, title?}
           delay    {seconds}  (max 3600)
           webhook  {url, method: POST|GET, body?}

Device triggers fire on transitions only, so repeated identical reports don't re-run a rule.
Stored in /data/automations.json. Time-based rules use the NTP-corrected clock and the
configured timezone (app/timesync.py).
"""
from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import json
import logging
import math
import time
import uuid
from datetime import timedelta
from urllib.parse import urlsplit
from collections import deque
from pathlib import Path
from typing import Any

log = logging.getLogger("automations")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TRIGGER_OPS = {"changes", "becomes", "above", "below", "equals"}
MAX_DELAY = 3600
MAX_ACTIONS = 20
MAX_DEPTH = 3            # automation → command → automation … chains deeper than this are cut
CHAIN_WINDOW = 2.0       # seconds: a device change this soon after an automation's command counts as its result
MAX_CATCHUP_MIN = 15     # a stalled scheduler still fires time triggers missed within this many minutes
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("automation_depth", default=0)
_BLOCKED_HOSTS = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("100.100.100.200"),
                  ipaddress.ip_address("fd00:ec2::254")}


# ---------------------------------------------------------------- value helpers
_TRUE = {"true", "on", "yes", "1", "open", "locked", "detected"}
_FALSE = {"false", "off", "no", "0", "closed", "unlocked", "clear"}


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return True if s in _TRUE else False if s in _FALSE else None


def values_equal(actual: Any, wanted: Any) -> bool:
    if isinstance(actual, bool) or isinstance(wanted, bool):
        a, w = _as_bool(actual), _as_bool(wanted)
        if a is not None and w is not None:
            return a == w
    na, nw = _num(actual), _num(wanted)
    if na is not None and nw is not None:
        return na == nw
    return str(actual).strip().lower() == str(wanted).strip().lower()


def state_matches(op: str, value: Any, wanted: Any) -> bool:
    """Does the current value satisfy op/wanted? (used for conditions and trigger targets)"""
    if value is None:
        return False
    if op in ("becomes", "equals"):
        return values_equal(value, wanted)
    if op in ("above", "below"):
        a, w = _num(value), _num(wanted)
        if a is None or w is None:
            return False
        return a > w if op == "above" else a < w
    if op == "changes":
        return True
    return False


def trigger_fires(op: str, old: Any, new: Any, wanted: Any = None) -> bool:
    """Transition logic: fire only when the state *enters* the target."""
    if op == "changes":
        return old is not None and not values_equal(old, new)
    return state_matches(op, new, wanted) and not state_matches(op, old, wanted)


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


async def check_webhook_url(url: str) -> None:
    """Refuse webhooks to cloud-metadata, link-local and the container's own loopback addresses.
    LAN addresses stay allowed (a webhook to another box at home is a normal use)."""
    host = urlsplit(url).hostname
    if not host:
        raise ValueError("webhook URL has no host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"can't resolve {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip in _BLOCKED_HOSTS or ip.is_link_local or ip.is_loopback or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"webhook to {host} ({ip}) is not allowed")


def _say(v: Any) -> str:
    """Human wording for log lines."""
    return "on" if v is True else "off" if v is False else str(v).replace("_", " ")


def _hhmm(s: str) -> int:
    h, m = str(s).strip().split(":")[:2]
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {s!r}")
    return h * 60 + m


def in_window(minute_of_day: int, after: str | None, before: str | None) -> bool:
    a = _hhmm(after) if after else None
    b = _hhmm(before) if before else None
    if a is None and b is None:
        return True
    if a is None:
        return minute_of_day < b
    if b is None:
        return minute_of_day >= a
    if a <= b:
        return a <= minute_of_day < b
    return minute_of_day >= a or minute_of_day < b        # wraps past midnight


# ---------------------------------------------------------------- validation
def validate(a: dict) -> dict:
    """Normalize and check an automation; raises ValueError with a human message."""
    out = {"id": str(a.get("id") or uuid.uuid4().hex[:10])[:40], "name": str(a.get("name") or "Automation").strip()[:80],
           "enabled": bool(a.get("enabled", True)), "cooldown": min(7 * 86400, max(0, int(_num(a.get("cooldown")) or 0))),
           "last_run": a.get("last_run"), "run_count": int(a.get("run_count") or 0)}
    t = dict(a.get("trigger") or {})
    kind = t.get("type")
    if kind == "device":
        if not t.get("device_id") or not t.get("capability"):
            raise ValueError("Pick a device and what to watch")
        op = t.get("op") or "changes"
        if op not in TRIGGER_OPS:
            raise ValueError(f"Unknown trigger condition {op}")
        if op in ("above", "below") and _num(t.get("value")) is None:
            raise ValueError("Above/below needs a number")
        if op in ("becomes", "equals") and _blank(t.get("value")):
            raise ValueError("Choose the value the device should become")
        out["trigger"] = {"type": "device", "device_id": str(t["device_id"]), "capability": str(t["capability"]), "op": op,
                          "value": t.get("value")}
    elif kind == "time":
        _hhmm(t.get("at") or "")
        days = [d for d in (t.get("days") or []) if d in DAYS]
        out["trigger"] = {"type": "time", "at": t["at"], "days": days}
    elif kind == "interval":
        mins = _num(t.get("minutes"))
        if not mins or mins < 1 or mins > 7 * 24 * 60:
            raise ValueError("Interval must be between 1 minute and 1 week")
        out["trigger"] = {"type": "interval", "minutes": mins}
    else:
        raise ValueError("Choose when the automation runs")
    conds = []
    for c in a.get("conditions") or []:
        ck = c.get("type")
        if ck == "device":
            if not c.get("device_id") or not c.get("capability"):
                raise ValueError("A condition is missing its device")
            cop = c.get("op") if c.get("op") in TRIGGER_OPS else "equals"
            if cop in ("becomes", "equals") and _blank(c.get("value")):
                raise ValueError("A condition is missing the value to compare with")
            if cop in ("above", "below") and _num(c.get("value")) is None:
                raise ValueError("Above/below needs a number")
            conds.append({"type": "device", "device_id": str(c["device_id"]), "capability": str(c["capability"]),
                          "op": cop, "value": c.get("value")})
        elif ck == "time":
            if c.get("after"):
                _hhmm(c["after"])
            if c.get("before"):
                _hhmm(c["before"])
            conds.append({"type": "time", "after": c.get("after") or None, "before": c.get("before") or None})
        elif ck == "days":
            conds.append({"type": "days", "days": [d for d in (c.get("days") or []) if d in DAYS]})
        else:
            raise ValueError(f"Unknown condition {ck}")
    out["conditions"] = conds
    acts = []
    for x in (a.get("actions") or [])[:MAX_ACTIONS]:
        k = x.get("type")
        if k == "command":
            if not x.get("device_id") or not x.get("capability"):
                raise ValueError("A 'do' step is missing its device")
            if _blank(x.get("value")):
                raise ValueError("A 'do' step is missing the value to set")
            acts.append({"type": "command", "device_id": str(x["device_id"]), "capability": str(x["capability"]), "value": x.get("value")})
        elif k == "notify":
            acts.append({"type": "notify", "message": str(x.get("message") or "")[:500], "title": str(x.get("title") or "")[:80]})
        elif k == "delay":
            secs = _num(x.get("seconds"))
            if secs is None or secs < 0:
                raise ValueError("Wait needs a number of seconds")
            acts.append({"type": "delay", "seconds": min(MAX_DELAY, secs)})
        elif k == "webhook":
            url = str(x.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise ValueError("Webhook URL must start with http:// or https://")
            acts.append({"type": "webhook", "url": url, "method": "GET" if str(x.get("method")).upper() == "GET" else "POST",
                         "body": x.get("body")})
        else:
            raise ValueError(f"Unknown action {k}")
    if not acts:
        raise ValueError("Add at least one thing to do")
    out["actions"] = acts
    return out


# ---------------------------------------------------------------- engine
class AutomationEngine:
    def __init__(self, hub, clock, data_dir: Path) -> None:
        self.hub = hub
        self.clock = clock                    # app.timesync.TimeService (now(), tz)
        self.path = Path(data_dir) / "automations.json"
        self.items: dict[str, dict] = {}
        self.runs: deque = deque(maxlen=200)
        self.notifications: deque = deque(maxlen=100)
        self._last_fire: dict[str, float] = {}
        self._minute_key: dict[str, str] = {}
        self._interval_at: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._running: dict[str, asyncio.Task] = {}          # one in-flight run per rule
        self._commanded: dict[str, tuple[float, int]] = {}   # device_id -> (time, depth) of an automation's command
        self._last_tick: float | None = None
        self._loop_task: asyncio.Task | None = None
        self.http = None                      # set lazily (httpx) for webhooks
        self.load()

    # ----- persistence -----
    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            for a in data.get("automations", []):
                try:
                    v = validate(a)
                    self.items[v["id"]] = v
                except ValueError as e:
                    log.warning("skipping invalid automation %s: %s", a.get("name"), e)
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.error("could not read %s: %s", self.path, e)

    def save(self) -> None:
        from .config import write_private      # webhook URLs can carry tokens
        write_private(self.path, json.dumps({"automations": list(self.items.values())}, indent=2, default=str))

    def upsert(self, data: dict) -> dict:
        prev = self.items.get(str(data.get("id"))) if data.get("id") else None
        if prev:
            data = {**data, "last_run": prev.get("last_run"), "run_count": prev.get("run_count", 0)}
        a = validate(data)
        self.items[a["id"]] = a
        self._interval_at.pop(a["id"], None)
        if not a["enabled"]:
            self._cancel(a["id"])
        self.save()
        return a

    def delete(self, aid: str) -> bool:
        if self.items.pop(aid, None) is None:
            return False
        self._cancel(aid)
        self.save()
        return True

    def _cancel(self, aid: str) -> None:
        """Stop an in-flight run (e.g. a pending 'wait, then unlock') of a deleted/disabled rule."""
        t = self._running.pop(aid, None)
        if t and not t.done():
            t.cancel()

    # ----- lifecycle -----
    async def start(self) -> None:
        if not self._loop_task:
            self._loop_task = asyncio.create_task(self._scheduler())

    async def stop(self) -> None:
        tasks = [t for t in [self._loop_task, *self._tasks] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None
        self._tasks.clear()
        if self.http:
            await self.http.aclose()
            self.http = None

    def _spawn(self, coro) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:          # no loop (e.g. sync test) — run nothing
            coro.close()
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def start_run(self, aid: str, reason: str, force: bool = False, depth: int | None = None) -> None:
        """Start a run in the background unless one is already in flight for this rule."""
        a = self.items.get(aid)
        if not a:
            raise KeyError(aid)
        cur = self._running.get(aid)
        if cur and not cur.done():
            self._log({"automation": aid, "name": a["name"], "ts": self.clock.now(), "reason": reason, "ok": True,
                       "skipped": "already running", "steps": []})
            return
        d = _depth.get() if depth is None else depth
        if d >= MAX_DEPTH:
            self._log({"automation": aid, "name": a["name"], "ts": self.clock.now(), "reason": reason, "ok": True,
                       "skipped": "stopped a loop between automations", "steps": []})
            return
        ctx = contextvars.copy_context()
        ctx.run(_depth.set, d + 1)
        try:
            t = asyncio.get_running_loop().create_task(self.run(aid, reason, force), context=ctx)
        except RuntimeError:
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        self._running[aid] = t
        t.add_done_callback(lambda _t, aid=aid: self._running.pop(aid, None) if self._running.get(aid) is _t else None)

    # ----- device events (called synchronously from Hub.set_values) -----
    def on_change(self, device_id: str, changes: dict[str, tuple[Any, Any]]) -> None:
        for a in list(self.items.values()):
            t = a["trigger"]
            if not a["enabled"] or t["type"] != "device" or t["device_id"] != device_id or t["capability"] not in changes:
                continue
            old, new = changes[t["capability"]]
            if trigger_fires(t["op"], old, new, t.get("value")):
                depth = _depth.get()
                cmd = self._commanded.get(device_id)
                if cmd and time.monotonic() - cmd[0] < CHAIN_WINDOW:
                    depth = max(depth, cmd[1])         # this change was caused by an automation
                self.start_run(a["id"], f"{self._dev_name(device_id)} {t['capability'].replace('_', ' ')} → {_say(new)}",
                               depth=depth)

    # ----- scheduler -----
    async def _scheduler(self) -> None:
        while True:
            try:
                self.tick(self.clock.now())
            except Exception as e:  # noqa: BLE001
                log.exception("scheduler tick failed: %s", e)
            await asyncio.sleep(5)

    def tick(self, now: float) -> list[str]:
        """Fire due time/interval automations. Returns ids fired (handy for tests)."""
        local = self.clock.local(now)
        # every minute since the previous tick (so a stalled loop still fires what it missed), capped
        minutes = [local.replace(second=0, microsecond=0)]
        if self._last_tick is not None and now > self._last_tick:
            back = min(MAX_CATCHUP_MIN, int((now - self._last_tick) // 60) + 1)
            minutes = [minutes[0] - timedelta(minutes=i) for i in range(back, -1, -1)]
        self._last_tick = now
        fired = []
        for a in list(self.items.values()):
            if not a["enabled"]:
                continue
            t = a["trigger"]
            if t["type"] == "time":
                at = _hhmm(t["at"])
                for m in minutes:
                    key = m.strftime("%Y-%m-%d %H:%M")
                    if m.hour * 60 + m.minute == at and (not t["days"] or DAYS[m.weekday()] in t["days"]) \
                            and self._minute_key.get(a["id"]) != key:
                        self._minute_key[a["id"]] = key
                        fired.append(a["id"])
                        self.start_run(a["id"], f"scheduled at {t['at']}", depth=0)
                        break
            elif t["type"] == "interval":
                due = self._interval_at.setdefault(a["id"], now + t["minutes"] * 60)
                if now >= due:
                    self._interval_at[a["id"]] = now + t["minutes"] * 60
                    fired.append(a["id"])
                    self.start_run(a["id"], f"every {t['minutes']:g} min", depth=0)
        return fired

    # ----- conditions -----
    def conditions_pass(self, a: dict, now: float | None = None) -> tuple[bool, str]:
        local = self.clock.local(now)
        minute = local.hour * 60 + local.minute
        for c in a["conditions"]:
            if c["type"] == "time" and not in_window(minute, c.get("after"), c.get("before")):
                return False, f"outside {c.get('after') or '00:00'}–{c.get('before') or '24:00'}"
            if c["type"] == "days" and c["days"] and DAYS[local.weekday()] not in c["days"]:
                return False, "not on a selected day"
            if c["type"] == "device":
                dev = self.hub.devices.get(c["device_id"])
                cap = dev.capabilities.get(c["capability"]) if dev else None
                if not cap or not state_matches(c["op"], cap.value, c.get("value")):
                    return False, f"{self._dev_name(c['device_id'])} {c['capability']} is {cap.value if cap else 'unknown'}"
        return True, ""

    # ----- running -----
    def _dev_name(self, did: str) -> str:
        d = self.hub.devices.get(did)
        return d.name if d else did

    async def run(self, aid: str, reason: str, force: bool = False) -> dict:
        a = self.items.get(aid)
        if not a:
            raise KeyError(aid)
        now = self.clock.now()
        entry = {"automation": aid, "name": a["name"], "ts": now, "reason": reason, "ok": True, "skipped": None, "steps": []}
        if not force:
            if a["cooldown"] and now - self._last_fire.get(aid, 0) < a["cooldown"]:
                entry.update(ok=True, skipped="cooldown")
                self._log(entry)
                return entry
            ok, why = self.conditions_pass(a, now)
            if not ok:
                entry.update(skipped=why)
                self._log(entry)
                return entry
        self._last_fire[aid] = now
        for act in a["actions"]:
            try:
                entry["steps"].append(await self._do(act, a))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                entry["ok"] = False
                entry["steps"].append(f"failed: {e}")
                break
        cur = self.items.get(aid) or a          # the rule may have been edited while it ran
        cur["last_run"] = now
        cur["run_count"] = cur.get("run_count", 0) + 1
        try:
            self.save()
        except OSError as e:
            log.warning("could not save automations: %s", e)
        self._log(entry)
        return entry

    async def _do(self, act: dict, a: dict) -> str:
        k = act["type"]
        if k == "command":
            self._commanded[act["device_id"]] = (time.monotonic(), _depth.get())
            await self.hub.command(act["device_id"], act["capability"], act["value"])
            return f"{self._dev_name(act['device_id'])}: {act['capability'].replace('_', ' ')} → {_say(act['value'])}"
        if k == "notify":
            self.notify(act.get("title") or a["name"], act["message"])
            return f"notified: {act['message']}"
        if k == "delay":
            await asyncio.sleep(act["seconds"])
            return f"waited {act['seconds']:g}s"
        if k == "webhook":
            import httpx
            await check_webhook_url(act["url"])
            if self.http is None:
                self.http = httpx.AsyncClient(timeout=10, follow_redirects=False)
            body = act.get("body") or {"automation": a["name"], "ts": self.clock.now()}
            r = await (self.http.get(act["url"]) if act["method"] == "GET" else self.http.post(act["url"], json=body))
            r.raise_for_status()
            return f"webhook {act['method']} {r.status_code}"
        raise ValueError(k)

    def notify(self, title: str, message: str) -> dict:
        n = {"type": "notification", "title": title, "message": message, "ts": self.clock.now()}
        self.notifications.appendleft(n)
        broadcast = getattr(self.hub, "broadcast", None)
        if broadcast:
            broadcast(n)
        return n

    def _log(self, entry: dict) -> None:
        self.runs.appendleft(entry)
        lvl = logging.INFO if entry["ok"] else logging.WARNING
        log.log(lvl, "%s (%s): %s", entry["name"], entry["reason"], entry["skipped"] or "; ".join(entry["steps"]))
