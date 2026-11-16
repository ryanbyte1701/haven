import asyncio
import re
import time

import pytest

from app import history as H
from app.hub import Hub
from app.models import Device, cap


def run(coro):
    return asyncio.run(coro)


# ---------------- value mapping ----------------
def test_to_number_mapping():
    assert H.to_number("leak", True) == (1.0, True)
    assert H.to_number("contact", "open") == (1.0, True)
    assert H.to_number("contact", "closed") == (0.0, True)
    assert H.to_number("lock", "unlocked") == (1.0, True)
    assert H.to_number("lock", "locked") == (0.0, True)
    assert H.to_number("valve", "open") == (1.0, True)
    assert H.to_number("ups_status", "on_battery") == (1.0, True)
    assert H.to_number("ups_status", "low_battery") == (2.0, True)
    assert H.to_number("temperature", 21.5) == (21.5, False)
    assert H.to_number("temperature", float("nan")) is None
    assert H.to_number("model", "abc") is None


# ---------------- sqlite store ----------------
def test_sqlite_throttle_changes_downsample_retention(tmp_path):
    async def go():
        s = H.SqliteStore(tmp_path / "h.db", sample_seconds=60, retention_days=30)
        await s.start()
        t0 = time.time() - 3600
        # numeric: one sample per 60 s even if reported every 10 s
        for i in range(360):
            s.record("d1", "temperature", 20 + (i % 10), t0 + i * 10)
        # binary: every change is kept even inside the sample window
        s.record("d1", "contact", "closed", t0)
        s.record("d1", "contact", "open", t0 + 1)
        s.record("d1", "contact", "closed", t0 + 2)
        s.record("d1", "contact", "closed", t0 + 3)   # unchanged + not due: dropped
        s.flush()
        n_temp = s._db.execute("SELECT COUNT(*) FROM samples WHERE cap='temperature'").fetchone()[0]
        n_contact = s._db.execute("SELECT COUNT(*) FROM samples WHERE cap='contact'").fetchone()[0]
        assert n_temp == 60 and n_contact == 3
        full = await s.query("d1", "temperature", t0 - 1, t0 + 3600, 300)
        assert len(full) == 60
        few = await s.query("d1", "temperature", t0 - 1, t0 + 3600, 10)
        assert 9 <= len(few) <= 11
        assert all(20 <= p["v"] <= 29 for p in few)
        # states come back as every change (never averaged): a brief "open" is kept
        cont = await s.query("d1", "contact", t0 - 1, t0 + 3600, 1)
        assert [p["v"] for p in cont] == [0, 1, 0]
        later = await s.query("d1", "contact", t0 + 1.5, t0 + 3600, 1)     # state in force at the start comes first
        assert later[0] == {"t": round(t0 + 1.5, 3), "v": 1.0} and later[-1]["v"] == 0
        # retention: old rows pruned
        s._db.execute("INSERT INTO samples VALUES ('d1','temperature',?,1,0)", (time.time() - 40 * 86400,))
        s._db.commit()
        assert s.prune() == 1
        assert "1y" not in s.ranges() and "30d" in s.ranges()
        await s.stop()
        # persists across restarts
        s2 = H.SqliteStore(tmp_path / "h.db", 60, 30)
        await s2.start()
        assert len(await s2.query("d1", "temperature", t0 - 1, t0 + 3600)) == 60
        await s2.stop()
    run(go())


def test_memory_store_downsample():
    hub = Hub()
    now = time.time()
    from collections import deque
    hub.history[("d", "temperature")] = deque([(now - 1000 + i, float(i)) for i in range(1000)])
    m = H.MemoryStore(hub)
    pts = run(m.query("d", "temperature", now - 1000, now, 50))
    assert 45 <= len(pts) <= 51


def test_history_manager_reconfigure_and_fallback(tmp_path):
    async def go():
        hub = Hub()
        h = H.History(hub)
        await h.configure({"backend": "sqlite", "sample_seconds": 1}, data_dir=tmp_path)
        assert h.info()["backend"] == "sqlite" and h.info()["ok"]
        h.record("d", "temperature", 21.0, time.time())
        assert (await h.query("d", "temperature", "1h"))[0]["v"] == 21.0
        # unreachable Prometheus -> falls back to memory, reports not ok
        await h.configure({"backend": "prometheus", "prometheus_url": "http://127.0.0.1:9"}, data_dir=tmp_path)
        assert h.info()["backend"] == "prometheus" and not h.info()["ok"]
        from collections import deque
        hub.history[("d", "temperature")] = deque([(time.time() - 5, 22.0)])
        assert (await h.query("d", "temperature", "1h"))[0]["v"] == 22.0
        await h.configure({"enabled": False}, data_dir=tmp_path)
        assert h.info()["backend"] == "memory" and not h.metrics_enabled
        await h.stop()
    run(go())


# ---------------- exporter ----------------
LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})? (-?[0-9.eE+-]+|NaN)$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text):
    out = []
    for line in text.strip().splitlines():
        if line.startswith("#"):
            assert re.match(r"^# (HELP|TYPE) haven_\w+ ", line), line
            continue
        m = LINE.match(line)
        assert m, f"bad line: {line}"
        labels = {k: v.encode().decode("unicode_escape") for k, v in LABEL.findall(m.group(3) or "")}
        out.append((m.group(1), labels, float(m.group(4))))
    return out


