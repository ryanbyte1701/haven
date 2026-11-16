"""Review round 3: Ecowitt trust (M1, L13), NUT servers (M2, L9), Tapo discovery (M3)."""
import asyncio
import logging
from types import SimpleNamespace as NS

import pytest

from app import settings as S
from app.adapters import ecowitt, nut, tapo
from app.hub import Hub
from app.security import RedactAccessLog


# ---------------- Ecowitt ----------------
@pytest.fixture
def eco(tmp_path, monkeypatch):
    monkeypatch.setattr(ecowitt.EcowittAdapter, "_state_path", lambda self: tmp_path / "gw.json")
    return ecowitt.EcowittAdapter(Hub(), {})


def test_secret_path_gateway_stays_on_the_secret_path(eco):
    form = {"PASSKEY": "AAAABBBBCCCCDDDD", "tempf": "70"}
    assert eco.ingest(form, via_secret=True, ip="10.0.0.9") == "ok"
    gid = ecowitt.gw_id(form["PASSKEY"])
    assert eco.approved[gid]["via_secret"] is True
    n = len(eco.hub.devices)
    assert eco.ingest({**form, "tempf": "-40"}, via_secret=False, ip="10.0.0.66") == "pending"
    assert len(eco.hub.devices) == n and not eco.pending
    dev = next(iter(eco.hub.devices.values()))
    assert dev.capabilities["temperature"].value > 20                  # the plain-path push changed nothing
    assert eco.gateways()["approved"][0]["via_secret"] is True
    # a gateway approved by hand keeps working on the plain path
    other = {"PASSKEY": "EEEEFFFFGGGGHHHH", "tempf": "60"}
    assert eco.ingest(other, ip="10.0.0.5") == "pending"
    assert eco.approve(ecowitt.gw_id(other["PASSKEY"]))
    assert eco.ingest(other, ip="10.0.0.5") == "ok"
    # PASSKEYs from the (masked) settings field: comma-separated string
    a2 = ecowitt.EcowittAdapter(Hub(), {"passkeys": "K1, K2"})
    assert a2.passkeys == ["K1", "K2"] and a2.trusted("K2")


def test_pending_expire_and_evict_oldest_preferring_the_same_sender(eco, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(ecowitt.time, "time", lambda: now[0])
    eco.ingest({"PASSKEY": "legit-1"}, ip="10.0.0.2")
    for i in range(10):                         # one address sprays PASSKEYs
        now[0] += 1
        eco.ingest({"PASSKEY": f"spray{i}"}, ip="10.0.0.66")
    keys = {v["passkey"] for v in eco.pending.values()}
    assert len(eco.pending) == ecowitt.MAX_PENDING and "legit-1" in keys
    now[0] += ecowitt.PENDING_TTL + 1
    assert eco.gateways()["pending"] == []      # expired after 24 h


def test_settings_passkeys_masked():
    f = next(f for f in S.SCHEMA_BY_ID["ecowitt"]["fields"] if f["key"] == "passkeys")
    assert f["type"] == "password"
    view = {s["id"]: s for s in S.public_view({"ecowitt": {"passkeys": ["SECRETKEY"]}}, {})}
    assert view["ecowitt"]["values"]["passkeys"] == S.MASK and "push_path" not in str(view["ecowitt"])


def test_access_log_hides_push_secret():
    r = logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/%s" %d',
                          ("10.0.0.9:5000", "POST", "/data/report/s3cr3t-Path_value", "1.1", 200), None)
    RedactAccessLog().filter(r)
    assert "s3cr3t" not in r.getMessage() and "/data/report/…" in r.getMessage()


# ---------------- NUT ----------------
def save_nut(monkeypatch, tmp_path, servers, existing=None):
    monkeypatch.setattr(S, "SETTINGS_FILE", tmp_path / "settings.json")
    if existing is not None:
        (tmp_path / "settings.json").write_text(__import__("json").dumps({"nut": {"servers": existing}}))
    monkeypatch.setattr(S, "load_config", lambda: {})
    return S.save_section("nut", True, {"servers": servers})["nut"]["servers"]


def test_nut_settings_validation(monkeypatch, tmp_path):
    for bad in ({"host": "10.0.0.1\r\nLOGOUT"}, {"host": "http://x/"}, {"host": "10.0.0.1", "port": 0},
                {"host": "10.0.0.1", "port": 70000}, {"host": "10.0.0.1", "port": "abc"},
                {"host": "10.0.0.1", "username": "u\nLIST"}, {"host": "10.0.0.1", "password": "p\x00"},
                {"host": "10.0.0.1", "ups": "a b"}, {"host": "10.0.0.1", "name": "x\ny"}):
        with pytest.raises(ValueError):
            save_nut(monkeypatch, tmp_path, [bad])
    old = [{"host": "10.0.0.1", "port": 3493, "ups": "ups", "username": "mon", "password": "pw1"}]
    keep = save_nut(monkeypatch, tmp_path, [{"host": "10.0.0.1", "ups": "ups", "username": "mon", "password": S.MASK}], old)
    assert keep[0]["password"] == "pw1"
    for moved in ({"host": "10.0.0.2", "ups": "ups"}, {"host": "10.0.0.1", "port": 3494, "ups": "ups"},
                  {"host": "10.0.0.1", "ups": "other"}):
        got = save_nut(monkeypatch, tmp_path, [{**moved, "username": "mon", "password": S.MASK}], old)
        assert "password" not in got[0]


def test_nut_client_argument_quoting():
    assert nut.nut_arg("simple", "password") == "simple"
    assert nut.nut_arg('a b"c\\d', "password") == '"a b\\"c\\\\d"'
    for bad in ("x\nLOGOUT", "x\r", "\x7f"):
        with pytest.raises(ValueError):
            nut.nut_arg(bad, "password")
    with pytest.raises(ValueError):
        nut.nut_arg("ups name", "UPS name", spaces=False)


