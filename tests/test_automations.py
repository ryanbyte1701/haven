from conftest import ADMIN  # noqa: E402  (test admin session)
import asyncio
import socket
import struct
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.automations import AutomationEngine, in_window, state_matches, trigger_fires, validate
from app.hub import Hub
from app.models import Device, cap
from app import timesync


class Clock:
    """Injectable clock: fixed local time in a timezone."""
    def __init__(self, dt: datetime):
        self.t = dt.timestamp()
        self.tz = dt.tzinfo

    def now(self):
        return self.t

    def local(self, ts=None):
        return datetime.fromtimestamp(self.t if ts is None else ts, self.tz)


def make_hub():
    h = Hub()
    h.upsert_device(Device(id="d:door", name="Front Door", source="d", capabilities={"contact": cap("contact")}))
    h.upsert_device(Device(id="d:temp", name="Office", source="d", capabilities={"temperature": cap("temperature")}))
    h.upsert_device(Device(id="d:porch", name="Porch Light", source="d", capabilities={"light": cap("light")}))
    h.commands = []

    async def command(did, capability, value):
        h.commands.append((did, capability, value))
        h.set_values(did, {capability: value})
    h.command = command
    return h


def engine(tmp_path, dt):
    h = make_hub()
    e = AutomationEngine(h, Clock(dt), tmp_path)
    h.listeners.append(e.on_change)
    return h, e


PHX = ZoneInfo("America/Phoenix")


# ---------------- pure logic ----------------
def test_trigger_transitions():
    assert trigger_fires("becomes", "closed", "open", "open")
    assert not trigger_fires("becomes", "open", "open", "open")            # repeated report: no re-fire
    assert trigger_fires("above", 29.0, 31.0, 30)
    assert not trigger_fires("above", 31.0, 32.0, 30)                      # already above
    assert trigger_fires("below", 12, 9, "10")
    assert trigger_fires("changes", "closed", "open")
    assert not trigger_fires("changes", None, "open")                      # first report is not a change
    assert trigger_fires("becomes", False, True, "on")                     # bool vs "on"
    assert state_matches("equals", "Open", "open")


def test_time_window_and_validation():
    assert in_window(23 * 60, "22:00", "06:00") and in_window(5 * 60, "22:00", "06:00")
    assert not in_window(12 * 60, "22:00", "06:00")
    assert in_window(9 * 60, "08:00", "17:00") and not in_window(17 * 60, "08:00", "17:00")
    with pytest.raises(ValueError):
        validate({"trigger": {"type": "time", "at": "25:00"}, "actions": [{"type": "notify", "message": "x"}]})
    with pytest.raises(ValueError):
        validate({"trigger": {"type": "device", "device_id": "a", "capability": "b", "op": "above", "value": "hot"},
                  "actions": [{"type": "notify", "message": "x"}]})
    with pytest.raises(ValueError):
        validate({"trigger": {"type": "interval", "minutes": 5}, "actions": []})


# ---------------- engine ----------------
def test_device_trigger_condition_and_actions(tmp_path):
    async def go():
        h, e = engine(tmp_path, datetime(2026, 11, 16, 23, 30, tzinfo=PHX))      # Monday night
        a = e.upsert({"name": "Night door", "trigger": {"type": "device", "device_id": "d:door", "capability": "contact", "op": "becomes", "value": "open"},
                      "conditions": [{"type": "time", "after": "22:00", "before": "06:00"}],
                      "actions": [{"type": "notify", "message": "Front door opened"},
                                  {"type": "command", "device_id": "d:porch", "capability": "light", "value": True}]})
        h.set_values("d:door", {"contact": "closed"})
        h.set_values("d:door", {"contact": "open"})
        h.set_values("d:door", {"contact": "open"})          # same state again: must not re-run
        await asyncio.sleep(0.05)
        assert h.commands == [("d:porch", "light", True)]
        assert e.items[a["id"]]["run_count"] == 1
        assert e.notifications[0]["message"] == "Front door opened"
        assert e.runs[0]["ok"] and not e.runs[0]["skipped"]
        # daytime: condition blocks it
        e.clock.t = datetime(2026, 11, 17, 12, 0, tzinfo=PHX).timestamp()
        h.set_values("d:door", {"contact": "closed"}); h.set_values("d:door", {"contact": "open"})
        await asyncio.sleep(0.05)
        assert len(h.commands) == 1 and "outside" in e.runs[0]["skipped"]
        # persisted
        assert "Night door" in (tmp_path / "automations.json").read_text()
        await e.stop()
    asyncio.run(go())


