"""HACS edition generator (tools/hacs/build.py): what ships, the manifest, versions, and that the generated
integration works on its own (imports in isolation; serves the API and live updates through the bridge)."""
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load("haven_hacs_build", ROOT / "tools" / "hacs" / "build.py")
allow = _load("haven_hacs_manifest_t", ROOT / "tools" / "hacs" / "manifest.py")
PKG = "custom_components/haven"
DAY = dt.date(2026, 11, 16)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("haven-hacs")
    changed, version = build.build(ROOT, out, today=DAY)
    assert changed
    return out, version


def _shipped(out: Path) -> set[str]:
    return {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}


def test_native_integrations_stay_in_the_docker_edition(built):
    out, _ = built
    files = _shipped(out)
    adapters = {Path(f).name for f in files if f.startswith(f"{PKG}/core/adapters/")}
    assert adapters == set(allow.HACS_ADAPTERS)
    # Home Assistant's devices, plus three non-brand integrations: UPS (NUT client), forecast, outdoor AQI
    assert adapters == {"__init__.py", "base.py", "homeassistant.py", "ha_rules.py", "hass_local.py", "nut.py", "weather.py", "airquality.py"}
    for brand in ("yolink.py", "ecobee.py", "tapo.py", "hue.py", "mqtt.py", "matter.py", "ecowitt.py", "backup.py", "demo.py",
                  "awair.py", "aqara.py", "ring.py", "xsense.py", "dyson.py", "vesync.py", "roborock.py"):
        assert f"{PKG}/core/adapters/{brand}" not in files
    for og in allow.OG_ONLY_CORE:
        assert f"{PKG}/core/{og}" not in files
    assert f"{PKG}/core/static/ha-splash.js" not in files


def test_core_and_dashboard_are_shipped(built):
    out, _ = built
    files = _shipped(out)
    for p in sorted((ROOT / "app").glob("*.py")):
        if p.name not in allow.OG_ONLY_CORE:
            assert f"{PKG}/core/{p.name}" in files, p.name
            assert (out / PKG / "core" / p.name).read_bytes() == p.read_bytes()      # unchanged copy
    for name in ("js/01-core.js", "js/11-boot.js", "style.css", "index.html", "accounts.js", "automations.js", "fonts/roboto-400.woff2"):
        assert f"{PKG}/core/static/{name}" in files
    for name in ("__init__.py", "config_flow.py", "const.py", "bridge.py", "auth_bridge.py", "views.py",
                 "frontend/panel.js", "frontend/hacs-shim.js", "frontend/hacs.css", "frontend/index.html",
                 "translations/en.json", "brand/icon.png"):
        assert f"{PKG}/{name}" in files, name
    assert {"hacs.json", "README.md"} <= files
    assert not any(f.startswith(".github/") for f in files)          # workflows only with --with-workflows
    assert not any("__pycache__" in f for f in files)


def test_shipped_adapters_load_in_the_hacs_edition():
    """Every shipped integration module is allowed to load (app/edition.py), and nothing else is."""
    from app import edition
    shipped = {Path(n).stem for n in allow.HACS_ADAPTERS if n not in ("__init__.py", "base.py", "homeassistant.py", "ha_rules.py")}
    assert shipped == set(edition.HACS_ADAPTER_MODULES)
    req = build.requirement_specs(ROOT, allow.HACS_REQUIREMENTS)
    assert [r.split("=")[0].split("<")[0].split(">")[0] for r in req] == ["fastapi", "starlette", "httpx"]


def test_new_files_follow_the_rule(tmp_path):
    """A new integration is default-excluded, a new core module default-included."""
    src = tmp_path / "src"
    shutil.copytree(ROOT / "app", src / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "requirements.txt", src / "requirements.txt")
    (src / "tools" / "hacs").mkdir(parents=True)
    shutil.copy(ROOT / "tools" / "hacs" / "manifest.py", src / "tools" / "hacs" / "manifest.py")
    (src / "app" / "adapters" / "zzbrand.py").write_text("ADAPTER = None\n")
    (src / "app" / "newcore.py").write_text("X = 1\n")
    out = tmp_path / "out"
    build.build(src, out, today=DAY)
    files = _shipped(out)
    assert f"{PKG}/core/newcore.py" in files
    assert f"{PKG}/core/adapters/zzbrand.py" not in files


