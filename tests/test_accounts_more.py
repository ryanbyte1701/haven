"""More account tests: saving failures roll back, all-or-nothing user changes, the forced first change's
edge cases, wrong-password counting, live updates cut off on revocation, and every /api route probed with
every role (generated from app.routes, so new endpoints are covered automatically)."""
import json
import re

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth
from conftest import ADMIN_PASSWORD

LAN = ("192.168.1.20", 50000)
PW = "correct-horse-battery"
PW2 = "another-good-password"


@pytest.fixture
def m():
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


def _disk(store):
    return json.loads(store.users_file.read_text())


@pytest.fixture
def broken_disk(monkeypatch):
    """Make every users.json save fail like a read-only / wrongly owned config folder on the NAS."""
    def fail(path, *a, **k):
        raise PermissionError(13, "Permission denied", str(path))
    state = {"on": False}
    real = auth.write_private

    def wp(path, *a, **k):
        if state["on"] and str(path).endswith("users.json"):
            fail(path)
        return real(path, *a, **k)
    monkeypatch.setattr(auth, "write_private", wp)
    return state


# ---------------- saving fails: nothing changes in memory either ----------------
def test_failed_saves_roll_back(tmp_path, broken_disk):
    s = auth.AuthStore(tmp_path)
    s.create_user("admin", PW, "admin")
    s.create_user("kim", PW, "user")
    tok = s.new_session("kim", "user")
    before, disk = json.dumps(s.users, sort_keys=True), _disk(s)
    broken_disk["on"] = True
    for call in (lambda: s.set_password("kim", PW2), lambda: s.rename_user("kim", "kimberly"),
                 lambda: s.set_role("kim", "monitor"), lambda: s.delete_user("kim"),
                 lambda: s.create_user("new", PW, "user"), lambda: s.set_guest_view(True),
                 lambda: s.update_user("kim", password=PW2, new_name="kimberly", role="admin")):
        with pytest.raises(PermissionError):
            call()
        assert json.dumps(s.users, sort_keys=True) == before and s.guest_view is False
        assert s.verify("kim", PW) == "user" and s.verify("kim", PW2) is None
        assert s.resolve(tok)["user"] == "kim"                  # not signed out by a change that didn't happen
    assert _disk(s) == disk
    broken_disk["on"] = False
    s.update_user("kim", password=PW2, new_name="Kimberly")
    assert s.verify("kimberly", PW2) == "user" and "kim" not in s.users and s.resolve(tok) is None


def test_forced_change_with_unwritable_config_folder(m, monkeypatch, broken_disk):
    monkeypatch.setattr(auth, "store", auth.AuthStore(auth.store.dir / "fresh"))
    monkeypatch.delenv("HAVEN_ADMIN_USER", raising=False)
    monkeypatch.delenv("HAVEN_ADMIN_PASSWORD", raising=False)
    with client(m) as c:
        h = login(c, "admin", auth.DEFAULT_ADMIN_PASSWORD)
        broken_disk["on"] = True
        r = c.post("/api/me/password", headers=h, json={"new": PW, "username": "ryan"})
        assert r.status_code == 500
        assert "can't save" in r.json()["detail"] and "uid" in r.json()["detail"]   # the clear storage message
        # nothing changed: same name, still must_change, the old password and this session still work
        assert list(auth.store.users) == ["admin"] and auth.store.users["admin"]["must_change"]
        assert c.get("/api/me", headers=h).json() == {"user": "admin", "role": "admin", "role_label": "Admin",
                                                     "guest": False, "must_change": True}
        assert c.post("/api/auth/login", json={"username": "ryan", "password": PW}).status_code == 401
        login(c, "admin", auth.DEFAULT_ADMIN_PASSWORD)
        broken_disk["on"] = False                                 # folder fixed: the same request now works
        r = c.post("/api/me/password", headers=h, json={"new": PW, "username": "ryan"})
        assert r.status_code == 200 and r.json()["user"] == "ryan"
        assert list(_disk(auth.store)["users"]) == ["ryan"]


