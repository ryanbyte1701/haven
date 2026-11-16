"""User accounts, roles, sessions and the first-run / DASH_TOKEN migration (app/auth.py, app/auth_api.py)."""
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth

LAN = ("192.168.1.20", 50000)
PW = "correct-horse-battery"


@pytest.fixture
def m(monkeypatch):
    import app.main as main
    main.auth_limiter.fails.clear()
    from app import auth_api
    auth_api.guest_limiter.buckets.clear()
    return main


def client(m, **kw):
    return TestClient(m.app, client=LAN, **kw)


def bearer(tok):
    return {"authorization": f"Bearer {tok}"}


def login(c, name, pw):
    r = c.post("/api/auth/login", json={"username": name, "password": pw})
    assert r.status_code == 200, r.text
    return bearer(r.json()["token"])


# ---------------- store ----------------
def test_hashing_and_files(tmp_path):
    s = auth.AuthStore(tmp_path)
    s.create_user("Alice", "a-very-long-pw", "user")       # names are lower-cased
    u = s.users["alice"]
    assert u["hash"] != "a-very-long-pw" and len(bytes.fromhex(u["salt"])) == 16
    assert u["hash"] == auth.hash_password("a-very-long-pw", bytes.fromhex(u["salt"]))
    assert s.verify("alice", "a-very-long-pw") == "user"
    assert s.verify("ALICE", "a-very-long-pw") == "user"
    assert s.verify("alice", "wrong-password") is None and s.verify("nobody", "x") is None
    s.create_user("bob", "a-very-long-pw", "user")
    assert s.users["bob"]["hash"] != u["hash"]               # per-user salt
    assert stat.S_IMODE(os.stat(tmp_path / "users.json").st_mode) == 0o600
    for bad in ("", "has space", "x" * 33, "viewer", "ümlaut"):
        with pytest.raises(ValueError):
            s.create_user(bad, "a-very-long-pw", "user")
    with pytest.raises(ValueError):
        s.create_user("carol", "short", "user")
    with pytest.raises(ValueError):
        s.create_user("carol", "a-very-long-pw", "root")
    with pytest.raises(ValueError):
        s.create_user("alice", "a-very-long-pw", "user")


def test_sessions_survive_restart_and_are_hashed(tmp_path):
    s = auth.AuthStore(tmp_path)
    s.create_user("admin", PW, "admin")
    tok = s.new_session("admin", "admin")
    raw = (tmp_path / "sessions.json").read_text()
    assert tok not in raw                                     # only sha256(token) is stored
    assert stat.S_IMODE(os.stat(tmp_path / "sessions.json").st_mode) == 0o600
    s2 = auth.AuthStore(tmp_path)                             # "restart"
    assert s2.resolve(tok) == {"user": "admin", "role": "admin", "guest": False}
    assert s2.resolve("nope") is None
    # idle for more than 30 days: gone
    data = json.loads(raw)
    for v in data.values():
        v["last_seen"] -= auth.IDLE_EXPIRY + 10
    (tmp_path / "sessions.json").write_text(json.dumps(data))
    assert auth.AuthStore(tmp_path).resolve(tok) is None


def test_last_admin_protection(tmp_path):
    s = auth.AuthStore(tmp_path)
    s.create_user("root", PW, "admin")
    with pytest.raises(ValueError):
        s.set_role("root", "user")
    with pytest.raises(ValueError):
        s.delete_user("root")
    s.create_user("second", PW, "admin")
    s.set_role("root", "monitor")
    with pytest.raises(ValueError):
        s.delete_user("second")


def test_policy_matrix():
    A = auth.allowed
    for role in ("monitor", "user", "admin"):
        assert A(role, "GET", "/api/devices") and A(role, "GET", "/api/layout")
        assert A(role, "GET", "/api/me") and A(role, "POST", "/api/auth/logout") and A(role, "GET", "/api/ha/status")
    for path in ("/api/settings", "/api/settings/hue/discover", "/api/automations", "/api/users", "/api/ha/config",
                 "/api/ecowitt/gateways", "/api/aqara/resources/x"):
        assert not A("monitor", "GET", path), path
    assert not A("user", "GET", "/api/settings") and A("user", "GET", "/api/automations")
    assert A("user", "GET", "/api/ring/x/snapshot") and not A("monitor", "GET", "/api/ring/x/snapshot")   # cameras
    assert A("user", "POST", "/api/devices/demo:lamp/command") and not A("monitor", "POST", "/api/devices/demo:lamp/command")
    for method, path in (("PUT", "/api/layout"), ("PATCH", "/api/devices/x"), ("POST", "/api/automations"),
                         ("PUT", "/api/settings/mqtt"), ("POST", "/api/users"), ("PUT", "/api/auth/guest_view"),
                         ("POST", "/api/devices/x/command/extra")):
        assert not A("user", method, path) and not A("monitor", method, path) and A("admin", method, path)