def test_nut_list_is_capped_and_ids_do_not_collide():
    async def endless(reader, writer):
        await reader.readline()
        writer.write(b"BEGIN LIST UPS\n" + b'UPS u "x"\n' * (nut.MAX_LIST_ROWS + 5) + b"END LIST UPS\n")
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()

    async def go():
        srv = await asyncio.start_server(endless, "127.0.0.1", 0)
        c = nut.NutClient("127.0.0.1", srv.sockets[0].getsockname()[1])
        await c.connect()
        with pytest.raises(RuntimeError, match="more than"):
            await c.list_ups()
        await c.close()
        srv.close()
    asyncio.run(go())
    a = nut.NutAdapter(Hub(), {})
    first = a.ups_id("10.0.0.1", 3493, "3494_x")             # legacy id "10.0.0.1_3494_x"
    second = a.ups_id("10.0.0.1", 3494, "x")                  # would have been the same id
    assert first == "nut:10.0.0.1_3494_x" and second != first
    assert a.ups_id("10.0.0.1", 3494, "x") == second          # stable


def test_nut_per_server_status_and_quiet_unchanged_polls():
    VARS = {"ups.status": "OL", "battery.charge": "100"}

    async def upsd(reader, writer):
        while line := await reader.readline():
            cmd = line.decode().strip()
            if cmd == "LIST UPS":
                writer.write(b'BEGIN LIST UPS\nUPS ups "UPS"\nEND LIST UPS\n')
            elif cmd == "LIST VAR ups":
                writer.write(b"BEGIN LIST VAR ups\n" + "".join(f'VAR ups {k} "{v}"\n' for k, v in VARS.items()).encode()
                             + b"END LIST VAR ups\n")
            else:
                writer.write(b"OK\n")
            await writer.drain()
        writer.close()

    async def go():
        srv = await asyncio.start_server(upsd, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        hub = Hub()
        q = hub.subscribe()
        a = nut.NutAdapter(hub, {"poll_seconds": 2, "servers": [{"host": "127.0.0.1", "port": port},
                                                                 {"host": "127.0.0.1", "port": 1}]})   # nothing on port 1
        a.poll_seconds = 0.05
        await a.start()
        await asyncio.sleep(0.6)
        await a.stop()
        srv.close()
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        return a, msgs
    a, msgs = asyncio.run(go())
    assert a.server_status[f"127.0.0.1:{a.servers[0]['port']}"] == "ok"
    assert a.server_status["127.0.0.1:1"].startswith("error")
    assert a.status.startswith("error: 1/2 UPS servers ok")
    updates = [m for m in msgs if m.get("type") == "device" and m["device"]["source"] == "nut"]
    assert 1 <= len(updates) <= 3                 # ~10 polls, but unchanged readings aren't re-broadcast


# ---------------- Tapo ----------------
def kdev(family, enc, host="10.0.0.50", dtype="Plug"):
    calls = []

    async def update():
        calls.append("update")

    async def disconnect():
        pass
    d = NS(config=NS(connection_type=NS(device_family=NS(value=family), encryption_type=NS(value=enc))),
           device_type=NS(name=dtype), children=[], modules={}, host=host, alias="x", model="m", device_id="id1",
           is_on=True, update=update, disconnect=disconnect)
    d.calls = calls
    return d


def test_tapo_discovery_off_by_default_and_listed():
    assert tapo.TapoAdapter(Hub(), {}).discovery is False
    f = next(f for f in tapo.SETTINGS["fields"] if f["key"] == "discovery")
    assert f["default"] is False
    assert tapo.may_log_in(kdev("SMART.TAPOPLUG", "KLAP"), listed=False)
    assert not tapo.may_log_in(kdev("SMART.TAPOPLUG", "AES"), listed=False)
    assert tapo.may_log_in(kdev("SMART.TAPOPLUG", "AES"), listed=True)
    assert not tapo.may_log_in(kdev("SMART.IPCAMERA", "KLAP"), listed=True)


def test_tapo_find_never_logs_in_to_strangers_and_remembers_skips(monkeypatch):
    import kasa
    camera = kdev("SMART.IPCAMERA", "AES", host="10.0.0.60")
    bulb = kdev("SMART.TAPOPLUG", "KLAP", host="10.0.0.61", dtype="Bulb")
    plug = kdev("SMART.TAPOPLUG", "KLAP", host="10.0.0.62")
    discovered = {"10.0.0.60": camera, "10.0.0.61": bulb, "10.0.0.62": plug}
    singles = []

    async def discover(**kw):
        return dict(discovered)

    async def discover_single(host, **kw):
        singles.append(host)
        return discovered.get(host)
    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(discover))
    monkeypatch.setattr(kasa.Discover, "discover_single", staticmethod(discover_single))

    async def go():
        a = tapo.TapoAdapter(Hub(), {"username": "u", "password": "p", "discovery": True, "hosts": "plug.lan"})

        async def resolve(h):
            return "10.0.0.62" if h == "plug.lan" else h
        a._resolve = resolve
        await a._find()
        await a._find()                     # rescan
        return a
    a = asyncio.run(go())
    assert camera.calls == []                               # never sent the Tapo login
    assert bulb.calls == ["update"]                         # logged in once, then remembered as skipped
    assert set(a.skipped) == {"10.0.0.60", "10.0.0.61"}
    assert list(a.parents) == ["10.0.0.62"] and plug.calls == ["update"]
    assert singles == []                                    # listed host resolved to the discovered IP: no 2nd connect
