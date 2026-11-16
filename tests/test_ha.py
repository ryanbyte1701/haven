"""High availability (app/ha.py): election, signing, config sync, VIP commands, promotion/demotion."""
import asyncio
import hmac
import json
import os
import stat
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as config_mod
from app import ha as H

SECRET = "correct-horse-battery-staple-42"
A, S, F, ST = H.ACTIVE, H.STANDBY, H.FAULT, H.STARTING


def me(state, prio=200, nid="aaaa", yielding=False):
    return {"state": state, "priority": prio, "node_id": nid, "yielding": yielding}


# ------------------------------------------------------------------ election
def test_peer_missing_takes_over_unless_isolated():
    assert H.decide(me(S), None, preempt=True)[0] == A
    assert H.decide(me(S), None, preempt=True, gateway_ok=None)[0] == A       # no gateway known: go ahead
    assert H.decide(me(S), None, preempt=True, gateway_ok=False)[0] == F      # isolated: don't grab the VIP
    assert H.decide(me(A), None, preempt=True, gateway_ok=False)[0] == F      # active + isolated: give it up


def test_priority_and_tie_break():
    hi, lo = me(ST, 200, "aaaa"), me(ST, 100, "zzzz")
    assert H.decide(hi, lo, preempt=True)[0] == A
    assert H.decide(lo, hi, preempt=True)[0] == S
    # equal priority: stable tie-break on node_id, exactly one winner
    x, y = me(S, 150, "1111"), me(S, 150, "2222")
    assert {H.decide(x, y, preempt=False)[0], H.decide(y, x, preempt=False)[0]} == {A, S}
    assert H.decide(y, x, preempt=False)[0] == A


def test_preempt_on_and_off():
    primary_back, standby_active = me(ST, 200, "p"), me(A, 100, "s")
    assert H.decide(primary_back, standby_active, preempt=True)[0] == A        # takes the VIP back
    assert H.decide(primary_back, standby_active, preempt=False)[0] == S       # waits until the other fails
    # the active (lower) node stays active while the higher one is only on standby
    assert H.decide(me(A, 100, "s"), me(S, 200, "p"), preempt=False)[0] == A
    assert H.decide(me(A, 100, "s"), me(S, 200, "p"), preempt=True)[0] == A


def test_split_brain_lower_priority_demotes():
    hi, lo = me(A, 200, "a"), me(A, 100, "b")
    assert H.decide(lo, hi, preempt=False)[0] == S
    assert H.decide(hi, lo, preempt=False)[0] == A
    assert H.decide(lo, hi, preempt=True)[0] == S


def test_peer_fault_or_stopping_means_take_over():
    assert H.decide(me(S, 100), me(F, 200), preempt=True)[0] == A
    assert H.decide(me(S, 100), me("stopping", 200), preempt=True)[0] == A


def test_yielding_hands_over():
    assert H.decide(me(A, 200, yielding=True), me(S, 100), preempt=True)[0] == S
    # the one that handed over ranks lowest, so it doesn't preempt straight back
    assert H.decide(me(S, 200, yielding=True), me(A, 100), preempt=True)[0] == S
    assert H.decide(me(S, 100), me(S, 200, yielding=True), preempt=True)[0] == A


# ------------------------------------------------------------------ signing
def test_signed_requests_verify_and_reject():
    auth, _ = H.derive_keys(SECRET)
    bad, _ = H.derive_keys(SECRET + "x")
    body = b'{"state":"active"}'
    hdr, _n = H.sign_request(auth, "POST", "/api/ha/peer/heartbeat", body)
    assert H.verify_request(auth, "POST", "/api/ha/peer/heartbeat", body, hdr, H.ReplayCache())[0]
    for args in ((bad, "POST", "/api/ha/peer/heartbeat", body, hdr),          # wrong secret
                 (auth, "POST", "/api/ha/peer/heartbeat", body + b" ", hdr),   # tampered body
                 (auth, "POST", "/api/ha/peer/yield", body, hdr),              # other endpoint
                 (auth, "POST", "/api/ha/peer/heartbeat", body, None),
                 (auth, "POST", "/api/ha/peer/heartbeat", body, "v1:1:xx:yy"),
                 (auth, "POST", "/api/ha/peer/heartbeat", body, SECRET)):      # the raw secret is not a signature
        ok, reason, _ = H.verify_request(*args, H.ReplayCache())
        assert not ok and reason == "signature"
    cache = H.ReplayCache()
    assert H.verify_request(auth, "POST", "/p", b"", H.sign_request(auth, "POST", "/p", b"", nonce="ab" * 8)[0], cache)[0]
    assert H.verify_request(auth, "POST", "/p", b"", H.sign_request(auth, "POST", "/p", b"", nonce="ab" * 8)[0],
                            cache)[1] == "replay"
    old = H.sign_request(auth, "POST", "/p", b"", ts=1_000_000)[0]
    assert H.verify_request(auth, "POST", "/p", b"", old, H.ReplayCache())[1] == "clock"


