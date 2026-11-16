import asyncio

import pytest

import app.adapters.nut as nutmod
from app.adapters.nut import NutAdapter
from app.hub import Hub


def make(server_cfg):
    a = NutAdapter(Hub(), {"server": server_cfg})
    a.relay["ups"] = {"device": "nut:x", "vars": {"ups.status": "OL"}, "ok": 1e18, "fsd": False}
    a._real_names.add("ups")
    a.clients["c"] = {}
    return a


def session(a, *cmds):
    s = {"user": None, "password": None, "login": None, "primary": False, "auth_failed": False}
    return [a._reply(c.split(), s, a._users(), "c") for c in cmds]


STRONG = "correct-horse-battery"


def test_fsd_refused_with_weak_password():
    for pw in ("secret", "", "short-pw", "SECRET"):
        a = make({"allow_fsd": True, "user": "monuser", "password": pw})
        out = session(a, "USERNAME monuser", f"PASSWORD {pw}" if pw else "PASSWORD x", "LOGIN ups", "PRIMARY ups", "FSD ups")
        assert out[3].startswith("ERR ACCESS-DENIED") and out[4].startswith("ERR ACCESS-DENIED")
        assert not a.relay["ups"]["fsd"]
        assert not a.fsd_allowed()
        a.server_cfg["enabled"] = True
        assert a._ok_status() == "ok · FSD disabled: set a strong NUT password"


def test_fsd_allowed_with_strong_password():
    a = make({"allow_fsd": True, "enabled": True, "user": "monuser", "password": STRONG})
    out = session(a, "USERNAME monuser", f"PASSWORD {STRONG}", "PRIMARY ups", "FSD ups")
    assert out[2].startswith("OK") and out[3] == "OK FSD-SET\n"
    assert a._ok_status() == "ok"


def test_wrong_password_marks_auth_failure():
    a = make({"user": "monuser", "password": STRONG})
    s = {"user": None, "password": None, "login": None, "primary": False, "auth_failed": False}
    for c in ("USERNAME monuser", "PASSWORD nope"):
        a._reply(c.split(), s, a._users(), "c")
    assert a._reply(["LOGIN", "ups"], s, a._users(), "c").startswith("ERR ACCESS-DENIED")
    assert s["auth_failed"]


# ---------------- live server ----------------
async def start(a):
    a.server_cfg.update(enabled=True, bind="127.0.0.1", port=0)
    a.server = await asyncio.start_server(a._handle, host="127.0.0.1", port=0)
    return a.server.sockets[0].getsockname()[1]


async def conn(port, *lines):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    out = []
    for line in lines:
        w.write(line.encode() + b"\n")
        await w.drain()
        out.append((await asyncio.wait_for(r.readline(), 3)).decode())
    return r, w, out


async def closed(r, timeout=3.0) -> bool:
    try:
        while True:
            data = await asyncio.wait_for(r.read(1024), timeout)
            if not data:
                return True
    except asyncio.TimeoutError:
        return False


def test_prelogin_ver_does_not_keep_connection(monkeypatch):
    monkeypatch.setattr(nutmod, "LOGIN_TIMEOUT", 0.3)

    async def go():
        a = make({})
        port = await start(a)
        r, w, out = await conn(port, "VER")
        assert out[0].startswith("Network UPS Tools")
        await asyncio.sleep(0.15)
        w.write(b"VER\n"); await w.drain()             # VER is not activity: deadline unchanged
        assert await closed(r, 1.0)
        a.server.close()
    asyncio.run(go())


def test_readonly_poller_stays_until_lifetime(monkeypatch):
    monkeypatch.setattr(nutmod, "LOGIN_TIMEOUT", 0.3)
    monkeypatch.setattr(nutmod, "PRELOGIN_LIFETIME", 1.2)

    async def go():
        a = make({})
        port = await start(a)
        r, w, _ = await conn(port)
        for _ in range(3):                             # poll LIST/GET every 0.2s past the login timeout
            w.write(b"GET VAR ups ups.status\n"); await w.drain()
            assert (await asyncio.wait_for(r.readline(), 2)).startswith(b"VAR ups ups.status")
            await asyncio.sleep(0.2)
        assert not r.at_eof()
        assert await closed(r, 2.0)                    # absolute pre-login lifetime reached
        a.server.close()
    asyncio.run(go())


def test_prelogin_cannot_exhaust_per_ip_slots():
    async def go():
        a = make({"user": "monuser", "password": STRONG})
        port = await start(a)
        idle = [await conn(port, "VER") for _ in range(nutmod.MAX_PER_IP)]
        assert len(a._conns) <= nutmod.PRELOGIN_PER_IP
        # a real upsmon from the same address still gets in and logs in
        r, w, out = await conn(port, "USERNAME monuser", f"PASSWORD {STRONG}", "LOGIN ups")
        assert out == ["OK\n", "OK\n", "OK\n"]
        logged = [c for c in a._conns.values() if c["sess"]["login"]]
        assert len(logged) == 1
        # more pre-login connections evict other pre-login ones, never the logged-in session
        for _ in range(10):
            await conn(port, "VER")
        assert any(c["sess"]["login"] for c in a._conns.values())
        assert a._per_ip["127.0.0.1"] <= nutmod.MAX_PER_IP
        w.write(b"GET VAR ups ups.status\n"); await w.drain()
        assert (await asyncio.wait_for(r.readline(), 2)).startswith(b"VAR ")
        for _, iw, _ in idle:
            iw.close()
        a.server.close()
    asyncio.run(go())


def test_bad_passwords_delay_and_close(monkeypatch):
    monkeypatch.setattr(nutmod, "AUTH_FAIL_DELAY", 0.2)

    async def go():
        a = make({"user": "monuser", "password": STRONG})
        port = await start(a)
        loop = asyncio.get_running_loop()
        r, w, _ = await conn(port, "USERNAME monuser", "PASSWORD wrong")
        t0 = loop.time()
        for _ in range(nutmod.AUTH_FAIL_LIMIT):
            w.write(b"LOGIN ups\n"); await w.drain()
            assert (await asyncio.wait_for(r.readline(), 3)).startswith(b"ERR ACCESS-DENIED")
        assert loop.time() - t0 >= 0.2 * nutmod.AUTH_FAIL_LIMIT - 0.05
        assert await closed(r, 2.0)
        a.server.close()
    asyncio.run(go())