# ---------------- API ----------------
def test_bootstrap_from_dash_token(m):
    with client(m) as c:                                      # conftest sets DASH_TOKEN
        assert list(auth.store.users) == ["admin"] and auth.store.users["admin"]["role"] == "admin"
        assert c.get("/api/auth/state").json() == {"setup_needed": False, "guest_view": False}   # upgrade stays private
        assert c.post("/api/auth/guest").status_code == 403
        h = login(c, "admin", m.DASH_TOKEN)                   # sign in as admin with the old password
        assert c.get("/api/me", headers=h).json()["role"] == "admin"
        assert c.get("/api/settings", headers=bearer(m.DASH_TOKEN)).status_code == 200   # scripts keep working
        assert c.get("/api/me", headers=bearer(m.DASH_TOKEN)).json()["dash_token"] is True
        assert c.post("/api/auth/setup", json={"username": "x", "password": PW}).status_code == 409


def test_first_run_setup_once_lan_only_with_code(m, monkeypatch):
    monkeypatch.setattr(m, "DASH_TOKEN", "")
    body = {"username": "me", "password": PW, "guest_view": True}
    with TestClient(m.app, client=("8.8.8.8", 1)) as c:      # internet address
        code = auth.store.setup_code()
        assert c.post("/api/auth/setup", json={**body, "code": code}).status_code == 403
        assert c.get("/api/auth/state").json()["guest_view"] is False      # no viewing from outside the LAN
        assert c.post("/api/auth/guest").status_code == 403
    with client(m) as c:
        assert auth.store.setup_code() == code                 # the same code as in the log
        st = c.get("/api/auth/state").json()
        assert st == {"setup_needed": True, "guest_view": True}
        assert c.get("/api/devices").status_code == 401
        assert c.post("/api/auth/setup", json={**body, "code": "WRONG-CODE"}).status_code == 400
        assert c.post("/api/auth/setup", json={**body, "code": code, "password": "short"}).status_code == 400
        r = c.post("/api/auth/setup", json={**body, "code": code.lower().replace("-", "")})
        assert r.status_code == 200 and r.json()["role"] == "admin"
        assert c.get("/api/settings", headers=bearer(r.json()["token"])).status_code == 200
        assert c.post("/api/auth/setup", json={**body, "username": "evil", "code": code}).status_code == 409
        assert list(auth.store.users) == ["me"] and auth.store.guest_view is True
        assert c.post("/api/auth/guest").status_code == 200


def test_setup_guest_view_off(m, monkeypatch):
    monkeypatch.setattr(m, "DASH_TOKEN", "")
    with client(m) as c:
        r = c.post("/api/auth/setup", json={"username": "me", "password": PW, "code": auth.store.setup_code()})
        assert r.status_code == 200 and auth.store.guest_view is False
        assert c.post("/api/auth/guest").status_code == 403


def test_login_logout_and_errors(m):
    with client(m) as c:
        auth.store.create_user("kim", PW, "user")
        r = c.post("/api/auth/login", json={"username": "kim", "password": "wrong-password"})
        assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password"
        r2 = c.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password"})
        assert r2.json()["detail"] == r.json()["detail"]      # no hint which part was wrong
        h = login(c, "Kim", PW)
        assert c.get("/api/me", headers=h).json() == {"user": "kim", "role": "user", "role_label": "Regular",
                                                      "guest": False, "dash_token": False}
        assert c.post("/api/auth/logout", headers=h).json() == {"ok": True}
        assert c.get("/api/me", headers=h).status_code == 401


def test_login_slows_wrong_guesses_but_never_locks_out(m):
    import time
    with client(m) as c:
        t0 = time.monotonic()
        codes = [c.post("/api/auth/login", json={"username": "admin", "password": f"nope-{i}"}).status_code
                 for i in range(12)]
        assert codes == [401] * 12 and time.monotonic() - t0 >= 0.7
        t0 = time.monotonic()
        assert c.post("/api/auth/login", json={"username": "admin", "password": m.DASH_TOKEN}).status_code == 200
        assert time.monotonic() - t0 < 0.5