def test_signature_compare_is_constant_time(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(H.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    auth, _ = H.derive_keys(SECRET)
    H.verify_request(auth, "POST", "/p", b"", "v1:123:" + "a" * 32 + ":" + "0" * 64, H.ReplayCache())
    H.verify_request(auth, "POST", "/p", b"", "garbage", H.ReplayCache())
    assert len(calls) == 2


def test_encryption_round_trip_bound_to_request():
    _, key = H.derive_keys(SECRET)
    box = H.encrypt(key, {"files": {"a": 1}}, "nonce1")
    assert H.decrypt(key, box, "nonce1") == {"files": {"a": 1}}
    with pytest.raises(Exception):
        H.decrypt(key, box, "nonce2")                       # can't be replayed as the answer to another request
    with pytest.raises(Exception):
        H.decrypt(H.derive_keys("another-secret-1234567")[1], box, "nonce1")
    assert SECRET not in json.dumps(box)


# ------------------------------------------------------------------ settings validation
def test_settings_validation(tmp_path, monkeypatch):
    import app.settings as s
    monkeypatch.setattr(s, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(s, "load_config", lambda: {})
    good = {"role": "primary", "peer": "192.168.1.21", "secret": SECRET, "vip": "192.168.1.50"}
    for bad, msg in (({"secret": "short"}, "16"), ({"vip": "nope"}, "IPv4"), ({"vip": "192.168.1.255"}, "broadcast"),
                     ({"vip": "127.0.0.5"}, "unused"), ({"peer": "http://x/y?z"}, "address"),
                     ({"priority": 300}, "1 to 254"), ({"heartbeat_seconds": 0}, "Heartbeat"),
                     ({"peer": ""}, "address")):
        with pytest.raises(ValueError, match=msg):
            s.save_section("ha", True, {**good, **bad})
    s.save_section("ha", False, {})                          # off: nothing required
    s.save_section("ha", True, good)
    cfg = H.load_config(s.effective_config()["ha"])
    assert (cfg.peer_url, cfg.priority, cfg.prefix, cfg.dead_after, cfg.preempt, cfg.sync, cfg.verify_tls) == \
        ("http://192.168.1.21:1701", 200, 24, 10, True, True, False)
    assert H.parse_peer("https://nas2") == "https://nas2:1702"
    assert H.parse_peer("nas2:1702") == "https://nas2:1702"
    assert H.load_config({**good, "enabled": True, "role": "standby"}).priority == 100


# ------------------------------------------------------------------ config sync
def test_config_sync_allow_list_and_local_ha(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir(), dst.mkdir()
    (src / "settings.json").write_text(json.dumps({"mqtt": {"host": "10.0.0.2"}, "ha": {"role": "primary", "secret": "A" * 20}}))
    (src / "layout.json").write_text(json.dumps({"rooms": ["Kitchen"]}))
    (src / "automations.json").write_text(json.dumps({"automations": []}))
    (src / "tls").mkdir()
    (src / "tls" / "key.pem").write_text("PRIVATE")
    (src / "ha_node.json").write_text('{"node_id": "abcdef0123"}')
    (dst / "settings.json").write_text(json.dumps({"ha": {"role": "standby", "peer": "x"}, "mqtt": {"host": "old"}}))
    files = H.export_config(src)
    assert set(files) == {"settings.json", "layout.json", "automations.json"}
    assert "ha" not in files["settings.json"]                         # never sends its own HA section
    assert H.config_version(src) == H.config_version(src)

    writes = []
    real = H.write_private
    monkeypatch.setattr(H, "write_private", lambda p, d, binary=False: writes.append(p.name) or real(p, d, binary))
    evil = {**files, "../evil.json": {"x": 1}, "tls/key.pem": {"x": 1}, "ha_node.json": {"node_id": "0" * 12}}
    changed = H.apply_config(dst, evil)
    assert sorted(changed) == ["automations.json", "layout.json", "settings.json"] == sorted(writes)
    got = json.loads((dst / "settings.json").read_text())
    assert got == {"mqtt": {"host": "10.0.0.2"}, "ha": {"role": "standby", "peer": "x"}}   # kept our own HA section
    assert not (tmp_path / "evil.json").exists() and not (dst / "tls").exists() and not (dst / "ha_node.json").exists()
    assert stat.S_IMODE(os.stat(dst / "settings.json").st_mode) == 0o600
    assert not [p for p in os.listdir(dst) if p.endswith(".tmp")]           # atomic: no temp files left
    assert H.config_version(dst) == H.config_version(src)                   # converged: no more syncing
    assert H.apply_config(dst, files) == []                                 # nothing changed: nothing written
    # one bad file: nothing is written at all
    before = (dst / "layout.json").read_text()
    with pytest.raises(ValueError):
        H.apply_config(dst, {"layout.json": {"rooms": ["X"]}, "overrides.json": "not json object"})
    assert (dst / "layout.json").read_text() == before


# ------------------------------------------------------------------ network layer
ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
ovs_eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0
ovs_eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
"""


def test_routes_interface_and_gateway():
    routes = H.read_routes(ROUTES)
    assert H.route_iface("192.168.1.50", routes) == "ovs_eth0"
    assert H.route_iface("172.17.0.9", routes) == "docker0"
    assert H.route_iface("10.9.9.9", routes) == "ovs_eth0"          # only the default route matches
    assert H.default_gateway(routes) == "192.168.1.1"


def test_garp_frame():
    mac = bytes.fromhex("001132aabbcc")
    f = H.garp_frame(mac, "192.168.1.50")
    assert len(f) == 42 and f[:6] == b"\xff" * 6 and f[6:12] == mac and f[12:14] == b"\x08\x06"
    assert f[20:22] == b"\x00\x01" and f[28:32] == f[38:42] == bytes([192, 168, 1, 50])
    assert H.garp_frame(mac, "192.168.1.50", reply=True)[20:22] == b"\x00\x02"


def test_vip_commands(monkeypatch):
    monkeypatch.setattr(H.LinuxNet, "ip_bin", staticmethod(lambda: "/usr/sbin/ip"))
    calls, answers = [], []

    async def run(argv):
        calls.append(argv)
        return answers.pop(0) if answers else (0, "", "")
    net = H.LinuxNet(run=run)

    async def go():
        answers.append((0, "", ""))
        assert await net.add_vip("192.168.1.50", 24, "eth0") == (True, "")
        answers.append((2, "", "RTNETLINK answers: File exists"))
        assert (await net.add_vip("192.168.1.50", 24, "eth0"))[0]           # already there: fine
        answers.append((2, "", "RTNETLINK answers: Operation not permitted"))
        ok, msg = await net.add_vip("192.168.1.50", 24, "eth0")
        assert not ok and msg == H.NEEDS_CAPS
        answers.append((1, "", 'Cannot find device "eth9"'))
        ok, msg = await net.add_vip("192.168.1.50", 24, "eth9")
        assert not ok and "eth9" in msg and "SYNOLOGY.md" in msg
        answers.append((2, "", "Error: ipv4: Address already assigned."))   # newer iproute2 wording
        assert (await net.add_vip("192.168.1.50", 24, "eth0"))[0]
        answers.append((2, "", "RTNETLINK answers: Cannot assign requested address"))
        assert (await net.del_vip("192.168.1.50", 24, "eth0"))[0]           # already gone: fine
        answers.append((2, "", "Error: ipv4: Address not found."))
        assert (await net.del_vip("192.168.1.50", 24, "eth0"))[0]
        answers.append((0, "2: eth0    inet 192.168.1.20/24 brd ...\n2: eth0    inet 192.168.1.50/24 scope global secondary eth0:haven", ""))
        assert await net.has_vip("192.168.1.50", "eth0") is True
        answers.append((0, "2: eth0    inet 192.168.1.150/24", ""))
        assert await net.has_vip("192.168.1.50", "eth0") is False
    asyncio.run(go())
    assert calls[0] == ["/usr/sbin/ip", "addr", "add", "192.168.1.50/24", "dev", "eth0", "label", "eth0:haven"]
    assert calls[5] == ["/usr/sbin/ip", "addr", "del", "192.168.1.50/24", "dev", "eth0"]
    assert calls[7] == ["/usr/sbin/ip", "-o", "-4", "addr", "show", "dev", "eth0"]
    # labels are limited to 15 characters: long interface names get none
    assert "label" not in net.add_argv("192.168.1.50", 24, "ovs_bond0_long")


def test_missing_ip_binary_is_a_clear_fault(monkeypatch):
    monkeypatch.setattr(H.LinuxNet, "ip_bin", staticmethod(lambda: None))
    ok, msg = asyncio.run(H.LinuxNet().add_vip("192.168.1.50", 24, "eth0"))
    assert not ok and "iproute2" in msg


# ------------------------------------------------------------------ the manager
class FakeNet(H.LinuxNet):
    dry_run = False

    def __init__(self, fail_add: str = "", gateway=True):
        super().__init__(run=None)
        self.ops, self.addrs, self.fail_add, self.gw = [], set(), fail_add, gateway

    def detect_iface(self, vip):
        return "eth0"

    async def add_vip(self, vip, prefix, iface):
        self.ops.append(("add", vip, prefix, iface))
        if self.fail_add:
            return False, self.fail_add
        self.addrs.add(vip)
        return True, ""

    async def del_vip(self, vip, prefix, iface):
        self.ops.append(("del", vip, prefix, iface))
        self.addrs.discard(vip)
        return True, ""

    async def has_vip(self, vip, iface):
        return vip in self.addrs

    async def announce(self, vip, iface):
        self.ops.append(("garp", vip, iface))
        return True, ""

    async def gateway_ok(self, check):
        return self.gw


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def cfg(**kw):
    return {"enabled": True, "role": "primary", "peer": "10.0.0.2", "secret": SECRET, "vip": "10.0.0.50",
            "heartbeat_seconds": 2, "failover_after": 5, **kw}


def node(tmp_path, monkeypatch, net=None, **kw):
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    m = H.HAManager(net=net or FakeNet(), clock=Clock())
    m.services = AsyncMock()
    m.services_on = False
    monkeypatch.setattr(H, "dry_run", lambda: False)

    async def no_loop():
        return None
    m._loop = no_loop                         # tests drive the steps themselves
    return m


def setup(m, **kw):
    async def go():
        await m.configure(cfg(**kw))
    asyncio.run(go())


def evaluate(m):
    asyncio.run(m.evaluate())


def test_lone_node_promotes_after_grace_then_demotes_for_higher_peer(tmp_path, monkeypatch):
    m = node(tmp_path, monkeypatch)
    setup(m, role="standby")
    assert m.state == ST
    m._problem("unreachable")                               # first heartbeat refused: still wait for the grace
    evaluate(m)
    assert m.state == ST and not m.net.ops                  # still giving the peer a chance to answer
    m.clock.t += 11
    evaluate(m)
    assert m.state == A and m.has_vip
    assert m.net.ops[0] == ("add", "10.0.0.50", 24, "eth0")
    m.services.assert_awaited_with(True)                    # adapters + automations started
    # the primary comes back and is active (preempted): we step down at once
    asyncio.run(m.on_heartbeat({"node_id": "ffff", "instance": "x", "priority": 200, "state": A}))
    evaluate(m)
    assert m.state == S and not m.has_vip and ("del", "10.0.0.50", 24, "eth0") in m.net.ops
    m.services.assert_awaited_with(False)                   # adapters + automations stopped
    assert m.last_failover["to"] == "peer"
    st = m.status()
    assert st["peer"]["state"] == A and st["events"][0]["to"] == S and st["vip"] == "10.0.0.50"


def test_vip_failure_is_a_fault_but_app_keeps_running(tmp_path, monkeypatch):
    m = node(tmp_path, monkeypatch, net=FakeNet(fail_add=H.NEEDS_CAPS))
    setup(m)
    m.clock.t += 11
    evaluate(m)
    assert m.state == F and m.fault_kind == "vip" and "NET_ADMIN" in m.message
    m.services.assert_awaited_with(True)                    # still serves as a single instance
    assert "fault: Needs host networking" in m.status_line()
    n = len(m.net.ops)
    evaluate(m)
    assert len(m.net.ops) == n                              # doesn't retry `ip` every heartbeat
    # once the other node is active, this one stops its adapters
    asyncio.run(m.on_heartbeat({"node_id": "0", "instance": "x", "priority": 100, "state": A}))
    evaluate(m)
    m.services.assert_awaited_with(False)


def test_isolated_active_node_gives_up_vip(tmp_path, monkeypatch):
    net = FakeNet()
    m = node(tmp_path, monkeypatch, net=net)
    setup(m)
    asyncio.run(m.on_heartbeat({"node_id": "0", "instance": "x", "priority": 100, "state": S}))
    evaluate(m)
    assert m.state == A
    net.gw = False                                          # cable pulled: no peer, no router
    m.clock.t += 11
    evaluate(m)
    assert m.state == F and m.fault_kind == "isolated" and not m.has_vip
    m.services.assert_awaited_with(False)


def test_split_brain_heartbeat_demotes_lower(tmp_path, monkeypatch):
    m = node(tmp_path, monkeypatch)
    setup(m, role="standby")
    m.clock.t += 11
    evaluate(m)
    assert m.state == A
    asyncio.run(m.on_heartbeat({"node_id": "0", "instance": "x", "priority": 200, "state": A}))
    evaluate(m)
    assert m.state == S and not m.net.addrs


def test_wrong_secret_on_peer_never_grabs_vip(tmp_path, monkeypatch):
    m = node(tmp_path, monkeypatch)
    setup(m, role="standby")
    m._problem("secret")
    m.peer_contact = m.clock()
    evaluate(m)
    assert m.state == F and m.fault_kind == "peer" and "shared secret" in m.message
    assert not [o for o in m.net.ops if o[0] == "add"]


def test_stop_removes_vip(tmp_path, monkeypatch):
    m = node(tmp_path, monkeypatch)
    setup(m)
    m.clock.t += 11
    evaluate(m)
    assert m.net.addrs
    asyncio.run(m.stop())
    assert not m.net.addrs and m.net.ops[-1][0] == "del"


def test_disabling_gives_up_vip_and_runs_as_single(tmp_path, monkeypatch):
    m = node(tmp_path, monkeypatch)
    setup(m)
    m.clock.t += 11
    evaluate(m)

    async def off():
        await m.configure({"enabled": False})
    asyncio.run(off())
    assert m.state == H.DISABLED and not m.net.addrs and m.services_on is True


# ------------------------------------------------------------------ two nodes over HTTP (ASGI, no sockets)
def _pair(tmp_path, monkeypatch):
    da, db = tmp_path / "a", tmp_path / "b"
    da.mkdir(), db.mkdir()
    (da / "settings.json").write_text(json.dumps({"mqtt": {"host": "broker"}, "ha": cfg(peer="b.lan")}))
    (da / "layout.json").write_text(json.dumps({"rooms": ["Den"]}))
    (db / "settings.json").write_text(json.dumps({"ha": cfg(role="standby", peer="a.lan")}))
    clock = Clock()
    monkeypatch.setattr(H, "dry_run", lambda: False)
    nodes, apps = {}, {}
    for name, d, role, peer in (("a", da, "primary", "b.lan"), ("b", db, "standby", "a.lan")):
        m = H.HAManager(net=FakeNet(), clock=clock)
        m.data_dir = lambda d=d: d                   # each node has its own data folder
        m.services = AsyncMock()
        m.services_on = False
        m.on_synced = AsyncMock()
        m.node_id = name * 8
        nodes[name] = (m, d, cfg(role=role, peer=peer))
        app = FastAPI()
        app.include_router(H.build_router(lambda m=m: m))
        apps[name] = app
    return nodes, apps, None, clock


def test_two_nodes_heartbeat_sync_failover_and_preempt(tmp_path, monkeypatch):
    nodes, apps, dirs, clock = _pair(tmp_path, monkeypatch)
    (a, da, ca), (b, db, cb) = nodes["a"], nodes["b"]
    down = set()

    def transport(target):
        async def handler(request):
            if target in down:
                raise httpx.ConnectError("down")
            t = httpx.ASGITransport(app=apps[target])
            return await t.handle_async_request(request)
        return httpx.MockTransport(handler)

    async def step(m, d):
        if m is a and "a" in down:
            return
        await m._beat()
        await m._maybe_sync()
        await m.evaluate()

    async def run():
        for (m, d, c), peer in ((nodes["a"], "b"), (nodes["b"], "a")):
            m._loop = AsyncMock()
            await m.configure(c)
            m._client, m._client_verify = httpx.AsyncClient(transport=transport(peer)), False
        for _ in range(3):
            await step(a, da)
            await step(b, db)
        assert (a.state, b.state) == (A, S)
        a.services.assert_awaited_with(True)
        assert b.services.await_count == 0 or b.services.await_args.args == (False,)
        # standby copied the primary's settings, but kept its own HA section
        await step(b, db)
        got = json.loads((db / "settings.json").read_text())
        assert got["mqtt"] == {"host": "broker"} and got["ha"]["role"] == "standby"
        assert json.loads((db / "layout.json").read_text()) == {"rooms": ["Den"]}
        b.on_synced.assert_awaited()
        # wrong secret on a direct request: 403
        bad = H.HAManager()
        bad.cfg, bad._keys = H.HAConfig(cfg(secret="x" * 20)), H.derive_keys("x" * 20)
        bad._client, bad._client_verify = httpx.AsyncClient(transport=transport("a")), False
        r, _ = await bad._request("POST", "/api/ha/peer/heartbeat", {"state": "active"})
        assert r.status_code == 403 and r.json()["reason"] == "signature"
        # the primary dies: the standby takes over after the timeout, not before
        down.add("a")
        clock.t += 4
        await step(b, db)
        assert b.state == S
        clock.t += 7
        await step(b, db)
        assert b.state == A and b.net.addrs == {"10.0.0.50"}
        b.services.assert_awaited_with(True)
        # the primary returns (preempt on): takes the VIP back, the standby steps down
        down.discard("a")
        a.state, a.has_vip, a.net.addrs = ST, False, set()
        a.peer, a.peer_seen = None, 0
        a._started = clock()
        for _ in range(3):
            await step(a, da)
            await step(b, db)
        assert (a.state, b.state) == (A, S)
        assert b.net.addrs == set() and a.net.addrs == {"10.0.0.50"}
        b.services.assert_awaited_with(False)
        # manual hand-over from the primary, then take back from it
        await a.failover("handover")
        for _ in range(2):
            await step(b, db)
            await step(a, da)
        assert (a.state, b.state) == (S, A)
        for _ in range(2):                                   # no preempting straight back after a hand-over
            await step(a, da)
        assert a.state == S
        await a.failover("takeover")
        for _ in range(3):
            await step(b, db)
            await step(a, da)
        assert (a.state, b.state) == (A, S)
        for m in (a, b):
            await m._client.aclose()
    asyncio.run(run())


# ------------------------------------------------------------------ wiring into the app
def test_peer_endpoints_bypass_sessions_but_need_the_secret(monkeypatch, tmp_path):
    from app import main
    m = H.HAManager(net=FakeNet())
    m.cfg, m._keys, m.node_id = H.HAConfig(cfg()), H.derive_keys(SECRET), "n1"
    monkeypatch.setattr(H, "ha", m)
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    c = TestClient(main.app)                                 # no session, no DASH_TOKEN
    body = json.dumps({"node_id": "n2", "instance": "i", "priority": 100, "state": "standby"}).encode()
    hdr, nonce = H.sign_request(m._keys[0], "POST", "/api/ha/peer/heartbeat", body)
    host = {"host": "nas2.example.com"}                      # not on the browser host allow-list: still fine
    r = c.post("/api/ha/peer/heartbeat", content=body, headers={H.AUTH_HEADER: hdr, **host})
    assert r.status_code == 200 and r.json()["node_id"] == "n1"
    assert r.headers[H.SIG_HEADER] == H.sign_response(m._keys[0], nonce, r.content)
    assert c.post("/api/ha/peer/heartbeat", content=body, headers={H.AUTH_HEADER: hdr}).status_code == 403   # replay
    wrong, _ = H.sign_request(H.derive_keys("wrong-secret-wrong-secret")[0], "POST", "/api/ha/peer/heartbeat", body)
    assert c.post("/api/ha/peer/heartbeat", content=body, headers={H.AUTH_HEADER: wrong}).status_code == 403
    assert c.post("/api/ha/peer/heartbeat", content=body).status_code == 403
    assert c.post("/api/ha/peer/heartbeat", content=body, headers={H.AUTH_HEADER: SECRET}).status_code == 403
    assert c.get("/api/ha/peer/config", headers={H.AUTH_HEADER: "nope"}).status_code == 403
    # admin endpoints still need a session
    assert c.get("/api/ha/status").status_code == 401
    assert c.post("/api/ha/failover", json={}).status_code == 401
    st = c.get("/api/ha/status", headers={"authorization": f"Bearer {os.environ['DASH_TOKEN']}"}).json()
    assert st["enabled"] and st["vip"] == "10.0.0.50" and "secret" not in json.dumps(st).lower().replace("shared secret", "")
    monkeypatch.setattr(H, "ha", H.HAManager())
    assert c.post("/api/ha/peer/heartbeat", content=body, headers={H.AUTH_HEADER: hdr}).status_code == 409


def test_manager_suspend_and_resume(monkeypatch):
    from app import main

    class Fake:
        started = 0

        def __init__(self, hub, cfg):
            self.status = "ok"

        async def start(self):
            Fake.started += 1

        async def stop(self):
            Fake.started -= 1
    monkeypatch.setattr(main, "ADAPTERS", {"fake": Fake})
    monkeypatch.setattr(main.settings_mod, "effective_config", lambda: {"fake": {"enabled": True}})
    mgr = main.Manager()

    async def go():
        await mgr.start_all()
        assert Fake.started == 1 and "fake" in main.hub.adapters
        await mgr.suspend()
        assert Fake.started == 0 and "fake" not in main.hub.adapters
        assert mgr.statuses()["fake"].startswith("paused")
        await mgr.restart("fake")                           # a Settings save on the standby: stays stopped
        assert Fake.started == 0
        await mgr.resume()
        assert Fake.started == 1 and main.hub.adapters["fake"]
        await mgr.suspend()
    asyncio.run(go())


def test_ha_services_toggle_automations(monkeypatch):
    from app import main
    eng = AsyncMock()
    eng.paused = False
    monkeypatch.setattr(main, "automations", eng)
    monkeypatch.setattr(main, "manager", AsyncMock())
    asyncio.run(main._ha_services(False))
    assert eng.paused is True
    eng.stop.assert_awaited()
    main.manager.suspend.assert_awaited()
    asyncio.run(main._ha_services(True))
    assert eng.paused is False
    eng.start.assert_awaited()
    main.manager.resume.assert_awaited()


def test_paused_engine_runs_nothing(tmp_path):
    from app.automations import AutomationEngine
    from app.hub import Hub

    class Clk:
        def now(self):
            return 0
    eng = AutomationEngine(Hub(), Clk(), tmp_path)
    eng.items["x"] = {"id": "x", "name": "X", "enabled": True}
    eng.paused = True
    eng.start_run("x", "test")
    assert not eng._running and eng.runs[0]["skipped"].startswith("this Haven is on standby")


def test_dry_run_net_records(monkeypatch):
    net = H.DryRunNet()

    async def go():
        await net.add_vip("10.0.0.50", 24, "dry0")
        assert await net.has_vip("10.0.0.50", "dry0")
        await net.del_vip("10.0.0.50", 24, "dry0")
    asyncio.run(go())
    assert [o[0] for o in net.ops] == ["add", "del"]
