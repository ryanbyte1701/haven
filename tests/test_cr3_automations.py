"""Review round 3: multi-hop trust (M8), strict times, redaction and DST (L10), SNTP reply checks (L11)."""
import asyncio
import datetime as dt
import socket
import threading
import time
from zoneinfo import ZoneInfo

import pytest

import app.automations as au
from app import timesync
from app.automations import AutomationEngine, BLOCKED_MSG, validate
from app.hub import Hub
from app.models import Device, cap

PHX = ZoneInfo("America/Phoenix")


class Clock:
    def __init__(self, dt_, tz=PHX):
        self.t, self.tz = dt_.timestamp(), tz

    def now(self):
        return self.t

    def local(self, ts=None):
        return dt.datetime.fromtimestamp(self.t if ts is None else ts, self.tz)


def chain_hub():
    """mqtt lock (low trust) -> automation A turns a (trusted) switch on -> automation B unlocks the front door."""
    h = Hub()
    for did, c in (("mqtt:garage_lock", "lock"), ("tapo:sw", "switch"), ("yale:door", "lock")):
        h.upsert_device(Device(id=did, name=did, source=did.split(":")[0], capabilities={c: cap(c, writable=True)}))
    h.commands = []

    async def command(did, capability, value):
        h.commands.append((did, capability, value))
        # the switch reports its new state at once, as many integrations do
        h.set_values(did, {capability: value if capability != "lock" else ("unlocked" if value == "unlock" else "locked")})
    h.command = command
    return h


def rules(allow_unsafe_b=False):
    a = {"name": "A", "trigger": {"type": "device", "device_id": "mqtt:garage_lock", "capability": "lock", "op": "changes"},
         "actions": [{"type": "command", "device_id": "tapo:sw", "capability": "switch", "value": True}]}
    b = {"name": "B", "trigger": {"type": "device", "device_id": "tapo:sw", "capability": "switch", "op": "becomes",
                                  "value": True},
         "actions": [{"type": "command", "device_id": "yale:door", "capability": "lock", "value": "unlock"},
                     {"type": "notify", "message": "door open"}],
         "allow_unsafe": allow_unsafe_b}
    return a, b


async def _settle(e):
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not any(not t.done() for t in e._tasks):
            break