def test_guest_on_off(m):
    with client(m) as c:
        admin = login(c, "admin", m.DASH_TOKEN)
        assert c.post("/api/auth/guest").status_code == 403                    # off after a DASH_TOKEN upgrade
        c.put("/api/auth/guest_view", headers=admin, json={"enabled": True})
        r = c.post("/api/auth/guest").json()
        assert r["role"] == "monitor" and r["guest"] and r["user"] == "viewer"
        g = bearer(r["token"])
        assert c.get("/api/devices", headers=g).status_code == 200
        assert c.post("/api/me/password", headers=g, json={"current": "x", "new": PW}).status_code == 400
        assert c.put("/api/auth/guest_view", headers=g, json={"enabled": False}).status_code == 403
        assert c.put("/api/auth/guest_view", headers=admin, json={"enabled": False}).json()["guest_view"] is False
        assert c.get("/api/devices", headers=g).status_code == 401           # existing viewers are signed out
        assert c.post("/api/auth/guest").status_code == 403
        assert c.get("/api/auth/state").json()["guest_view"] is False
        c.put("/api/auth/guest_view", headers=admin, json={"enabled": True})
        assert c.post("/api/auth/guest").status_code == 200


def _ws(c, token):
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        return ws.receive_json()["type"]


def test_role_matrix_http_and_ws(m):
    with client(m) as c:
        admin = login(c, "admin", m.DASH_TOKEN)
        c.post("/api/users", headers=admin, json={"name": "reg", "password": PW, "role": "user"})
        c.post("/api/users", headers=admin, json={"name": "mon", "password": PW, "role": "monitor"})
        user, mon = login(c, "reg", PW), login(c, "mon", PW)
        c.put("/api/auth/guest_view", headers=admin, json={"enabled": True})
        guest = bearer(c.post("/api/auth/guest").json()["token"])
        dev = next(iter(m.hub.devices), None) or "demo:none"
        cmd = {"capability": "light", "value": True}
        for h in (mon, guest):
            assert c.get("/api/devices", headers=h).status_code == 200
            assert c.get("/api/layout", headers=h).status_code == 200
            assert c.get("/api/settings", headers=h).status_code == 403
            assert c.get("/api/automations", headers=h).status_code == 403
            assert c.get("/api/users", headers=h).status_code == 403
            assert c.get("/api/ecowitt/gateways", headers=h).status_code == 403
            assert c.get(f"/api/ring/{dev}/snapshot", headers=h).status_code == 403
            assert c.post(f"/api/devices/{dev}/command", headers=h, json=cmd).status_code == 403
            assert c.put("/api/layout", headers=h, json={"pinned": []}).status_code == 403
            cfg = c.get("/api/config", headers=h).json()
            assert "adapters" not in cfg and "ui" in cfg
            assert c.get("/api/health", headers=h).json() == {"ok": True, "auth": True}
            assert _ws(c, h["authorization"][7:]) == "snapshot"
        # regular user: + commands, nothing else that writes
        assert c.post(f"/api/devices/{dev}/command", headers=user, json=cmd).status_code != 403
        assert c.get("/api/automations", headers=user).status_code == 200
        assert c.get("/api/settings", headers=user).status_code == 403
        assert c.get("/api/ecowitt/gateways", headers=user).status_code == 403
        assert c.get(f"/api/ring/{dev}/snapshot", headers=user).status_code != 403
        assert c.put("/api/settings/mqtt", headers=user, json={"enabled": False}).status_code == 403
        assert c.post("/api/automations", headers=user, json={}).status_code == 403
        assert c.patch(f"/api/devices/{dev}", headers=user, json={"name": "x"}).status_code == 403
        assert c.put("/api/layout", headers=user, json={"pinned": []}).status_code == 403
        assert c.get("/api/users", headers=user).status_code == 403
        assert _ws(c, user["authorization"][7:]) == "snapshot"
        # admin: everything
        assert c.get("/api/settings", headers=admin).status_code == 200
        assert "adapters" in c.get("/api/config", headers=admin).json()
        assert "adapters" in c.get("/api/health", headers=admin).json()
        assert c.put("/api/layout", headers=admin, json={"pinned": []}).status_code == 200
        # no / bad token
        assert c.get("/api/devices").status_code == 401
        with pytest.raises(WebSocketDisconnect) as e:
            _ws(c, "bogus")
        assert e.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/ws") as ws:
                ws.send_json({"type": "hello"})
                ws.receive_json()
        assert e.value.code == 4401