def test_threshold_device_condition_and_cooldown(tmp_path):
    async def go():
        h, e = engine(tmp_path, datetime(2026, 11, 16, 15, 0, tzinfo=PHX))
        h.set_values("d:porch", {"light": False})
        e.upsert({"name": "Hot office", "cooldown": 600,
                  "trigger": {"type": "device", "device_id": "d:temp", "capability": "temperature", "op": "above", "value": 30},
                  "conditions": [{"type": "device", "device_id": "d:porch", "capability": "light", "op": "equals", "value": "off"}],
                  "actions": [{"type": "command", "device_id": "d:porch", "capability": "light", "value": True}]})
        for v in (25, 31, 29, 32):      # crosses 30 twice, but cooldown allows one run
            h.set_values("d:temp", {"temperature": v})
            await asyncio.sleep(0.02)
        assert h.commands == [("d:porch", "light", True)]
        await e.stop()
    asyncio.run(go())


def test_time_and_interval_triggers_with_injected_clock(tmp_path):
    async def go():
        h, e = engine(tmp_path, datetime(2026, 11, 16, 6, 59, 50, tzinfo=PHX))       # Monday
        wk = e.upsert({"name": "Weekday porch off", "trigger": {"type": "time", "at": "07:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
                       "actions": [{"type": "command", "device_id": "d:porch", "capability": "light", "value": False}]})
        we = e.upsert({"name": "Weekend", "trigger": {"type": "time", "at": "07:00", "days": ["sat", "sun"]},
                       "actions": [{"type": "notify", "message": "weekend"}]})
        iv = e.upsert({"name": "Every 15", "trigger": {"type": "interval", "minutes": 15}, "actions": [{"type": "notify", "message": "tick"}]})
        assert e.tick(e.clock.now()) == []                                   # 06:59 — nothing; interval armed
        e.clock.t += 15
        assert e.tick(e.clock.now()) == [wk["id"]]                           # 07:00 Monday
        e.clock.t += 20
        assert e.tick(e.clock.now()) == []                                   # same minute: once only
        e.clock.t += 15 * 60
        assert iv["id"] in e.tick(e.clock.now())
        await asyncio.sleep(0.05)
        assert ("d:porch", "light", False) in h.commands
        assert we["id"] not in [r["automation"] for r in e.runs]
        await e.stop()
    asyncio.run(go())


def test_failed_action_is_logged(tmp_path):
    async def go():
        h, e = engine(tmp_path, datetime(2026, 11, 16, 12, 0, tzinfo=PHX))

        async def broken(*a):
            raise RuntimeError("device offline")
        h.command = broken
        a = e.upsert({"name": "x", "trigger": {"type": "interval", "minutes": 5},
                      "actions": [{"type": "command", "device_id": "d:porch", "capability": "light", "value": True},
                                  {"type": "notify", "message": "never"}]})
        res = await e.run(a["id"], "test", force=True)
        assert not res["ok"] and "device offline" in res["steps"][-1] and not e.notifications
        await e.stop()
    asyncio.run(go())


# ---------------- API ----------------
def test_api_crud(tmp_path, monkeypatch):
    monkeypatch.setenv("HAVEN_DATA", str(tmp_path))
    from fastapi.testclient import TestClient
    import app.main as m
    with TestClient(m.app, headers=ADMIN) as c:
        body = {"name": "Leak alert", "trigger": {"type": "device", "device_id": "demo:sink_leak", "capability": "leak", "op": "becomes", "value": True},
                "actions": [{"type": "notify", "message": "Leak under the sink"}]}
        a = c.post("/api/automations", json=body).json()
        assert a["id"] and a["enabled"]
        assert c.post("/api/automations", json={**body, "actions": []}).status_code == 400
        assert [x["name"] for x in c.get("/api/automations").json()] == ["Leak alert"]
        upd = c.put(f"/api/automations/{a['id']}", json={**body, "name": "Sink leak", "enabled": False}).json()
        assert upd["name"] == "Sink leak" and not upd["enabled"]
        r = c.post(f"/api/automations/{a['id']}/run").json()
        assert r == {"ok": True, "started": True}           # runs in the background
        for _ in range(50):
            if c.get("/api/notifications").json():
                break
            time.sleep(0.02)
        assert c.get("/api/notifications").json()[0]["message"] == "Leak under the sink"
        assert c.get("/api/automations/log").json()[0]["reason"] == "run manually"
        assert c.delete(f"/api/automations/{a['id']}").json() == {"ok": True}
        assert c.delete(f"/api/automations/{a['id']}").status_code == 404
        t = c.get("/api/time").json()
        assert {"offset_ms", "timezone", "servers", "ok"} <= set(t)


# ---------------- SNTP ----------------
def test_sntp_against_fake_server():
    """A local UDP server that answers like an NTP server whose clock is 2.5 s ahead."""
    AHEAD = 2.5
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def serve():
        data, addr = srv.recvfrom(48)
        t2 = time.time() + AHEAD
        reply = bytearray(48)
        reply[0] = 0x24                    # LI 0, VN 4, mode 4 (server)
        reply[1] = 2                       # stratum
        reply[24:32] = data[40:48]         # originate = client's transmit
        reply[32:40] = timesync._to_ntp(t2)
        reply[40:48] = timesync._to_ntp(time.time() + AHEAD)
        srv.sendto(bytes(reply), addr)

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    offset, delay = timesync.query("127.0.0.1", port)
    srv.close()
    assert abs(offset - AHEAD) < 0.05 and 0 <= delay < 0.1
    # encode/decode round trip
    assert abs(timesync._from_ntp(timesync._to_ntp(1763300000.25)) - 1763300000.25) < 1e-6
    with pytest.raises(ValueError):
        timesync.parse_response(bytes(48), 0, 0)          # stratum 0 / mode 0 is rejected


def test_time_service_median_and_status():
    async def go():
        ts = timesync.TimeService()
        answers = iter([(0.010, 0.02), (0.012, 0.03), (5.0, 3.0), (0.011, 0.02)])   # one absurd sample
        ts.query = lambda host: next(answers)
        ts.servers = ["a", "b"]
        assert await ts.sync_once()
        assert abs(ts.offset - 0.011) < 0.002
        assert ts.status_line.startswith("ok · offset +11 ms")
        ts.tzname = "Not/AZone"
        assert ts.local().tzinfo.key == "UTC" and "unknown timezone" in ts.status_line
    asyncio.run(go())


def test_time_rule_saved_after_its_minute_does_not_fire_late():
    import datetime as dt
    from app.automations import AutomationEngine
    eng = AutomationEngine.__new__(AutomationEngine)
    eng.items = {"a": {"id": "a", "enabled": True, "trigger": {"type": "time", "at": "08:00", "days": []}}}
    eng._minute_key, eng._interval_at, eng._last_tick = {}, {}, None
    tz = dt.timezone.utc
    eng.clock = type("C", (), {"local": staticmethod(lambda t: dt.datetime.fromtimestamp(t, tz))})()
    eng.start_run = lambda *a, **k: None
    base = dt.datetime(2026, 1, 5, 8, 1, 5, tzinfo=tz).timestamp()
    assert eng.tick(base) == []            # 08:01:05
    assert eng.tick(base + 5) == []        # rule exists now; 08:00 already passed
    eng2 = AutomationEngine.__new__(AutomationEngine)
    eng2.__dict__.update(items=eng.items, _minute_key={}, _interval_at={}, _last_tick=None, clock=eng.clock, start_run=eng.start_run)
    t0 = dt.datetime(2026, 1, 5, 7, 59, 58, tzinfo=tz).timestamp()
    assert eng2.tick(t0) == [] and eng2.tick(t0 + 5) == ["a"]     # crosses 08:00 normally
    eng3 = AutomationEngine.__new__(AutomationEngine)
    eng3.__dict__.update(items=eng.items, _minute_key={}, _interval_at={}, _last_tick=None, clock=eng.clock, start_run=eng.start_run)
    assert eng3.tick(t0) == [] and eng3.tick(t0 + 180) == ["a"]   # stalled loop still catches up
