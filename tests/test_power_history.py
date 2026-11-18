"""Power page trend: per-device and total watts on one time axis, and energy (kWh) over the range."""
import asyncio
import time
from types import SimpleNamespace

from app.history import History
from app.models import Device, cap


def _hub(readings):
    devs = {}
    hist = {}
    for did, (now_v, pts) in readings.items():
        devs[did] = Device(id=did, name=did.title(), source="test", capabilities={"power": cap("power", value=now_v)})
        if pts:
            hist[(did, "power")] = pts
    return SimpleNamespace(devices=devs, history=hist, state_history={}, recent={})


def test_total_is_sum_and_energy_is_area():
    now = time.time()
    # a 1000 W heater for the whole hour, a 100 W lamp for the last half
    heater = [(now - 3600 + i * 60, 1000.0) for i in range(60)]
    lamp = [(now - 1800 + i * 60, 100.0) for i in range(30)]
    hub = _hub({"heater": (1000.0, heater), "lamp": (100.0, lamp)})
    h = History(hub)
    res = asyncio.run(h.power_history(list(hub.devices.values()), "1h", buckets=60))
    assert res["range"] == "1h" and len(res["t"]) == 60 == len(res["total"])
    by = {d["id"]: d for d in res["devices"]}
    assert res["devices"][0]["id"] == "heater"                 # most energy first
    assert abs(by["heater"]["kwh"] - 1.0) < 0.05
    assert abs(by["lamp"]["kwh"] - 0.05) < 0.01
    assert by["lamp"]["points"][0] is None                    # no reading yet: not invented
    assert res["total"][-1] == 1100.0 and res["peak"] == 1100.0
    assert abs(res["total_kwh"] - 1.05) < 0.06


def test_quiet_device_holds_its_current_value():
    hub = _hub({"fridge": (60.0, None)})
    h = History(hub)
    res = asyncio.run(h.power_history(list(hub.devices.values()), "4h", buckets=24))
    d = res["devices"][0]
    assert d["points"] == [60.0] * 24 and abs(d["kwh"] - 0.24) < 0.001


def test_endpoint_lists_power_devices_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from conftest import ADMIN
    import app.config as config
    import app.history as hist_mod
    import app.main as main
    import app.settings as settings_mod
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text("history:\n  backend: memory\n")
    monkeypatch.setattr(config, "CONFIG_PATH", cfgfile)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hist_mod, "DATA_DIR", tmp_path)
    main.hub.devices.clear()
    with TestClient(main.app, headers=ADMIN) as c:
        main.hub.upsert_device(Device(id="p:1", name="Desk plug", source="test",
                                      capabilities={"power": cap("power", value=42.0), "switch": cap("switch", value=True)}))
        main.hub.upsert_device(Device(id="t:1", name="Hall", source="test",
                                      capabilities={"temperature": cap("temperature", value=21.0)}))
        r = c.get("/api/power/history?range=4h")
        assert r.status_code == 200
        body = r.json()
        assert [d["id"] for d in body["devices"]] == ["p:1"]
        assert body["total"][-1] == 42.0
        assert c.get("/api/power/history?start=5&end=1").status_code == 400
    main.hub.devices.clear()


def test_devices_can_be_moved_to_the_newer_pages(tmp_path, monkeypatch):
    """Edit → pages: Windows, Switches and Appliances are accepted (were rejected as unknown tabs)."""
    from fastapi.testclient import TestClient

    from conftest import ADMIN
    import app.config as config
    import app.main as main
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(main.hub, "_overrides_path", tmp_path / "overrides.json")
    main.hub.devices.clear()
    with TestClient(main.app, headers=ADMIN) as c:
        main.hub.upsert_device(Device(id="b:1", name="Blinds", source="test", capabilities={"contact": cap("contact", value="open")}))
        r = c.patch("/api/devices/b:1", json={"categories": ["windows", "switches", "appliances"]})
        assert r.status_code == 200 and r.json()["categories"] == ["windows", "switches", "appliances"]
        assert c.patch("/api/devices/b:1", json={"categories": ["nope"]}).status_code == 400
    main.hub.devices.clear()


def test_custom_view_keeps_name_keywords():
    from app.hub import _clean_view
    v = _clean_view({"id": "v1", "name": "Bedtime", "keywords": [" bedroom ", "", "lamp", 5, True], "devices": ["a"]})
    assert v["keywords"] == ["bedroom", "lamp", "5"] and v["devices"] == ["a"]


def test_websocket_burst_keeps_the_newest_state_of_each_device():
    from app.main import coalesce
    dev = lambda i, v: {"type": "device", "device": {"id": i, "v": v}}  # noqa: E731
    out = coalesce([dev("a", 1), dev("b", 1), {"type": "layout"}, dev("a", 2), dev("a", 3)])
    assert out == [dev("b", 1), {"type": "layout"}, dev("a", 3)]


def test_sqlite_store_reads_every_device_in_one_query(tmp_path):
    """With SQLite the Power page reads all devices' slices in one query; the result matches the per-device math."""
    now = time.time()
    hub = _hub({"heater": (1000.0, None), "lamp": (100.0, None), "idle": (5.0, None)})
    h = History(hub)

    async def run():
        await h.configure({"enabled": True, "backend": "sqlite", "sample_seconds": 1}, data_dir=tmp_path)
        st = h.store
        assert hasattr(st, "bucketed")
        for i in range(60):
            st.record("heater", "power", 1000.0, now - 3600 + i * 60)
            if i >= 30:
                st.record("lamp", "power", 100.0, now - 3600 + i * 60)
        st.record("idle", "power", 5.0, now - 7200)                 # before the window only: held through it
        calls = []
        orig = st._bucketed
        st._bucketed = lambda *a: calls.append(a) or orig(*a)
        res = await h.power_history(list(hub.devices.values()), "1h", buckets=60)
        again = await h.power_history(list(hub.devices.values()), "1h", buckets=60)   # cached
        await h.stop()
        return res, again, calls
    res, again, calls = asyncio.run(run())
    assert len(calls) == 1 and again is res
    by = {d["id"]: d for d in res["devices"]}
    assert abs(by["heater"]["kwh"] - 1.0) < 0.05 and abs(by["lamp"]["kwh"] - 0.05) < 0.01
    assert by["lamp"]["points"][0] is None and by["idle"]["points"][0] == 5.0


def test_last_reading_before_the_window_is_found_with_an_index_seek():
    import inspect

    from app.history import SqliteStore
    src = inspect.getsource(SqliteStore._bucketed)
    assert "SELECT MAX(ts)" not in src                     # a correlated sub-query read every row of every device
    assert "ORDER BY ts DESC LIMIT 1" in src
