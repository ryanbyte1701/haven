"""Second security review: host allow-list, password-less writes, guessing limits, WS auth,
Ecowitt gateway trust, body limits, redaction."""
from conftest import ADMIN  # noqa: E402  (test admin session)
import pytest
from app import security as sec


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HAVEN_DATA", str(tmp_path))
    from fastapi.testclient import TestClient
    import app.main as m
    from app.adapters import ecowitt
    monkeypatch.setattr(ecowitt.EcowittAdapter, "_state_path", lambda self: tmp_path / "ecowitt_gateways.json")
    m.auth_limiter.fails.clear()
    m.ecowitt_limiter.buckets.clear()
    had = "ecowitt" in m.hub.adapters
    with TestClient(m.app, headers=ADMIN) as c:
        yield c, m
    if not had:                                  # leave the shared hub as we found it
        m.hub.adapters.pop("ecowitt", None)
        m.hub.remove_source("ecowitt")


def test_host_allowed():
    for h in ("192.168.1.20:1701", "[fe80::1]:1702", "diskstation:1701", "haven.local", "nas.lan", "localhost:1701",
              "testserver", "nas.home.arpa"):
        assert sec.host_allowed(h, extra=set()), h
    for h in ("rebind.attacker.example:1701", "evil.com", "", "x.ts.net"):
        assert not sec.host_allowed(h, extra=set()), h
    assert sec.host_allowed("haven.example.com", extra={"haven.example.com"})


def test_rebinding_host_refused(client):
    c, m = client
    r = c.post("/api/devices/demo:front_lock/command", json={"capability": "lock", "value": "unlock"},
               headers={"host": "rebind.attacker.example:1701", "origin": "http://rebind.attacker.example:1701"})
    assert r.status_code == 421
    assert c.get("/", headers={"host": "rebind.attacker.example"}).status_code == 421
    assert c.get("/api/health", headers={"host": "192.168.1.5:1701"}).status_code == 200


def test_forwarded_host_only_from_trusted_proxy(client, monkeypatch):
    c, m = client
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.9.9.9")      # testclient isn't this address
    r = c.put("/api/layout", json={"pinned": []}, headers={"origin": "https://evil.example", "x-forwarded-host": "evil.example"})
    assert r.status_code in (403, 421)


def test_signed_out_and_guests_are_view_only(client, monkeypatch):
    """Without a session nothing works; a viewer (guest viewing on) only reads."""
    c, m = client
    from app import auth
    c.headers.pop("authorization", None)
    monkeypatch.setattr(auth, "is_private_ip", lambda ip: True)          # the test client isn't an IP address
    assert c.put("/api/settings/mqtt", json={"enabled": True}).status_code == 401
    assert c.post("/api/automations", json={}).status_code == 401
    auth.store.set_guest_view(True)
    assert c.get("/api/auth/state").json() == {"guest_view": True, "default_login": False}
    g = {"authorization": "Bearer " + c.post("/api/auth/guest").json()["token"]}
    assert c.get("/api/devices", headers=g).status_code == 200
    assert c.get("/api/settings", headers=g).status_code == 403
    assert c.put("/api/layout", json={"pinned": []}, headers=g).status_code == 403


def test_guessing_is_slowed(client, monkeypatch):
    """Wrong tokens are answered more and more slowly; the right one is never locked out."""
    import time
    c, m = client
    from conftest import ADMIN_TOKEN
    t0 = time.monotonic()
    codes = [c.get("/api/devices", headers={"authorization": "Bearer nope"}).status_code for _ in range(12)]
    assert codes == [401] * 12 and time.monotonic() - t0 >= 0.7      # 11th and 12th waited 0.25 + 0.5 s
    t0 = time.monotonic()
    assert c.get("/api/devices", headers={"authorization": f"Bearer {ADMIN_TOKEN}"}).status_code == 200
    assert time.monotonic() - t0 < 0.5 and not m.auth_limiter.fails                  # right one: at once, counter cleared
    assert c.get(f"/api/devices?token={ADMIN_TOKEN}", headers={"authorization": ""}).status_code == 401  # no tokens in URLs
    c.get("/api/devices", headers={"authorization": ""})                              # no token is not a guess
    c.get("/api/auth/state", headers={"authorization": "Bearer stale-session"})      # nor a stale one on sign-in pages
    assert not m.auth_limiter.fails


