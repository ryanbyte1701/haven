"""Second security review: host allow-list, password-less writes, guessing limits, WS auth,
Ecowitt gateway trust, body limits, redaction."""
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
    with TestClient(m.app) as c:
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


def test_no_password_blocks_account_writes(client, monkeypatch):
    c, m = client
    monkeypatch.setattr(m, "NO_PW_OK", False)
    monkeypatch.setattr(m, "DASH_TOKEN", "")
    r = c.put("/api/settings/mqtt", json={"enabled": True})
    assert r.status_code == 403 and "password" in r.json()["detail"]
    assert c.post("/api/automations", json={}).status_code == 403
    assert c.get("/api/settings").status_code == 200            # reading is still fine
    assert c.put("/api/layout", json={"pinned": []}).status_code == 200


def test_guessing_is_slowed(client, monkeypatch):
    c, m = client
    monkeypatch.setattr(m, "DASH_TOKEN", "a-long-enough-pass")
    codes = [c.get("/api/devices", headers={"authorization": "Bearer nope"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and 429 in codes[10:]
    assert c.get("/api/devices", headers={"authorization": "Bearer a-long-enough-pass"}).status_code == 429   # still waiting
    m.auth_limiter.fails.clear()
    assert c.get("/api/devices", headers={"authorization": "Bearer a-long-enough-pass"}).status_code == 200
    assert c.get("/api/devices?token=a-long-enough-pass").status_code == 401           # no tokens in URLs


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