def test_admin_actions_with_unwritable_config_folder(m, broken_disk, admin_headers):
    with client(m) as c:
        auth.store.create_user("kim", PW, "user")
        broken_disk["on"] = True
        for method, path, body in (("POST", "/api/users", {"name": "new", "password": PW, "role": "user"}),
                                   ("PATCH", "/api/users/kim", {"password": PW2}),
                                   ("PATCH", "/api/users/kim", {"role": "monitor"}),
                                   ("DELETE", "/api/users/kim", None),
                                   ("PUT", "/api/auth/guest_view", {"enabled": True})):
            r = c.request(method, path, headers=admin_headers, json=body)
            assert r.status_code == 500 and "can't save" in r.json()["detail"], (method, path, r.text)
        assert sorted(auth.store.users) == ["admin", "kim"] and auth.store.users["kim"]["role"] == "user"
        assert auth.store.verify("kim", PW) == "user" and auth.store.guest_view is False


# ---------------- all or nothing ----------------
def test_patch_password_and_role_together_is_all_or_nothing(m, admin_headers):
    with client(m) as c:
        # the last admin can't be demoted: the password in the same request must not change either
        r = c.patch("/api/users/admin", headers=admin_headers, json={"password": PW2, "role": "user"})
        assert r.status_code == 400 and "last admin" in r.json()["detail"]
        assert auth.store.verify("admin", ADMIN_PASSWORD) == "admin"
        assert c.get("/api/me", headers=admin_headers).status_code == 200      # and nobody was signed out
        auth.store.create_user("kim", PW, "user")
        r = c.patch("/api/users/kim", headers=admin_headers, json={"password": PW2, "role": "root"})
        assert r.status_code == 400 and auth.store.verify("kim", PW) == "user"
        r = c.patch("/api/users/kim", headers=admin_headers, json={"password": "short", "role": "monitor"})
        assert r.status_code == 400 and auth.store.users["kim"]["role"] == "user"
        r = c.patch("/api/users/kim", headers=admin_headers, json={"password": PW2, "role": "monitor"})
        assert r.status_code == 200 and auth.store.verify("kim", PW2) == "monitor"


def test_forced_change_username_taken_changes_nothing(m, monkeypatch):
    # e.g. a backup that received the primary's users.json, or an admin reset to the default by hand
    s = auth.AuthStore(auth.store.dir / "taken")
    s.create_user("kim", PW, "user")
    s.bootstrap_default()                      # no-op: there are users already
    salt = bytes(16)
    s.users["admin"] = {"role": "admin", "hash": auth.hash_password("haven-admin", salt), "salt": salt.hex(),
                        "created": 0, "must_change": True}
    s._save_users()
    monkeypatch.setattr(auth, "store", s)
    with client(m) as c:
        h = login(c, "admin", "haven-admin")
        r = c.post("/api/me/password", headers=h, json={"new": PW2, "username": "KIM"})
        assert r.status_code == 400 and "already exists" in r.json()["detail"]
        assert auth.store.verify("admin", "haven-admin") == "admin" and auth.store.users["admin"]["must_change"]
        assert auth.store.verify("kim", PW) == "user"
        assert c.get("/api/me", headers=h).status_code == 200            # still signed in for another try
        for bad in ("viewer", "has space", "x" * 33):
            r = c.post("/api/me/password", headers=h, json={"new": PW2, "username": bad})
            assert r.status_code == 400, bad
        # blank / same name in other case: keeps the name
        r = c.post("/api/me/password", headers=h, json={"new": PW2, "username": "  ADMIN "})
        assert r.status_code == 200 and r.json()["user"] == "admin"
        assert "must_change" not in auth.store.users["admin"]