def test_user_management_and_last_admin(m):
    with client(m) as c:
        admin = login(c, "admin", m.DASH_TOKEN)
        r = c.post("/api/users", headers=admin, json={"name": "pat", "password": "short", "role": "user"})
        assert r.status_code == 400 and "10" in r.json()["detail"]
        assert c.post("/api/users", headers=admin, json={"name": "pat", "password": PW, "role": "user"}).status_code == 200
        assert c.post("/api/users", headers=admin, json={"name": "pat", "password": PW, "role": "user"}).status_code == 400
        pat = login(c, "pat", PW)
        # role change signs pat out
        users = c.patch("/api/users/pat", headers=admin, json={"role": "monitor"}).json()["users"]
        assert {"name": "pat", "role": "monitor"}.items() <= next(u for u in users if u["name"] == "pat").items()
        assert c.get("/api/me", headers=pat).status_code == 401
        pat = login(c, "pat", PW)
        # password reset signs pat out, new password works
        c.patch("/api/users/pat", headers=admin, json={"password": "another-long-one"})
        assert c.get("/api/me", headers=pat).status_code == 401
        pat = login(c, "pat", "another-long-one")
        # last admin can't be demoted or deleted
        r = c.patch("/api/users/admin", headers=admin, json={"role": "user"})
        assert r.status_code == 400 and "last admin" in r.json()["detail"]
        assert c.delete("/api/users/admin", headers=admin).status_code == 400
        assert c.delete("/api/users/ghost", headers=admin).status_code == 404
        assert c.delete("/api/users/pat", headers=admin).status_code == 200
        assert c.get("/api/me", headers=pat).status_code == 401
        assert "users.json" in os.listdir(auth.store.dir) and "hash" not in json.dumps(c.get("/api/users", headers=admin).json())


def test_password_change_revokes_other_sessions(m):
    with client(m) as c:
        auth.store.create_user("sam", PW, "user")
        phone, laptop = login(c, "sam", PW), login(c, "sam", PW)
        r = c.post("/api/me/password", headers=laptop, json={"current": "wrong-one-xx", "new": "brand-new-password"})
        assert r.status_code == 400
        r = c.post("/api/me/password", headers=laptop, json={"current": PW, "new": "short"})
        assert r.status_code == 400
        r = c.post("/api/me/password", headers=laptop, json={"current": PW, "new": "brand-new-password"})
        assert r.status_code == 200
        assert c.get("/api/me", headers=phone).status_code == 401
        assert c.get("/api/me", headers=laptop).status_code == 401
        assert c.get("/api/me", headers=bearer(r.json()["token"])).json()["user"] == "sam"   # this device stays in
        login(c, "sam", "brand-new-password")


def test_sessions_survive_server_restart(m, monkeypatch):
    with client(m) as c:
        h = login(c, "admin", m.DASH_TOKEN)
    monkeypatch.setattr(auth, "store", auth.AuthStore(auth.store.dir))   # module state reloaded from disk
    with client(m) as c:
        assert c.get("/api/me", headers=h).json()["user"] == "admin"


def test_metrics_accepts_admin_session(m, monkeypatch):
    cls = type(m.history_store)
    monkeypatch.setattr(cls, "metrics_enabled", property(lambda self: True))
    monkeypatch.setattr(cls, "metrics_token", property(lambda self: ""))
    with client(m) as c:
        admin = login(c, "admin", m.DASH_TOKEN)
        auth.store.create_user("reg", PW, "user")
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers=login(c, "reg", PW)).status_code == 401
        assert c.get("/metrics", headers=bearer(m.DASH_TOKEN)).status_code == 200
        assert c.get("/metrics", headers=admin).status_code == 200


def test_health_metrics_ws_count_wrong_tokens(m):
    with client(m) as c:
        c.get("/api/health", headers=bearer("guess-1"))
        c.get("/metrics", headers=bearer("guess-2"))
        with pytest.raises(WebSocketDisconnect):
            _ws(c, "guess-3")
        assert m.auth_limiter.fails[LAN[0]][0] >= 2           # /metrics may be off (404) in this config
        with pytest.raises(WebSocketDisconnect) as e:          # a WS that never authenticates isn't a guess
            with c.websocket_connect("/ws") as ws:
                ws.send_json({"type": "auth"})
                ws.receive_json()
        assert e.value.code == 4401
        n = m.auth_limiter.fails[LAN[0]][0]
        assert _ws(c, m.DASH_TOKEN) == "snapshot" and LAN[0] not in m.auth_limiter.fails
        assert n >= 2