def test_manifest_and_hacs_json(built):
    out, version = built
    m = json.loads((out / PKG / "manifest.json").read_text())
    for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners", "config_flow",
                "iot_class", "integration_type", "requirements", "dependencies"):
        assert key in m, key
    keys = list(m)
    assert keys[:2] == ["domain", "name"] and keys[2:] == sorted(keys[2:])      # hassfest's order
    assert m["domain"] == "haven" and m["version"] == version == "2026.11.16.1"
    assert m["config_flow"] is True and m["single_config_entry"] is True
    assert {"frontend", "http", "panel_custom"} <= set(m["dependencies"])
    assert any(r.startswith("fastapi") for r in m["requirements"])
    assert m["iot_class"] in ("local_push", "local_polling", "calculated")
    h = json.loads((out / "hacs.json").read_text())
    assert h["name"] == "Haven" and h["homeassistant"]
    assert json.loads((out / PKG / "translations" / "en.json").read_text())["config"]["step"]["user"]
    page = (out / PKG / "frontend" / "index.html").read_text()
    assert '"/static/' not in page and "/haven_static/js/01-core.js?v=2026.11.16.1" in page
    assert page.index("hacs-shim.js") < page.index("theme-init.js") < page.index("js/01-core.js")
    assert "generated" in (out / "README.md").read_text().lower()