def test_forced_change_blank_username_keeps_name(m, monkeypatch):
    monkeypatch.setattr(auth, "store", auth.AuthStore(auth.store.dir / "blank"))
    monkeypatch.delenv("HAVEN_ADMIN_USER", raising=False)
    monkeypatch.delenv("HAVEN_ADMIN_PASSWORD", raising=False)
    with client(m) as c:
        h = login(c, "admin", auth.DEFAULT_ADMIN_PASSWORD)
        r = c.post("/api/me/password", headers=h, json={"new": PW, "username": "   "})
        assert r.status_code == 200 and r.json()["user"] == "admin"


# ---------------- wrong passwords ----------------
def test_own_sign_in_or_viewer_session_does_not_reset_wrong_password_count(m, admin_headers):
    """Before: any valid session (a viewer's, or the attacker's own account) cleared the per-address count of
    wrong passwords, so guesses at the admin's password were never slowed down."""
    ip = LAN[0]
    with client(m) as c:
        auth.store.create_user("mallory", PW, "monitor")
        c.put("/api/auth/guest_view", headers=admin_headers, json={"enabled": True})
        guest = bearer(c.post("/api/auth/guest").json()["token"])
        for i in range(5):
            assert c.post("/api/auth/login", json={"username": "admin", "password": f"guess-{i}"}).status_code == 401
            c.get("/api/devices", headers=guest)                       # valid viewer session
            login(c, "mallory", PW)                                    # attacker's own account
        assert m.auth_limiter.fails["pw:" + ip][0] == 5
        # ... and the right password is still never slowed down or refused
        assert c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200


def test_429_messages_are_readable(m, admin_headers):
    with client(m) as c:
        m.auth_limiter._waiting = {"pw:" + LAN[0]: 25}
        try:
            r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
            assert r.status_code == 429 and "Too many wrong passwords" in r.json()["detail"]
            assert r.headers.get("retry-after")
            r = c.post("/api/me/password", headers=admin_headers, json={"current": "wrong-password", "new": PW2})
            assert r.status_code == 429 and "Too many" in r.json()["detail"]
        finally:
            m.auth_limiter._waiting = {}
        c.put("/api/auth/guest_view", headers=admin_headers, json={"enabled": True})
        codes = [c.post("/api/auth/guest") for _ in range(12)]
        r = next(x for x in codes if x.status_code == 429)
        assert "Wait a minute" in r.json()["detail"]


# ---------------- live updates ----------------
def _ws_open(c, token):
    ws = c.websocket_connect("/ws").__enter__()
    ws.send_json({"type": "auth", "token": token})
    return ws


def test_must_change_websocket_refused_without_counting_a_wrong_password(m, monkeypatch):
    monkeypatch.setattr(auth, "store", auth.AuthStore(auth.store.dir / "mc"))
    monkeypatch.delenv("HAVEN_ADMIN_USER", raising=False)
    monkeypatch.delenv("HAVEN_ADMIN_PASSWORD", raising=False)
    with client(m) as c:
        h = login(c, "admin", auth.DEFAULT_ADMIN_PASSWORD)
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/ws") as ws:
                ws.send_json({"type": "auth", "token": h["authorization"][7:]})
                ws.receive_json()
        assert e.value.code == 4401
        assert LAN[0] not in m.auth_limiter.fails


@pytest.mark.parametrize("how", ["delete", "role", "password", "logout"])
def test_live_updates_end_when_the_session_is_revoked(m, admin_headers, how):
    with client(m) as c:
        auth.store.create_user("kim", PW, "user")
        kim = login(c, "kim", PW)
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": kim["authorization"][7:]})
            assert ws.receive_json()["type"] == "snapshot"
            if how == "delete":
                c.delete("/api/users/kim", headers=admin_headers)
            elif how == "role":
                c.patch("/api/users/kim", headers=admin_headers, json={"role": "monitor"})
            elif how == "password":
                c.patch("/api/users/kim", headers=admin_headers, json={"password": PW2})
            else:
                c.post("/api/auth/logout", headers=kim)
            c.put("/api/layout", headers=admin_headers, json={"pinned": []})     # any broadcast
            with pytest.raises(WebSocketDisconnect) as e:     # closed instead of getting that broadcast
                ws.receive_json()
            assert e.value.code == 4401


