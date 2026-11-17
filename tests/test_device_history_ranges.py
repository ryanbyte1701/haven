"""Device panel ranges: live "5m" (every reading from memory), "4h", and custom start/end windows."""
import asyncio
import time
import uuid
from collections import deque

import pytest

from conftest import ADMIN
from app.models import Device, cap


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.config as config
    import app.history as hist_mod
    import app.main as main
    import app.settings as settings_mod
    cfgfile = tmp_path / "config.yaml"
    # sqlite keeps 2 days and samples numbers at most once a minute (too sparse for a 5-minute chart)
    cfgfile.write_text("history:\n  backend: sqlite\n  sample_seconds: 60\n  retention_days: 2\n")
    monkeypatch.setattr(config, "CONFIG_PATH", cfgfile)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hist_mod, "DATA_DIR", tmp_path)
    main.hub.devices.clear()
    with TestClient(main.app, headers=ADMIN) as c:
        did = f"r:{uuid.uuid4().hex[:8]}"
        main.hub.upsert_device(Device(id=did, name="Hall", source="test", capabilities={
            "temperature": cap("temperature"), "contact": cap("contact")}))
        yield c, main, did
    main.hub.devices.clear()


def test_live_5m_reads_every_reading_from_memory(client):
    c, main, did = client
    hub = main.hub
    for v in (20.0, 20.5, 21.0, 21.5, 22.0):          # five readings within a second: all kept
        hub.set_values(did, {"temperature": v})
    for v in ("closed", "open", "closed"):
        hub.set_values(did, {"contact": v})
    r = c.get(f"/api/devices/{did}/history?range=5m")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["range"] == "5m" and j["bucket"] == 0 and abs(j["end"] - j["start"] - 300) < 1
    assert [p["v"] for p in j["series"]["temperature"]] == [20.0, 20.5, 21.0, 21.5, 22.0]   # raw, not bucketed
    assert j["kinds"]["temperature"] == "number" and j["kinds"]["contact"] == "state"
    assert [p["v"] for p in j["series"]["contact"]] == [0, 1, 0]
    assert j["last"]["temperature"]["v"] == 22.0 and j["last"]["contact"]["v"] == 0
    # the SQLite store (one sample a minute) would have far fewer points for the same window
    assert len(c.get(f"/api/devices/{did}/history/temperature?range=5m").json()) == 5


def test_live_5m_window_and_quiet_sensor(client):
    c, main, did = client
    hub, now = main.hub, time.time()
    hub.recent[(did, "temperature")] = deque([(now - 400, 18.0), (now - 200, 19.0), (now - 100, 20.0), (now - 5, 21.0)])
    hub.state_history[(did, "contact")] = deque([(now - 900, 1.0), (now - 600, 0.0)])     # closed 10 min ago
    j = c.get(f"/api/devices/{did}/history?range=5m").json()
    assert [p["v"] for p in j["series"]["temperature"]] == [19.0, 20.0, 21.0]          # older than 5 min dropped
    # the state in force when the window starts is reported at the window start
    assert [p["v"] for p in j["series"]["contact"]] == [0.0] and abs(j["series"]["contact"][0]["t"] - j["start"]) < 1
    # a sensor quiet for 12 minutes: nothing in the window, but its last reading is still given
    hub.recent[(did, "temperature")] = deque([(now - 720, 17.5)])
    j = c.get(f"/api/devices/{did}/history?range=5m").json()
    assert j["series"]["temperature"] == [] and j["last"]["temperature"] == {"t": round(now - 720, 3), "v": 17.5}


def test_hub_recent_buffer_is_trimmed():
    from app.hub import RECENT_SECONDS, Hub
    hub = Hub()
    hub.upsert_device(Device(id="x:1", name="T", source="t", capabilities={"temperature": cap("temperature")}))
    hub.recent[("x:1", "temperature")] = deque([(time.time() - RECENT_SECONDS - 50, 1.0)])
    hub.set_values("x:1", {"temperature": 2.0})
    assert [v for _, v in hub.recent[("x:1", "temperature")]] == [2.0]