def _hub_with_devices():
    hub = Hub()
    hub.upsert_device(Device(id="x:1", name='Kitchen "Sink"\\Leak', source="yolink", room="Kitchen",
                             capabilities={"leak": cap("leak"), "battery": cap("battery"), "temperature": cap("temperature")}))
    hub.set_values("x:1", {"leak": False, "battery": 80, "temperature": 21.5})
    hub.upsert_device(Device(id="nut:ups", name="Rack UPS", source="nut", room="Closet",
                             capabilities={c: cap(c) for c in ("ups_status", "battery", "runtime", "load", "input_voltage")}))
    hub.set_values("nut:ups", {"ups_status": "on_battery", "battery": 64, "runtime": 23.0, "load": 30, "input_voltage": 0.0})
    hub.upsert_device(Device(id="y:door", name="Front Door", source="aqara", capabilities={"contact": cap("contact")}))
    hub.set_values("y:door", {"contact": "open"}, online=False)
    return hub


def test_render_metrics_format():
    hub = _hub_with_devices()
    rows = parse_metrics(H.render_metrics(hub))
    get = lambda name, **lab: [v for n, l, v in rows if n == name and all(l.get(k) == x for k, x in lab.items())]  # noqa: E731
    assert get("haven_value", device_id="x:1", capability="temperature") == [21.5]
    assert get("haven_value", device_id="x:1", capability="leak") == [0.0]
    assert get("haven_value", device_id="y:door", capability="contact") == [1.0]
    assert get("haven_device_online", device_id="y:door") == [0.0]
    assert get("haven_battery_percent", device_id="x:1") == [80.0]
    assert get("haven_ups_status", device="Rack UPS") == [1.0]
    assert get("haven_ups_runtime_minutes") == [23.0]
    # tricky label value round-trips
    assert any(l.get("device") == 'Kitchen "Sink"\\Leak' for _, l, _ in rows)
    assert len(get("haven_last_update_timestamp_seconds")) == 3


# ---------------- HTTP API ----------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.config as config
    import app.history as hist_mod
    import app.main as main
    import app.settings as settings_mod
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text("history:\n  backend: sqlite\n  sample_seconds: 1\n  metrics_token: s3cret\n")
    monkeypatch.setattr(config, "CONFIG_PATH", cfgfile)
    monkeypatch.setattr(config, "EXAMPLE_PATH", tmp_path / "nope.yaml")
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hist_mod, "DATA_DIR", tmp_path)
    main.hub.devices.clear()
    with TestClient(main.app, headers={"authorization": f"Bearer {main.DASH_TOKEN}"}) as c:
        hub = main.hub
        hub.upsert_device(Device(id="t:1", name="Office", source="test", capabilities={"temperature": cap("temperature")}))
        now = time.time()
        for i, v in enumerate((20.0, 21.0, 22.0)):          # older samples, 10 min apart
            main.history_store.record("t:1", "temperature", v, now - 1800 + i * 600)
        hub.set_values("t:1", {"temperature": 19.0})          # live path through Hub -> recorder
        yield c, main
    main.hub.devices.clear()


def test_api_history_metrics_and_settings(client):
    c, main = client
    info = c.get("/api/history/info").json()
    assert info["backend"] == "sqlite" and info["ok"] and "24h" in info["ranges"] and info["metrics_auth"]
    pts = c.get("/api/devices/t:1/history/temperature?range=1h").json()
    assert [p["v"] for p in pts] == [20.0, 21.0, 22.0, 19.0] and all("t" in p for p in pts)
    assert c.get("/api/devices/t:1/history/temperature").status_code == 200        # default range
    # /metrics token
    assert c.get("/metrics", headers={"Authorization": ""}).status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert any(n == "haven_value" and l["device_id"] == "t:1" for n, l, _ in parse_metrics(r.text))
    # settings: history section shows up, saving it reconfigures live (to memory, metrics off)
    secs = {s["id"]: s for s in c.get("/api/settings").json()["sections"]}
    assert secs["history"]["values"]["metrics_token"] == "********"
    r = c.put("/api/settings/history", json={"enabled": True, "values": {
        "backend": "memory", "metrics_enabled": False, "metrics_token": "********"}})
    assert r.status_code == 200, r.text
    assert c.get("/api/history/info").json()["backend"] == "memory"
    assert c.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 404


def test_memory_state_changes():
    import asyncio
    from app.hub import Hub
    from app.history import MemoryStore
    from app.models import Device, cap
    hub = Hub()
    hub.upsert_device(Device(id="l:1", name="Lamp", source="t", capabilities={"light": cap("light")}))
    for v in (True, False, True, True, False):
        hub.set_values("l:1", {"light": v})
    pts = asyncio.run(MemoryStore(hub).query("l:1", "light", 0, 1e12))
    assert [p["v"] for p in pts] == [1.0, 0.0, 1.0, 0.0]


def test_device_history_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("HAVEN_DATA", str(tmp_path))
    from fastapi.testclient import TestClient
    import app.main as m
    from app.models import Device, cap
    import uuid
    did = f"h:{uuid.uuid4().hex[:8]}"          # the shared test history store may hold older runs
    with TestClient(m.app, headers={"authorization": f"Bearer {m.DASH_TOKEN}"}) as c:
        m.hub.upsert_device(Device(id=did, name="Door", source="t", capabilities={
            "contact": cap("contact"), "temperature": cap("temperature"), "name_text": cap("status_text")}))
        for v in ("closed", "open", "closed"):
            m.hub.set_values(did, {"contact": v, "temperature": 20.5})
        r = c.get(f"/api/devices/{did}/history?range=1h").json()
        assert set(r["series"]) == {"contact", "temperature"}
        assert [p["v"] for p in r["series"]["contact"]] == [0, 1, 0] and r["states"]["contact"]["open"] == 1
        assert c.get("/api/devices/nope/history").status_code == 404
        m.hub.devices.pop(did, None)