@pytest.mark.parametrize("allow", [False, True])
def test_two_hop_chain_from_unauthenticated_sensor(tmp_path, allow):
    async def go():
        h = chain_hub()
        e = AutomationEngine(h, Clock(dt.datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
        h.listeners.append(e.on_change)
        a, b = rules(allow)
        e.upsert(a)
        e.upsert(b)                    # B alone reads a trusted device: it saves fine
        h.set_values("mqtt:garage_lock", {"lock": "locked"})
        h.set_values("mqtt:garage_lock", {"lock": "unlocked"})     # spoofed MQTT message
        await _settle(e)
        return h, e
    h, e = asyncio.run(go())
    assert ("tapo:sw", "switch", True) in h.commands                 # webhook/switch-type actions still run
    unlocked = ("yale:door", "lock", "unlock") in h.commands
    run_b = next(r for r in e.runs if r["name"] == "B")
    if allow:
        assert unlocked and run_b["ok"]
    else:
        assert not unlocked and not run_b["ok"] and BLOCKED_MSG in run_b["steps"][0]
        assert any(BLOCKED_MSG in n["message"] for n in e.notifications)


def test_taint_follows_a_polled_state_change(tmp_path):
    """The switch's new state arrives later from the adapter's poll loop (outside the run's context)."""
    async def go():
        h = chain_hub()

        async def command(did, capability, value):
            h.commands.append((did, capability, value))
        h.command = command
        e = AutomationEngine(h, Clock(dt.datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
        h.listeners.append(e.on_change)
        a, b = rules()
        e.upsert(a)
        e.upsert(b)
        h.set_values("tapo:sw", {"switch": False})
        h.set_values("mqtt:garage_lock", {"lock": "locked"})
        h.set_values("mqtt:garage_lock", {"lock": "unlocked"})
        await _settle(e)
        h.set_values("tapo:sw", {"switch": True})        # from the poll loop, within CHAIN_WINDOW
        await _settle(e)
        return h, e
    h, e = asyncio.run(go())
    assert ("yale:door", "lock", "unlock") not in h.commands
    assert BLOCKED_MSG in next(r for r in e.runs if r["name"] == "B")["steps"][0]


def test_untainted_chain_and_manual_runs_still_unlock(tmp_path):
    async def go():
        h = chain_hub()
        e = AutomationEngine(h, Clock(dt.datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
        h.listeners.append(e.on_change)
        _, b = rules()
        e.upsert(b)
        h.set_values("tapo:sw", {"switch": False})
        h.set_values("tapo:sw", {"switch": True})                    # a person pressed the plug's button
        await _settle(e)
        return h
    assert ("yale:door", "lock", "unlock") in asyncio.run(go()).commands


def test_time_validation_and_normalisation():
    base = {"name": "t", "actions": [{"type": "notify", "message": "m"}]}
    assert validate({**base, "trigger": {"type": "time", "at": "7:05"}})["trigger"]["at"] == "07:05"
    for bad in ("24:00", "12:60", "12:5", "1200", "12:00:00", " ", "ab:cd", "-1:30"):
        with pytest.raises(ValueError, match="HH:MM"):
            validate({**base, "trigger": {"type": "time", "at": bad}})
    for bad in (1200, None, ["12:00"], {"h": 1}):
        with pytest.raises(ValueError, match="HH:MM"):
            validate({**base, "trigger": {"type": "time", "at": bad}})
    v = validate({**base, "trigger": {"type": "interval", "minutes": 5},
                  "conditions": [{"type": "time", "after": "8:00", "before": "17:30"}]})
    assert v["conditions"][0] == {"type": "time", "after": "08:00", "before": "17:30"}


def test_non_string_time_is_a_400(tmp_path):
    from fastapi.testclient import TestClient
    import app.main as m
    with TestClient(m.app, headers={"authorization": f"Bearer {m.DASH_TOKEN}"}) as c:
        r = c.post("/api/automations", json={"name": "x", "trigger": {"type": "time", "at": 1200},
                                             "actions": [{"type": "notify", "message": "m"}]})
        assert r.status_code == 400 and "HH:MM" in r.json()["detail"]


def test_webhook_url_redacted_in_run_log(tmp_path, caplog):
    async def go():
        e = AutomationEngine(Hub(), Clock(dt.datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
        a = e.upsert({"name": "hook", "trigger": {"type": "interval", "minutes": 5}, "actions": [
            {"type": "webhook", "url": "https://hooks.example.com/services/T000/B000/SECRETPATH?token=abc123"}]})

        async def failing(act, rule):
            raise RuntimeError(f"Client error for url '{act['url']}'")
        e._do = failing
        return await e.run(a["id"], "test", force=True)
    with caplog.at_level("WARNING", logger="automations"):
        entry = asyncio.run(go())
    text = " ".join(entry["steps"]) + caplog.text
    assert "SECRETPATH" not in text and "abc123" not in text and "failed" in text


def _engine_for(at, tz):
    eng = AutomationEngine.__new__(AutomationEngine)
    eng.items = {"a": {"id": "a", "enabled": True, "trigger": {"type": "time", "at": at, "days": []}}}
    eng._minute_key, eng._interval_at, eng._last_tick = {}, {}, None
    eng.clock = type("C", (), {"local": staticmethod(lambda t: dt.datetime.fromtimestamp(t, tz))})()
    eng.started = []
    eng.start_run = lambda aid, reason, **k: eng.started.append(aid)
    return eng


def test_spring_forward_time_in_skipped_hour_fires_once_after_gap():
    ny = ZoneInfo("America/New_York")           # 2026-03-08 02:00 -> 03:00
    eng = _engine_for("02:30", ny)
    t = dt.datetime(2026, 3, 8, 1, 58, tzinfo=ny).timestamp()
    fired = []
    for i in range(0, 10 * 60, 5):                # 01:58 EST .. 03:08 EDT, ticking every 5 s
        fired += eng.tick(t + i)
    assert fired == ["a"]
    # fall back (01:30 happens twice): fires once
    eng = _engine_for("01:30", ny)
    t = dt.datetime(2026, 11, 1, 0, 59, tzinfo=ny).timestamp()
    fired = []
    for i in range(0, 3 * 3600, 30):
        fired += eng.tick(t + i)
    assert fired == ["a"]


def test_normal_day_still_fires_at_its_minute():
    eng = _engine_for("08:00", dt.timezone.utc)
    t0 = dt.datetime(2026, 1, 5, 7, 59, 58, tzinfo=dt.timezone.utc).timestamp()
    assert eng.tick(t0) == [] and eng.tick(t0 + 5) == ["a"] and eng.tick(t0 + 65) == []


# ---------------- SNTP ----------------
def test_sntp_reply_must_echo_our_timestamp():
    t1 = time.time()
    req = timesync.build_request(t1)
    reply = bytearray(48)
    reply[0], reply[1] = 0x24, 2
    reply[24:32] = timesync._to_ntp(t1 - 100)          # somebody else's request
    reply[32:40] = reply[40:48] = timesync._to_ntp(t1)
    with pytest.raises(ValueError, match="does not match"):
        timesync.parse_response(bytes(reply), t1, t1, sent=req[40:48])
    reply[24:32] = req[40:48]
    timesync.parse_response(bytes(reply), t1, t1, sent=req[40:48])


def test_sntp_ignores_spoofed_and_stale_packets():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    spoof = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    spoof.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def serve():
        data, addr = srv.recvfrom(48)
        bogus = bytearray(48)
        bogus[0], bogus[1] = 0x24, 1
        bogus[24:32] = data[40:48]
        bogus[32:40] = bogus[40:48] = timesync._to_ntp(time.time() + 3600)    # "an hour ahead"
        spoof.sendto(bytes(bogus), addr)                       # right originate, wrong sender
        stale = bytearray(bogus)
        stale[24:32] = timesync._to_ntp(1.0)
        srv.sendto(bytes(stale), addr)                         # right sender, wrong originate
        time.sleep(0.05)
        good = bytearray(48)
        good[0], good[1] = 0x24, 2
        good[24:32] = data[40:48]
        good[32:40] = good[40:48] = timesync._to_ntp(time.time() + 1.0)
        srv.sendto(bytes(good), addr)

    threading.Thread(target=serve, daemon=True).start()
    offset, delay = timesync.query("127.0.0.1", port)
    srv.close()
    spoof.close()
    assert abs(offset - 1.0) < 0.2


def test_engine_module_exposes_reads_low_trust():
    assert au.reads_low_trust({"trigger": {"type": "device", "device_id": "ecowitt:x"}})
    assert not au.reads_low_trust({"trigger": {"type": "time"}, "conditions": []})