def test_4h_range_and_info(client):
    c, main, did = client
    info = c.get("/api/history/info").json()
    assert info["ranges"] == ["1h", "4h", "24h"]                  # retention 2 days: no 7d/30d/1y
    assert info["max_span"] == 2 * 86400 and info["live_range"] == "5m"
    main.history_store.record(did, "temperature", 20.0, time.time() - 3 * 3600)
    main.history_store.record(did, "temperature", 21.0, time.time() - 600)
    j = c.get(f"/api/devices/{did}/history?range=4h").json()
    assert j["range"] == "4h" and abs(j["end"] - j["start"] - 4 * 3600) < 1 and j["bucket"] == 48.0
    assert [p["v"] for p in j["series"]["temperature"]] == [20.0, 21.0]
    # the memory backend serves 4h too
    from app.history import MemoryStore
    assert MemoryStore(main.hub).ranges() == ["1h", "4h", "24h"]


def test_custom_range_bucket_and_clamping(client):
    c, main, did = client
    now = time.time()
    for i in range(10):
        main.history_store.record(did, "temperature", 20.0 + i, now - 30 * 3600 + i * 3600)
    start, end = now - 30 * 3600 - 60, now - 20 * 3600
    j = c.get(f"/api/devices/{did}/history?start={start}&end={end}").json()
    assert j["range"] == "custom" and j["start"] == round(start, 3) and j["end"] == round(end, 3)
    assert j["bucket"] == round((end - start) / 300, 3) and not j["clamped"]           # ~300 points over the span
    assert [p["v"] for p in j["series"]["temperature"]] == [20.0 + i for i in range(10)]
    # end in the future -> now; start older than the 2-day retention -> the oldest kept reading
    j = c.get(f"/api/devices/{did}/history?start={now - 5 * 86400}&end={now + 3600}").json()
    assert j["clamped"] and j["end"] <= time.time() + 1 and abs(j["start"] - (now - 2 * 86400)) < 5
    assert abs(j["bucket"] - 2 * 86400 / 300) < 1
    # per-capability endpoint too
    pts = c.get(f"/api/devices/{did}/history/temperature?start={start}&end={end}").json()
    assert [p["v"] for p in pts] == [20.0 + i for i in range(10)]


@pytest.mark.parametrize("query, msg", [
    ("start=100", "both a start and an end"),
    ("end=100", "both a start and an end"),
    ("start=abc&end=def", "seconds since 1970"),
    ("start=nan&end=1", "seconds since 1970"),
    ("start=1731600000000&end=1731700000000", "not milliseconds"),
    ("start={b}&end={a}", "start must be before the end"),
    ("start={a}&end={a}", "start must be before the end"),
    ("start={a}&end={a30}", "at least 1 minute"),
    ("start={future}&end={future2}", "start before now"),
    ("start={old}&end={old2}", "only goes back 2 days"),
])
def test_custom_range_validation(client, query, msg):
    c, main, did = client
    now = time.time()
    q = query.format(a=now - 7200, a30=now - 7170, b=now - 3600, future=now + 600, future2=now + 1200,
                     old=now - 10 * 86400, old2=now - 9 * 86400)
    for url in (f"/api/devices/{did}/history?{q}", f"/api/devices/{did}/history/temperature?{q}"):
        r = c.get(url)
        assert r.status_code == 400, (url, r.text)
        assert msg in r.json()["detail"]


def test_window_memory_backend_retention():
    from app.hub import Hub
    from app.history import History
    h = History(Hub())
    now = 1_800_000_000.0
    name, a, b, clamped = h.window(None, now - 3 * 86400, now - 3600, now)
    assert name == "custom" and clamped and a == now - 86400 and b == now - 3600
    assert h.window("4h", now=now)[1:3] == (now - 4 * 3600, now)
    assert h.window("bogus", now=now)[0] == "24h"
    assert h.window("5m", now=now)[1] == now - 300
    assert asyncio.run(h.query_many("none", ["temperature"], "5m"))["series"] == {"temperature": []}