def test_version_only_moves_when_the_output_changes(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(ROOT / "app", src / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "requirements.txt", src / "requirements.txt")
    (src / "tools" / "hacs").mkdir(parents=True)
    shutil.copy(ROOT / "tools" / "hacs" / "manifest.py", src / "tools" / "hacs" / "manifest.py")
    out = tmp_path / "out"
    assert build.build(src, out, today=DAY) == (True, "2026.11.16.1")
    assert build.build(src, out, today=DAY) == (False, "2026.11.16.1")
    (src / "app" / "adapters" / "yolink.py").write_text("# changed\n")          # Docker edition only
    assert build.build(src, out, today=DAY + dt.timedelta(days=1)) == (False, "2026.11.16.1")
    (src / "app" / "hub.py").write_text((src / "app" / "hub.py").read_text() + "\n# changed\n")
    assert build.build(src, out, today=DAY) == (True, "2026.11.16.2")
    assert build.build(src, out, today=DAY + dt.timedelta(days=1)) == (False, "2026.11.16.2")
    (src / "app" / "hub.py").write_text((src / "app" / "hub.py").read_text() + "\n# again\n")
    assert build.build(src, out, today=DAY + dt.timedelta(days=1)) == (True, "2026.11.17.1")
    assert build.main(["--source", str(src), "--out", str(out), "--check"]) == 0
    # workflow files aren't installed by HACS: writing them doesn't make a new version
    assert build.build(src, out, with_workflows=True, today=DAY + dt.timedelta(days=2)) == (True, "2026.11.17.1")
    assert (out / ".github" / "workflows" / "sync.yml").exists()


E2E = r'''
import asyncio, json, os, sys
sys.path[:0] = [os.environ["PKG_DIR"], os.environ["TESTS_DIR"]]
import hacs_fakes as fk
fk.install()
import core.main as main
from core import auth, edition
from core.adapters import hass_local, ADAPTERS
from bridge import AsgiBridge
from auth_bridge import HassAuthStore
from aiohttp import ClientSession, web

assert edition.HACS and list(ADAPTERS) == ["hass", "airquality", "weather", "nut"]
assert "app" not in sys.modules and not any(m.startswith("app.") for m in sys.modules)
assert not any(m.endswith((".yolink", ".mqtt", ".ecowitt", ".demo", ".backup")) for m in sys.modules)
for _m in ("core.adapters.nut", "core.adapters.weather", "core.adapters.airquality"):
    assert _m in sys.modules, _m

hass = fk.sample_home(fk.FakeHass())
admin = hass.auth.add("r1", fk.User("Ada", is_admin=True))
user = hass.auth.add("r2", fk.User("Bob"))
hass_local.set_hass(hass)
store = HassAuthStore(hass)
auth.store = store
br = AsgiBridge(main.app, blocked=edition.path_blocked, ws_token=store.ws_ticket, on_request=store.prepare_request)


async def run():
    import threading
    hass.loop = asyncio.get_running_loop()          # "Home Assistant's" loop; Haven gets its own thread
    await br.startup()
    assert br.loop is not hass.loop and br._thread.is_alive()
    app = web.Application()
    app.router.add_get("/api/haven/websocket", lambda r: br.handle_ws(r, "/ws"))
    app.router.add_route("*", "/api/haven/{path:.*}", lambda r: br.handle_http(r, "/api/" + r.match_info["path"]))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = "http://127.0.0.1:%d" % site._server.sockets[0].getsockname()[1]
    res = {}
    async with ClientSession() as s:
        async def call(method, path, tok, body=None):
            async with s.request(method, base + path, json=body, headers={"Authorization": "Bearer " + tok}) as r:
                try:
                    return r.status, await r.json()
                except Exception:
                    return r.status, None
        res["me_admin"] = await call("GET", "/api/haven/me", admin)
        res["me_user"] = await call("GET", "/api/haven/me", user)
        res["bad_token"] = (await call("GET", "/api/haven/me", "at-nobody"))[0]
        st, devs = await call("GET", "/api/haven/devices", user)
        res["devices"] = sorted(d["id"] for d in devs)
        res["settings_user"] = (await call("GET", "/api/haven/settings", user))[0]
        st, sett = await call("GET", "/api/haven/settings", admin)
        res["settings_admin"] = [x["id"] for x in sett["sections"]]
        # turn on Haven's own weather forecast (no ZIP yet: it says so instead of failing)
        res["weather_on"] = (await call("PUT", "/api/haven/settings/weather", admin, {"enabled": True, "values": {}}))[0]
        for _ in range(50):
            st, sett = await call("GET", "/api/haven/settings", admin)
            wx = next(x for x in sett["sections"] if x["id"] == "weather")
            if wx.get("status") and wx["status"] != "starting":
                break
            await asyncio.sleep(0.1)
        res["weather_status"] = wx.get("status")
        res["layout_put_user"] = (await call("PUT", "/api/haven/layout", user, {"pinned": []}))[0]
        home = {"weather": False, "tiles": ["leaks", "power"], "hidden_pages": ["water"], "title": "Casa", "start": "climate"}
        st, lay = await call("PUT", "/api/haven/layout", admin, {"home": home})
        st2, lay2 = await call("GET", "/api/haven/layout", admin)
        res["home_put"] = [st, (lay or {}).get("home"), st2, (lay2 or {}).get("home")]
        res["command"] = (await call("POST", "/api/haven/devices/hass:d2/command", user, {"capability": "light", "value": False}))[0]
        res["calls"] = [list(c[:2]) for c in hass.services.calls]
        res["blocked"] = [(await call(m, p, admin))[0] for m, p in [("POST", "/api/haven/auth/login"), ("GET", "/api/haven/users"),
                          ("GET", "/api/haven/ha/status"), ("GET", "/api/haven/ecowitt/gateways"), ("POST", "/api/haven/me/password")]]
        async with s.ws_connect(base + "/api/haven/websocket") as ws:
            await ws.send_json({"type": "auth", "token": admin})
            snap = await ws.receive_json(timeout=10)
            res["ws_snapshot"] = (snap["type"], len(snap["devices"]))
            hass.set_state("binary_sensor.front_door", "on", {"device_class": "door"})   # in HA's loop, like HA
            while True:
                m = await ws.receive_json(timeout=10)
                if m.get("type") == "device" and m["device"]["id"] == "hass:d1":
                    res["ws_update"] = m["device"]["capabilities"]["contact"]["value"]
                    break
        async with s.ws_connect(base + "/api/haven/websocket") as ws:
            await ws.send_json({"type": "auth", "token": "at-nobody"})
            msg = await ws.receive(timeout=10)
            res["ws_bad"] = ws.close_code
    await br.shutdown()
    res["thread_stopped"] = not br._thread.is_alive()
    await runner.cleanup()
    print("RESULT " + json.dumps(res))

asyncio.run(run())
'''


def test_generated_integration_runs_on_its_own(built, tmp_path):
    """Import the shipped core in isolation (no source checkout on the path, HAVEN_EDITION=hacs) and drive its
    FastAPI app through the bridge with a fake Home Assistant: sign-in, roles, devices, commands, blocked paths,
    live updates."""
    pytest.importorskip("aiohttp")
    out, _ = built
    data = tmp_path / "haven"
    data.mkdir()
    (data / "base_config.yaml").write_text("hass:\n  enabled: true\nhistory:\n  enabled: true\n  backend: memory\n"
                                           "time:\n  enabled: false\n  timezone: UTC\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HAVEN_")}
    env.update(HAVEN_EDITION="hacs", HAVEN_DATA=str(data), HAVEN_CONF_DIR=str(data), HAVEN_CONFIG=str(data / "base_config.yaml"),
               PKG_DIR=str(out / PKG), TESTS_DIR=str(TESTS))
    r = subprocess.run([sys.executable, "-I", "-c", E2E], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-3000:]
    res = json.loads(next(l for l in r.stdout.splitlines() if l.startswith("RESULT "))[7:])
    assert res["me_admin"] == [200, {"user": "Ada", "role": "admin", "role_label": "Admin", "guest": False, "must_change": False}]
    assert res["me_user"][1]["role"] == "user"
    assert res["bad_token"] == 401
    assert "hass:d1" in res["devices"] and "hass:d2" in res["devices"]
    assert res["settings_user"] == 403 and res["layout_put_user"] == 403
    hp = res["home_put"]
    assert hp[0] == 200 and hp[2] == 200, hp
    assert hp[3]["title"] == "Casa" and hp[3]["tiles"] == ["leaks", "power"] and hp[3]["hidden_pages"] == ["water"], hp
    assert res["settings_admin"] == ["hass", "nut", "airquality", "weather", "history"]
    assert res["weather_on"] == 200 and "ZIP" in (res["weather_status"] or ""), res
    assert res["command"] == 200 and res["calls"] == [["light", "turn_off"]]
    assert res["blocked"] == [404, 404, 404, 404, 404]
    assert res["ws_snapshot"][0] == "snapshot" and res["ws_snapshot"][1] >= 3
    assert res["ws_update"] == "open"
    assert res["ws_bad"] == 4401
    assert res["thread_stopped"]


def test_changelog_keeps_only_entries_for_the_ha_edition(tmp_path):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("hacs_build", pathlib.Path(__file__).parents[1] / "tools/hacs/build.py")
    b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\nintro\n\n## 2026-12-01\n\n### Both editions\n- shared fix\n\n### Haven OG only\n- ecobee thing\n\n"
        "### Home Assistant edition only\n- ha thing\n\n## 2026-11-30\n\n### Haven OG only\n- only og\n")
    log = b.hacs_changelog(tmp_path)
    assert "shared fix" in log and "ha thing" in log and "ecobee" not in log
    assert "2026-11-30" not in log            # nothing left for the HA edition that day
    notes = b.latest_notes(tmp_path)
    assert notes.startswith("## 2026-12-01") and "only og" not in notes
