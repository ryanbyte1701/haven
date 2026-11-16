"""Regression tests for the server-side review findings (S2–S8, B1–B8, F1/F2/F5/F7)."""
import asyncio
import json
import os
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.automations import AutomationEngine, check_webhook_url, validate
from app.hub import Hub
from app.models import Device, cap

PHX = ZoneInfo("America/Phoenix")


class Clock:
    def __init__(self, dt):
        self.t, self.tz = dt.timestamp(), dt.tzinfo

    def now(self):
        return self.t

    def local(self, ts=None):
        return datetime.fromtimestamp(self.t if ts is None else ts, self.tz)


def hub_with(*devs):
    h = Hub()
    for did, c in devs:
        h.upsert_device(Device(id=did, name=did, source="t", capabilities={c: cap(c)}))
    return h


# ---------------- hub ----------------
def test_nonfinite_values_are_dropped():
    h = hub_with(("t:x", "temperature"))
    h.set_values("t:x", {"temperature": 21.0})
    h.set_values("t:x", {"temperature": float("nan")})
    h.set_values("t:x", {"temperature": float("inf")})
    assert h.devices["t:x"].capabilities["temperature"].value == 21.0


def test_layout_put_is_partial(tmp_path):
    h = Hub()
    h.set_layout({"rooms": ["Kitchen"], "pinned": ["a"]})
    out = h.set_layout({"pinned": ["b"]})
    assert out["rooms"] == ["Kitchen"] and out["pinned"] == ["b"]


def test_override_unknown_device_is_refused():
    h = Hub()
    assert h.set_override("nope:1", name="x") is None
    assert "nope:1" not in h.overrides


# ---------------- API ----------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HAVEN_DATA", str(tmp_path))
    from fastapi.testclient import TestClient
    import app.main as m
    with TestClient(m.app) as c:
        yield c, m


def test_api_origin_404_config(client):
    c, m = client
    assert c.patch("/api/devices/missing:1", json={"name": "x"}).status_code == 404
    r = c.put("/api/layout", json={"pinned": []}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert c.put("/api/layout", json={"pinned": []}, headers={"origin": "http://testserver"}).status_code == 200
    cfg = c.get("/api/config").json()
    assert cfg["time_format"] in ("12h", "24h") and "timezone" in cfg


def test_auth_health_metrics_ws(client, monkeypatch):
    c, m = client
    monkeypatch.setattr(m, "DASH_TOKEN", "s3cret")
    assert c.get("/api/health").json() == {"ok": True, "auth": True}
    full = c.get("/api/health", headers={"authorization": "Bearer s3cret"}).json()
    assert {"adapters", "devices"} <= set(full)
    assert c.get("/api/devices").status_code == 401
    if m.history_store.metrics_token is None or not m.history_store.metrics_token:
        assert c.get("/metrics").status_code in (401, 403)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "wrong"})
            ws.receive_json()
    assert e.value.code == 4401
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "s3cret"})
        assert ws.receive_json()["type"] == "snapshot"
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
    assert e.value.code == 4403


def test_run_now_is_background(client):
    c, m = client
    a = c.post("/api/automations", json={"name": "n", "trigger": {"type": "interval", "minutes": 5},
                                         "actions": [{"type": "notify", "message": "hi"}]}).json()
    assert c.post(f"/api/automations/{a['id']}/run").json() == {"ok": True, "started": True}
    assert c.delete(f"/api/automations/{a['id']}").status_code == 200


def test_empty_values_rejected_by_api(client):
    c, _ = client
    body = {"name": "x", "trigger": {"type": "device", "device_id": "demo:a", "capability": "contact",
                                     "op": "becomes", "value": ""},
            "actions": [{"type": "notify", "message": "m"}]}
    assert c.post("/api/automations", json=body).status_code == 400


# ---------------- automations ----------------
def test_validate_rejects_blank_and_nonfinite():
    base = {"name": "x", "trigger": {"type": "interval", "minutes": 5}, "actions": [{"type": "notify", "message": "m"}]}
    with pytest.raises(ValueError):
        validate({**base, "actions": [{"type": "command", "device_id": "a", "capability": "light", "value": ""}]})
    with pytest.raises(ValueError):
        validate({**base, "conditions": [{"type": "device", "device_id": "a", "capability": "c", "op": "equals", "value": None}]})
    with pytest.raises(ValueError):
        validate({**base, "trigger": {"type": "device", "device_id": "a", "capability": "t", "op": "above", "value": "nan"}})
    assert validate({**base, "cooldown": "inf"})["cooldown"] == 0