def test_live_updates_end_for_viewers_when_viewing_is_turned_off(m, admin_headers):
    with client(m) as c:
        c.put("/api/auth/guest_view", headers=admin_headers, json={"enabled": True})
        g = c.post("/api/auth/guest").json()["token"]
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": g})
            assert ws.receive_json()["type"] == "snapshot"
            c.put("/api/auth/guest_view", headers=admin_headers, json={"enabled": False})
            c.put("/api/layout", headers=admin_headers, json={"pinned": []})
            with pytest.raises(WebSocketDisconnect) as e:     # closed instead of getting that broadcast
                ws.receive_json()
            assert e.value.code == 4401


def test_still_valid():
    s = auth.store
    s.create_user("kim", PW, "user")
    t = s.new_session("kim", "user")
    assert s.still_valid(t) and not s.still_valid("nope") and not s.still_valid(None)
    s.set_role("kim", "monitor")
    assert not s.still_valid(t)


# ---------------- every route, every role ----------------
def _routes(m):
    """(method, path) for every HTTP route under /api, path parameters filled in."""
    out = []

    def walk(routes):
        for r in routes:
            if hasattr(r, "original_router"):
                walk(r.original_router.routes)
            elif hasattr(r, "routes") and not hasattr(r, "path"):
                walk(r.routes)
            elif getattr(r, "methods", None) and r.path.startswith("/api/"):
                for meth in sorted(r.methods - {"HEAD"}):
                    out.append((meth, re.sub(r"\{[^}]+\}", "x", r.path)))
    walk(m.app.routes)
    return sorted(set(out))


ADMIN_ONLY = re.compile(r"^/api/(settings|users|ecowitt|aqara|hue|ha/(?!status$|where$|peer/)|auth/guest_view)")


def test_every_api_route_with_every_role(m, admin_headers):
    routes = _routes(m)
    assert len(routes) > 30 and ("GET", "/api/users") in routes and ("POST", "/api/me/password") in routes
    with client(m) as c:
        auth.store.create_user("reg", PW, "user")
        auth.store.create_user("mon", PW, "monitor")
        c.put("/api/auth/guest_view", headers=admin_headers, json={"enabled": True})
        heads = {"user": login(c, "reg", PW), "monitor": login(c, "mon", PW),
                 "guest": bearer(c.post("/api/auth/guest").json()["token"])}
        problems = []
        for method, path in routes:
            if auth.is_public(path) or (method, path) in {("POST", "/api/auth/logout")}:
                continue
            for who, h in heads.items():
                r = c.request(method, path, headers=h, json={})
                role = "monitor" if who == "guest" else who
                expect_ok = auth.allowed(role, method, path)
                if who == "guest" and path == "/api/me/password":
                    expect_ok = True                                 # reaches the handler, which says 400
                if method != "GET" or ADMIN_ONLY.match(path):
                    # config, secrets and every write: refused (only a regular user's device commands pass)
                    must_refuse = not (role == "user" and re.fullmatch(r"/api/devices/x/command", path)) \
                        and (method, path) not in auth.SELF_SERVICE and not (method == "GET" and path in auth.READ_OK)
                    if must_refuse and r.status_code != 403:
                        problems.append((who, method, path, r.status_code))
                if not expect_ok and r.status_code != 403:
                    problems.append((who, method, path, r.status_code, "policy says no"))
                if expect_ok and r.status_code in (401, 403):
                    problems.append((who, method, path, r.status_code, "policy says yes"))
            # no token at all: 401 everywhere that isn't public
            r = c.request(method, path, json={})
            if r.status_code != 401:
                problems.append(("nobody", method, path, r.status_code))
        assert not problems, problems
        # admins get through the policy everywhere (the handler may still say 400/404/422 for {} bodies)
        for method, path in routes:
            if method == "GET":
                r = c.get(path, headers=admin_headers)
                assert r.status_code not in (401, 403), (path, r.status_code)
        for path in ("/api/settings", "/api/users", "/api/automations"):
            assert c.get(path, headers=admin_headers).status_code == 200, path