def test_limiter_key_ignores_spoofed_forwarded_for(monkeypatch):
    """Behind uvicorn's proxy_headers, X-Forwarded-For only counts from a trusted proxy: rotating it from
    anywhere else doesn't spread guesses over many keys."""
    import app.main as m
    from fastapi.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    m.auth_limiter.fails.clear()
    wrapped = ProxyHeadersMiddleware(m.app, trusted_hosts="127.0.0.1")
    with TestClient(wrapped, client=("192.168.1.50", 4000)) as c:
        for i in range(3):
            c.get("/api/devices", headers={"authorization": "Bearer wrong", "x-forwarded-for": f"10.0.0.{i}"})
    assert set(m.auth_limiter.fails) == {"192.168.1.50"} and m.auth_limiter.fails["192.168.1.50"][0] == 3
    m.auth_limiter.fails.clear()
    with TestClient(wrapped, client=("127.0.0.1", 4000)) as c:            # the trusted proxy: its last hop counts
        c.get("/api/devices", headers={"authorization": "Bearer wrong", "x-forwarded-for": "6.6.6.6, 192.168.1.77"})
    assert set(m.auth_limiter.fails) == {"192.168.1.77"}
    m.auth_limiter.fails.clear()


def test_docs_disabled(client):
    c, _ = client
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(p).status_code == 404


def test_body_limit(client):
    c, _ = client
    r = c.post("/data/report", content=b"PASSKEY=x&" + b"a" * 70000, headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    assert c.post("/api/automations", content=b"{" + b" " * (1024 * 1024 + 10) + b"}",
                  headers={"content-type": "application/json"}).status_code == 413


def _ecowitt(m):
    from app.adapters.ecowitt import EcowittAdapter
    a = m.hub.adapters.get("ecowitt")
    if a is None:
        a = m.hub.adapters["ecowitt"] = EcowittAdapter(m.hub, {})
    return a


def test_ecowitt_gateway_trust(client):
    c, m = client
    a = _ecowitt(m)
    before = len(m.hub.devices)
    key = "0123456789ABCDEF0123456789A1B2C3"
    assert c.post("/data/report", data={"PASSKEY": key, "leak_ch1": "0"}).text == "OK"
    assert len(m.hub.devices) == before                         # unknown gateway: nothing created
    g = c.get("/api/ecowitt/gateways").json()
    assert len(g["pending"]) == 1 and key not in str(g)          # passkey never shown in full
    gid = g["pending"][0]["id"]
    assert c.post(f"/api/ecowitt/gateways/{gid}/approve").status_code == 200
    m.ecowitt_limiter.buckets.clear()
    c.post("/data/report", data={"PASSKEY": key, "leak_ch1": "0"})
    assert len(m.hub.devices) > before
    # same last-6 characters, different PASSKEY: not trusted, can't touch the real device
    m.ecowitt_limiter.buckets.clear()
    c.post("/data/report", data={"PASSKEY": "xxA1B2C3", "leak_ch1": "1"})
    leak = [d for d in m.hub.devices.values() if d.source == "ecowitt" and "leak" in d.capabilities]
    assert leak and all(d.capabilities["leak"].value is False for d in leak)
    # the secret path trusts a gateway at once; a wrong secret is a 404
    m.ecowitt_limiter.buckets.clear()
    assert c.post("/data/report/wrong-secret-xxxxxxxx", data={"PASSKEY": "K2", "tempf": "70"}).status_code == 404
    path = c.get("/api/ecowitt/gateways").json()["push_path"]
    m.ecowitt_limiter.buckets.clear()
    assert c.post(path, data={"PASSKEY": "SECRETPATHGW", "tempf": "70"}).text == "OK"
    assert any(x["passkey"].endswith("THGW") for x in c.get("/api/ecowitt/gateways").json()["approved"])
    # flooding with random PASSKEYs creates no devices and at most 4 pending entries
    n = len(m.hub.devices)
    for i in range(20):
        m.ecowitt_limiter.buckets.clear()
        c.post("/data/report", data={"PASSKEY": f"rand{i:04d}", "tempf": "70"})
    assert len(m.hub.devices) == n and len(c.get("/api/ecowitt/gateways").json()["pending"]) <= 4


def test_ecowitt_rate_limit(client):
    c, m = client
    _ecowitt(m)
    codes = [c.post("/data/report", data={"PASSKEY": "zz", "tempf": "1"}).status_code for _ in range(10)]
    assert 429 in codes


def test_redact():
    s = "error: Server error '500' for url 'https://www.airnowapi.org/x?zipCode=1&API_KEY=SECRET-KEY-123'"
    out = sec.redact(s)
    assert "SECRET-KEY-123" not in out and "airnowapi.org" in out
    assert "hunter2xx" not in sec.redact("login failed for hunter2xx", ["hunter2xx"])
    assert sec.redact("password=abc&x=1") == "password=…&x=1"