def test_one_run_at_a_time_cancel_and_stats(tmp_path):
    async def go():
        h = hub_with(("t:l", "light"))
        e = AutomationEngine(h, Clock(datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
        a = e.upsert({"name": "slow", "trigger": {"type": "interval", "minutes": 5},
                      "actions": [{"type": "delay", "seconds": 30}, {"type": "notify", "message": "done"}]})
        e.start_run(a["id"], "t", force=True)
        e.start_run(a["id"], "t", force=True)
        await asyncio.sleep(0.02)
        assert e.runs[0].get("skipped") == "already running"
        task = e._running[a["id"]]
        e.upsert({**a, "enabled": False})            # disabling cancels the pending delay
        await asyncio.sleep(0.02)
        assert task.cancelled() or task.done()
        assert not e.notifications
        # stats survive an edit made while a run was in flight
        b = e.upsert({"name": "fast", "trigger": {"type": "interval", "minutes": 5},
                      "actions": [{"type": "notify", "message": "x"}]})
        await e.run(b["id"], "t", force=True)
        e.upsert({**e.items[b["id"]], "name": "renamed"})
        assert e.items[b["id"]]["run_count"] == 1
        await e.stop()
    asyncio.run(go())


def test_automation_loop_is_cut(tmp_path):
    async def go():
        h = hub_with(("t:a", "switch"), ("t:b", "switch"))

        async def command(did, capability, value):
            h.set_values(did, {capability: value})
        h.command = command
        e = AutomationEngine(h, Clock(datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
        h.listeners.append(e.on_change)
        h.set_values("t:a", {"switch": False}); h.set_values("t:b", {"switch": False})
        # a on → b on → a off → b off → a on → … (an endless ping-pong without the guard)
        for src, v, dst, nv in (("t:a", True, "t:b", True), ("t:b", True, "t:a", False),
                                ("t:a", False, "t:b", False), ("t:b", False, "t:a", True)):
            e.upsert({"name": f"{src}{v}", "trigger": {"type": "device", "device_id": src, "capability": "switch",
                                                        "op": "becomes", "value": v},
                      "actions": [{"type": "command", "device_id": dst, "capability": "switch", "value": nv}]})
        h.set_values("t:a", {"switch": True})
        await asyncio.sleep(0.3)
        total = sum(x.get("run_count", 0) for x in e.items.values())
        assert total <= 4 and any("loop" in (r.get("skipped") or "") for r in e.runs)
        await e.stop()
    asyncio.run(go())


def test_missed_minute_fires_once(tmp_path):
    async def go():
        h = hub_with(("t:l", "light"))
        e = AutomationEngine(h, Clock(datetime(2026, 11, 16, 6, 59, 50, tzinfo=PHX)), tmp_path)
        a = e.upsert({"name": "seven", "trigger": {"type": "time", "at": "07:00"},
                      "actions": [{"type": "notify", "message": "m"}]})
        assert e.tick(e.clock.now()) == []
        e.clock.t += 75                                  # the loop stalled across 07:00
        assert e.tick(e.clock.now()) == [a["id"]]
        e.clock.t += 5
        assert e.tick(e.clock.now()) == []
        await e.stop()
    asyncio.run(go())


def test_webhook_ssrf_block():
    async def go():
        for url in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8080/api/settings", "http://[::1]/"):
            with pytest.raises(ValueError):
                await check_webhook_url(url)
        await check_webhook_url("http://192.168.1.20/hook")      # LAN stays allowed
    asyncio.run(go())


def test_automations_file_is_private(tmp_path):
    e = AutomationEngine(Hub(), Clock(datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
    e.upsert({"name": "n", "trigger": {"type": "interval", "minutes": 5}, "actions": [{"type": "notify", "message": "m"}]})
    assert stat.S_IMODE(os.stat(tmp_path / "automations.json").st_mode) == 0o600


# ---------------- NUT server ----------------
def nut(server_cfg):
    from app.adapters.nut import NutAdapter
    a = NutAdapter(Hub(), {"server": server_cfg})
    a.relay["ups"] = {"device": "nut:x", "vars": {"ups.status": "OL"}, "ok": 1e18, "fsd": False}
    a._real_names.add("ups")
    a.clients["c"] = {}
    return a


def session(a, users, *cmds):
    s = {"user": None, "password": None, "login": None, "primary": False}
    return [a._reply(c.split(), s, users, "c") for c in cmds]


def test_nut_fsd_needs_opt_in_and_user():
    users = {"monuser": "secret"}
    a = nut({"user": "monuser", "password": "secret"})
    out = session(a, users, "USERNAME monuser", "PASSWORD secret", "LOGIN ups", "PRIMARY ups", "FSD ups")
    assert out[2] == "OK\n" and out[3].startswith("ERR ACCESS-DENIED") and out[4].startswith("ERR ACCESS-DENIED")
    assert not a.relay["ups"]["fsd"]
    a = nut({"allow_fsd": True})                                      # no user configured: never
    assert session(a, {}, "PRIMARY ups")[0].startswith("ERR ACCESS-DENIED")
    a = nut({"allow_fsd": True, "user": "monuser", "password": "secret"})          # default password: still no
    out = session(a, users, "USERNAME monuser", "PASSWORD secret", "PRIMARY ups", "FSD ups")
    assert out[3].startswith("ERR ACCESS-DENIED")
    strong = {"monuser": "correct-horse-battery"}
    a = nut({"allow_fsd": True, "user": "monuser", "password": "correct-horse-battery"})
    out = session(a, strong, "USERNAME monuser", "PASSWORD correct-horse-battery", "PRIMARY ups", "FSD ups")
    assert out[2].startswith("OK") and out[3] == "OK FSD-SET\n"


def test_nut_default_allow_is_private_only():
    a = nut({})
    assert a._allowed("192.168.1.5") and a._allowed("10.1.2.3") and a._allowed("::ffff:172.20.0.1")
    assert not a._allowed("8.8.8.8") and not a._allowed("2001:db8::1")
    assert nut({"allow": ["8.8.8.0/24"]})._allowed("8.8.8.8")


def test_nut_alias_pinned_to_first_server():
    from app.adapters.nut import NutAdapter
    a = NutAdapter(Hub(), {"servers": [{"host": "a"}, {"host": "b"}], "server": {}})
    a._register_relay("other", "nut:b_other", "b")                 # second server answered first
    assert a.relay["ups"]["device"] == "nut:b_other"
    a._register_relay("main", "nut:a_main", "a", primary=True)
    assert a.relay["ups"]["device"] == "nut:a_main"
    a._register_relay("other", "nut:b_other", "b")
    assert a.relay["ups"]["device"] == "nut:a_main"


def test_nut_bind_failure_keeps_polling():
    async def go():
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen()
        port = s.getsockname()[1]
        from app.adapters.nut import NutAdapter
        a = NutAdapter(Hub(), {"server": {"enabled": True, "bind": "127.0.0.1", "port": port}})
        a.status = "ok"
        await a._start_server()
        assert a.server is None and a.status.startswith("ok · NUT server failed")
        s.close()
    asyncio.run(go())


# ---------------- MQTT ----------------
def test_mqtt_discovery_delete_and_bad_topics():
    from app.adapters.mqtt import MqttAdapter
    h = Hub()
    a = MqttAdapter(h, {})
    topic = "homeassistant/binary_sensor/d1/contact/config"
    cfg = {"device_class": "door", "state_topic": "z2m/d1", "unique_id": "d1c",
           "device": {"identifiers": ["d1"], "name": "Door"}}
    a.handle_message(topic, json.dumps(cfg).encode())
    assert "mqtt:d1" in h.devices and "z2m/d1" in a.by_topic
    a.handle_message(topic, b"")
    assert "mqtt:d1" not in h.devices and "z2m/d1" not in a.by_topic
    a.handle_message("homeassistant/sensor/d2/t/config", json.dumps(
        {"device_class": "temperature", "state_topic": "#", "unique_id": "t", "device": {"identifiers": ["d2"]}}).encode())
    assert "#" not in a.by_topic and "#" not in a._pending_subs


# ---------------- adapters ----------------
def test_spawn_does_not_spin_on_normal_return():
    async def go():
        from app.adapters.base import Adapter
        a = Adapter(Hub(), {})
        calls = []

        async def quick():
            calls.append(1)
        a.spawn("q", quick)
        await asyncio.sleep(0.2)
        await a.stop()
        assert len(calls) == 1
    asyncio.run(go())


def test_poll_failures_mark_offline_and_status():
    from app.adapters.base import Adapter
    h = hub_with(("t:x", "temperature"))
    h.set_values("t:x", {"temperature": 20})
    a = Adapter(h, {}); a.status = "ok"
    for _ in range(3):
        a.poll_failed("t:x", RuntimeError("boom"), ["t:x"])
    assert h.devices["t:x"].online is False and a.status == "error: boom"
    a.poll_ok("t:x")
    assert a.status == "ok"


def test_ecowitt_channel_cap():
    from app.adapters.ecowitt import parse
    g = parse({"soilmoisture999999": "10", "soilmoisture3": "40", "temp1f": "nan"})
    assert "soil3" in g and "soil999999" not in g


# ---------------- settings ----------------
def test_settings_validation_and_secret_rebinding(tmp_path, monkeypatch):
    import app.settings as s
    monkeypatch.setattr(s, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(s, "load_config", lambda: {})
    s.save_section("mqtt", True, {"host": "broker.lan", "password": "pw"})
    assert stat.S_IMODE(os.stat(tmp_path / "settings.json").st_mode) == 0o600
    s.save_section("mqtt", True, {"host": "broker.lan", "password": s.MASK})
    assert s.load_saved()["mqtt"]["password"] == "pw"
    s.save_section("mqtt", True, {"host": "evil.example", "password": s.MASK})
    assert s.load_saved()["mqtt"]["password"] == ""
    with pytest.raises(ValueError):
        s.save_section("history", True, {"backend": "bogus"})
    s.save_section("nut", True, {"servers": ["junk", {"host": "10.0.0.2"}]})
    assert [x["host"] for x in s.load_saved()["nut"]["servers"]] == ["10.0.0.2"]