def test_must_change_session_blocked_everywhere_but_the_allowed_set(m, monkeypatch):
    monkeypatch.setattr(auth, "store", auth.AuthStore(auth.store.dir / "mc2"))
    monkeypatch.delenv("HAVEN_ADMIN_USER", raising=False)
    monkeypatch.delenv("HAVEN_ADMIN_PASSWORD", raising=False)
    with client(m) as c:
        h = login(c, "admin", auth.DEFAULT_ADMIN_PASSWORD)
        for method, path in _routes(m):
            if auth.is_public(path) or (method, path) in auth.MUST_CHANGE_OK:
                continue
            r = c.request(method, path, headers=h, json={})
            assert r.status_code == 403 and r.json().get("must_change") is True, (method, path, r.status_code)
        assert c.get("/api/me", headers=h).status_code == 200
        assert list(auth.store.users) == ["admin"] and auth.store.users["admin"]["must_change"]


def test_forwarded_for_and_private_addresses():
    """The default-password sign-in is LAN-only: what counts as LAN, and who may set X-Forwarded-For."""
    import os
    import app.main as main
    for ip in ("192.168.1.5", "10.0.0.2", "172.16.4.1", "127.0.0.1", "::1", "fe80::1", "fd00::5",
               "::ffff:192.168.1.5", "169.254.3.3"):
        assert auth.is_private_ip(ip), ip
    for ip in ("8.8.8.8", "100.64.0.1", "::ffff:8.8.8.8", "2001:4860::8888", "?", "", "not-an-ip"):
        assert not auth.is_private_ip(ip), ip
    old = os.environ.pop("FORWARDED_ALLOW_IPS", None)
    try:
        assert main._trusted_proxy("127.0.0.1") and not main._trusted_proxy("192.168.1.9")
        os.environ["FORWARDED_ALLOW_IPS"] = "192.168.1.9, 10.0.0.1"
        assert main._trusted_proxy("192.168.1.9") and main._trusted_proxy("10.0.0.1")
    finally:
        os.environ.pop("FORWARDED_ALLOW_IPS", None)
        if old is not None:
            os.environ["FORWARDED_ALLOW_IPS"] = old


def test_forwarded_header_from_untrusted_peer_is_ignored(m, monkeypatch):
    """uvicorn (proxy_headers=True) only rewrites the client address for peers in FORWARDED_ALLOW_IPS; a
    client on the internet can't claim to be on the LAN with X-Forwarded-For."""
    import asyncio
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]
    for peer, trusted, fwd, want in (("8.8.8.8", "127.0.0.1", "192.168.1.5", "8.8.8.8"),
                                     ("127.0.0.1", "127.0.0.1", "8.8.8.8", "8.8.8.8"),
                                     ("127.0.0.1", "127.0.0.1", "192.168.1.5", "192.168.1.5"),
                                     ("127.0.0.1", "127.0.0.1", "192.168.1.5, 8.8.8.8", "8.8.8.8")):
        mw = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
        scope = {"type": "http", "client": (peer, 1), "headers": [(b"x-forwarded-for", fwd.encode())],
                 "scheme": "http"}
        asyncio.run(mw(scope, None, None))
        assert seen["client"] == want, (peer, fwd, seen)
        assert auth.is_private_ip(seen["client"]) == auth.is_private_ip(want)
