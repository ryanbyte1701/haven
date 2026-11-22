"""Browser tests: every page of a demo Haven in Chromium, desktop and phone, plus the main controls.

Run with `pytest tests/browser` after `pip install playwright && playwright install chromium`. Skipped when
Playwright isn't installed (the rest of the suite doesn't need a browser); CI installs it and runs these too.
They start a real server (uvicorn) with the demo devices and a throwaway data folder.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from conftest import ADMIN_TOKEN  # noqa: E402

VIEWPORTS = {"desktop": (1440, 1000), "phone": (390, 844), "tiny": (320, 640)}
SETTINGS_PAGES = ["home", "smart", "appearance", "time", "history", "diagnostics", "users", "account"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    data = tmp_path_factory.mktemp("haven-browser")
    (data / "config.yaml").write_text("demo:\n  enabled: true\nhistory:\n  backend: sqlite\n")
    env = {**os.environ, "HAVEN_DATA": str(data), "HAVEN_CONFIG": str(data / "config.yaml"), "PYTHONPATH": str(ROOT)}
    subprocess.run([sys.executable, "-c", "import sys; sys.path[:0]=[sys.argv[1], sys.argv[2]];"
                    "from pathlib import Path; from app import auth; from conftest import add_admin;"
                    "add_admin(auth.AuthStore(Path(sys.argv[3])))", str(ROOT), str(ROOT / "tests"), str(data)],
                   env=env, check=True)
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    time.sleep(1.5)                                      # demo devices come online
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _page(browser, server, size):
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(ADMIN_TOKEN)});")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"page error: {e}"))
    page.on("console", lambda m: m.type == "error" and "favicon" not in m.text and errors.append(f"console: {m.text}"))
    page.goto(server + "/", wait_until="networkidle")
    page.wait_for_function("window.Haven && document.querySelector('#rooms').children.length > 0", timeout=15000)
    return page, errors


def _check_page(page, what: str, errors: list[str], phone: bool):
    page.wait_for_timeout(350)
    text = page.inner_text("#rooms")
    assert text.strip(), f"{what}: empty page"
    for bad in ("undefined", "NaN", "[object Object]"):
        assert bad not in text, f"{what}: shows {bad!r}"
    if phone:
        over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert over <= 2, f"{what}: page is {over}px wider than the phone screen"
    assert not errors, f"{what}: {errors}"


@pytest.mark.parametrize("size", list(VIEWPORTS))
def test_every_page_renders_without_errors(browser, server, size):
    page, errors = _page(browser, server, VIEWPORTS[size])
    try:
        views = ["all", *page.evaluate("TABS.map(t => t[0])"), *page.evaluate("[...PAGES]"), "automations"]
        for v in dict.fromkeys(views):
            page.evaluate("v => Haven.go(v)", v)
            _check_page(page, f"{size} {v}", errors, size != "desktop")
        page.evaluate("Haven.go('settings')")
        for s in SETTINGS_PAGES:
            page.evaluate("Haven.go('settings')")
            page.wait_for_timeout(200)
            page.click(f'[data-open-setting="{s}"]')
            _check_page(page, f"{size} settings/{s}", errors, size != "desktop")
    finally:
        page.context.close()


def test_controls_work(browser, server):
    page, errors = _page(browser, server, VIEWPORTS["desktop"])
    try:
        # a device's panel opens from its card and closes again
        page.evaluate("Haven.go('climate')")
        page.wait_for_timeout(300)
        page.locator("#rooms .card").first.click()
        page.wait_for_selector("dialog[open]", timeout=5000)
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('dialog[open]')", timeout=5000)
        # search narrows a page
        page.evaluate("Haven.go('switches')")
        page.wait_for_timeout(300)
        before = page.locator("#rooms .card").count()
        page.fill("#pagesearch-input", "plug")
        page.wait_for_timeout(400)
        after = page.locator("#rooms .card").count()
        assert 0 < after < before
        page.fill("#pagesearch-input", "")
        # thermostat on Home: two taps, one command, the new setpoint arrives
        page.evaluate("Haven.go('all')")
        page.wait_for_selector(".thermo .tset")
        cur = page.inner_text(".thermo .tset b")
        page.click('.thermo [data-tstep$=":1"]')
        page.click('.thermo [data-tstep$=":1"]')
        page.wait_for_timeout(2500)
        assert page.inner_text(".thermo .tset b") != cur
        # Power page: the trend chart and its table
        page.evaluate("Haven.go('power')")
        page.wait_for_selector(".ptrend-chart svg path", timeout=8000)
        assert page.locator(".ptrend-table tbody tr").count() >= 2
        # live updates keep flowing (demo values change every few seconds)
        page.evaluate("Haven.go('all')")
        first = page.evaluate("JSON.stringify([...devices.values()].map(d => d.updated || d.last_seen))")
        page.wait_for_timeout(7000)
        assert page.evaluate("JSON.stringify([...devices.values()].map(d => d.updated || d.last_seen))") != first
        assert not errors, errors
    finally:
        page.context.close()


def test_live_bursts_patch_cards_and_throttle_full_redraws(browser, server):
    """200 updates in a burst: the changed card shows the newest value at once, the whole page redraws at most
    about once a second."""
    page, errors = _page(browser, server, VIEWPORTS["desktop"])
    try:
        page.evaluate("Haven.go('climate')")
        page.wait_for_timeout(1500)
        res = page.evaluate("""async () => {
            const d0 = [...devices.values()].find(d => typeof val(d, "temperature") === "number"
                                                  && document.querySelector(`#rooms .card[data-id="${CSS.escape(d.id)}"]`));
            let renders = 0; const orig = render; render = () => { renders++; orig(); };
            const t0 = performance.now();
            for (let i = 0; i < 200; i++) {
                const d = structuredClone(d0); d.capabilities.temperature.value = 10 + i / 10;
                devices.set(d.id, d); patchCard(d); scheduleQuiet();
                await new Promise(r => setTimeout(r, 5));
            }
            const shown = document.querySelector(`#rooms .card[data-id="${CSS.escape(d0.id)}"]`).textContent;
            await new Promise(r => setTimeout(r, 1200));
            render = orig;
            return { renders, secs: (performance.now() - t0) / 1000, shown, want: temp(29.9) };
        }""")
        assert res["want"] in res["shown"], res
        assert res["renders"] <= res["secs"] + 2, res          # ~1 per second, not 200
        assert not errors, errors
    finally:
        page.context.close()


def test_history_chart_joins_readings_across_gaps(browser, server):
    """A device that reports now and then: one connected line with each reading marked; broken with the setting off."""
    page, errors = _page(browser, server, VIEWPORTS["desktop"])
    try:
        res = page.evaluate("""() => {
            const d = [...devices.values()].find(x => typeof val(x, "temperature") === "number");
            const t1 = Date.now() / 1000, t0 = t1 - 86400;
            // 6 readings, hours apart, plus a dense burst
            const pts = [0, 4, 9, 15, 20, 23].map(h => ({ t: t0 + h * 3600, v: 18 + h / 4 }))
              .concat(Array.from({ length: 50 }, (_, i) => ({ t: t1 - 3000 + i * 60, v: 24 })));
            const paths = html => { const el = document.createElement("div"); el.innerHTML = html;
              return { points: (el.querySelector("path.line")?.getAttribute("d").match(/[ML]/g) || []).length, dots: el.querySelectorAll("circle.lone").length }; };
            const on = paths(numChart(d, "temperature", pts, t0, t1));
            joinGaps = false; const off = paths(numChart(d, "temperature", pts, t0, t1)); joinGaps = true;
            return { on, off };
        }""")
        assert res["on"]["points"] == 56 and res["off"]["points"] < 56, res   # joined: the line goes through every reading
        assert 6 <= res["on"]["dots"] < 20, res                    # readings next to a gap marked, not the dense burst
        assert not errors, errors
    finally:
        page.context.close()


def test_diagnostics_buttons_fit_a_phone(browser, server):
    """Copy summary, Report on GitHub and Download wrap inside the card instead of running off the screen."""
    page, errors = _page(browser, server, VIEWPORTS["phone"])
    try:
        page.evaluate("Haven.go('settings')")
        page.wait_for_timeout(200)
        page.click('[data-open-setting="diagnostics"]')
        page.wait_for_selector(".diag-actions button")
        boxes = page.evaluate("""() => { const w = innerWidth;
            return [...document.querySelectorAll(".diag-actions button")].map(b => { const r = b.getBoundingClientRect();
              return { text: b.textContent.trim(), left: r.left, right: r.right, w }; }); }""")
        assert len(boxes) == 3 and all(b["text"] and b["left"] >= 0 and b["right"] <= b["w"] for b in boxes), boxes
        assert not errors, errors
    finally:
        page.context.close()


def test_home_tiles_escape_device_values_and_copy_has_a_fallback(browser, server):
    """A device's own text (an alarm state from MQTT) can't inject markup; copying works without the clipboard API."""
    page, errors = _page(browser, server, VIEWPORTS["desktop"])
    try:
        page.evaluate("""() => {
            const evil = "<img src=x onerror=window.__pwn=1>";
            devices.set("t:alarm", { id: "t:alarm", name: "Panel", source: "mqtt", room: "Hall", online: true, hidden: false, attributes: {},
              capabilities: { alarm_panel: { name: "alarm_panel", value: evil, writable: false } } });
            Haven.go("all"); render();
        }""")
        page.wait_for_timeout(600)
        res = page.evaluate("""() => ({ tile: [...document.querySelectorAll(".htile")].some(t => t.textContent.includes("<img")),
            img: !!document.querySelector("#rooms img[src='x']"), pwn: window.__pwn === 1 })""")
        assert res["tile"] and not res["img"] and not res["pwn"], res   # the text shows as text; no element was made from it
        ok = page.evaluate("""async () => { Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
            document.execCommand = () => true; return await copyText("hello"); }""")
        assert ok is True
    finally:
        page.context.close()


def test_pool_lights_water_climate_security_and_switches_pages(browser, server):
    """Pool takes WaterGuru and anything named pool; Lights has a tab per system; Water groups by type; leak sensors
    stay off Climate; smoke alarms sit together on Security; Switches holds plugs and wall switches, not bulbs."""
    page, errors = _page(browser, server, VIEWPORTS["desktop"])
    try:
        page.evaluate("""() => {
            const dev = (id, name, caps, attrs = {}, source = "homeassistant", room = "Hall") => devices.set(id, { id, name, source, room, online: true,
              hidden: false, model: "", attributes: attrs, capabilities: Object.fromEntries(Object.entries(caps).map(([k, v]) =>
                [k, { name: k, value: v, writable: ["light", "switch"].includes(k) }])) });
            dev("t:pooltemp", "Pool Temperature", { temperature: 27 });
            dev("t:whirl", "Whirlpool Washer", { switch: true }, { kind: "smart_plug" });
            dev("t:leak", "Sink Leak", { leak: false, temperature: 20 });
            dev("t:nano", "Shapes", { light: true, brightness: 50 }, { integration: "Nanoleaf" });
            dev("t:hue", "Desk Lamp", { light: false, brightness: 0 }, { integration: "Philips Hue" });
            dev("t:dimmer", "Hall Dimmer Switch", { light: true, brightness: 80 }, { integration: "Lutron Caséta" });
            dev("t:hueplug", "Couch Outlet", { light: true }, { integration: "Philips Hue" });
            dev("t:strip", "Office Strip", { switch_outlet_1: true, switch_outlet_2: false });
            devices.get("t:strip").capabilities.switch_outlet_1.writable = devices.get("t:strip").capabilities.switch_outlet_2.writable = true;
            dev("t:valve", "Tortoise Water Valve", { switch: false }, {}, "homeassistant", "Backyard");
            dev("t:smoke1", "Hall Smoke", { smoke: false }, {}, "homeassistant", "Hall");
            dev("t:smoke2", "Attic Smoke", { smoke: false }, {}, "homeassistant", "Attic");
            render();
        }""")
        r = page.evaluate("""() => ({
            pool: tabsOf(devices.get("t:pooltemp")), wg: [...devices.values()].filter(d => srcLabel(d) === "WaterGuru").map(tabsOf),
            whirl: tabsOf(devices.get("t:whirl")), leakClimate: tabsOf(devices.get("t:leak")).includes("climate"),
            bulb: tabsOf(devices.get("t:hue")).includes("switches"), dimmer: tabsOf(devices.get("t:dimmer")),
            poolNav: !document.querySelector('#nav [data-f="pool"]').hidden, controlsNav: !!document.querySelector('[data-f="controls"]') })""")
        assert r["pool"] == ["pool"] and r["wg"] and all(t == ["pool"] for t in r["wg"]), r
        assert "pool" not in r["whirl"] and not r["leakClimate"] and not r["bulb"], r
        outlets = page.evaluate("() => [tabsOf(devices.get('t:hueplug')), tabsOf(devices.get('t:strip'))]")
        assert all("switches" in t for t in outlets), outlets
        assert page.evaluate("() => tabsOf(devices.get('t:valve'))") == ["water"]
        assert "switches" in r["dimmer"] and "lights" in r["dimmer"] and r["poolNav"] and not r["controlsNav"], r
        page.evaluate("Haven.go('lights')"); page.wait_for_timeout(300)
        tabs = page.locator(".brandtabs [data-lightbrand]").all_inner_texts()
        assert any("Nanoleaf" in t for t in tabs) and any("Philips Hue" in t for t in tabs), tabs
        page.click('.brandtabs [data-lightbrand="Nanoleaf"]'); page.wait_for_timeout(300)
        names = page.locator("#rooms .card .name").all_inner_texts()
        assert names == ["Shapes"], names
        page.click('.brandtabs [data-lightbrand=""]')
        page.evaluate("Haven.go('water')"); page.wait_for_timeout(300)
        assert page.locator('#rooms [data-type="leak"], #rooms .tgroup[data-type="leak"], #rooms h2:has-text("Leak sensors")').count() >= 1
        page.evaluate("Haven.go('security')"); page.wait_for_timeout(300)
        first = page.locator("#rooms > section").first
        assert "Smoke" in first.locator("h2").inner_text() and first.locator(".card").count() >= 2
        assert not errors, errors
    finally:
        page.context.close()


def test_smart_assignment_follows_the_page_rules(browser, server):
    """Settings → Smart assignment suggests Pool for pool devices, takes leak sensors off Climate, bulbs off Switches
    and drops the removed Controls page."""
    page, errors = _page(browser, server, VIEWPORTS["desktop"])
    try:
        sg = page.evaluate("""() => {
            const dev = (id, name, caps, cats) => devices.set(id, { id, name, source: "homeassistant", room: "Hall", online: true, hidden: false,
              model: "", attributes: {}, categories: cats, capabilities: Object.fromEntries(Object.entries(caps).map(([k, v]) =>
                [k, { name: k, value: v, writable: ["light", "switch"].includes(k) }])) });
            dev("t:pool", "Pool Pump", { switch: true }, ["switches"]);
            dev("t:leak", "Sink Leak", { leak: false, temperature: 20 }, ["water", "climate"]);
            dev("t:bulb", "Desk Lamp", { light: true, brightness: 10 }, ["lights", "switches", "controls"]);
            return Object.fromEntries(smartSuggestions().filter(x => x.id.startsWith("t:")).map(x => [x.id, x.body.categories || x.body]));
        }""")
        assert sg["t:pool"] in ({"auto_categories": True}, ["pool"]), sg
        assert sg["t:leak"] == {"auto_categories": True} or sg["t:leak"] == ["water"], sg
        assert sg["t:bulb"] == {"auto_categories": True} or sg["t:bulb"] == ["lights"], sg
        assert not errors, errors
    finally:
        page.context.close()


@pytest.mark.parametrize("size", ["desktop", "phone", "tiny"])
def test_tile_bubble_and_glance_cards(browser, server, size):
    """The new card styles draw every page without errors, fit the screen, and the round icon turns a light on."""
    page, errors = _page(browser, server, VIEWPORTS[size])
    try:
        for style in ("tile", "bubble", "glance"):
            page.evaluate(f"() => {{ cardStyle = '{style}'; Haven.go('lights'); render(); }}")
            page.wait_for_timeout(250)
            cls = {"tile": "dtile", "bubble": "bub", "glance": "glance"}[style]
            assert page.locator(f"#rooms .card.{cls}").count() > 0, style
            wide = page.evaluate("() => [...document.querySelectorAll('#rooms .card')].filter(c => c.getBoundingClientRect().right > innerWidth + 1).length")
            assert wide == 0, (style, wide)
            for p in ("all", "climate", "security", "water", "switches"):
                page.evaluate(f"Haven.go('{p}')"); page.wait_for_timeout(120)
        page.evaluate("() => { cardStyle = 'bubble'; Haven.go('lights'); render(); }"); page.wait_for_timeout(250)
        btn = page.locator("#rooms .card.bub button.bub-ic[data-cmd='light']").first
        before = btn.get_attribute("aria-pressed")
        btn.click(); page.wait_for_timeout(600)
        assert page.locator("#rooms .card.bub button.bub-ic[data-cmd='light']").first.get_attribute("aria-pressed") != before
        assert not errors, errors
    finally:
        page.context.close()


def test_one_device_can_have_its_own_card_style_and_diagnostics_describe_the_dashboard(browser, server):
    page, errors = _page(browser, server, VIEWPORTS["desktop"])
    try:
        page.evaluate("Haven.go('lights')"); page.wait_for_timeout(300)
        id_ = page.locator("#rooms .card").first.get_attribute("data-id")
        page.evaluate(f"""async () => {{ await api('/api/devices/' + encodeURIComponent({json.dumps(id_)}), {{ method: 'PATCH', body: JSON.stringify({{ card_style: 'glance' }}) }}); }}""")
        page.wait_for_timeout(800)
        assert page.locator(f'#rooms .card.glance[data-id="{id_}"]').count() == 1
        assert page.locator("#rooms .card:not(.glance)").count() > 0          # the others keep the dashboard's style
        bad = page.evaluate(f"""async () => {{ try {{ await api('/api/devices/' + encodeURIComponent({json.dumps(id_)}), {{ method: 'PATCH', body: JSON.stringify({{ card_style: 'huge' }}) }}); return 'ok'; }} catch (e) {{ return e.message; }} }}""")
        assert "unknown card style" in bad
        data = page.evaluate("""async () => { const r = await api('/api/diagnostics/link', { method: 'POST', body: JSON.stringify({ dashboard: dashboardInfo() }) });
            return await (await fetch(r.path)).json(); }""")
        dash = data["dashboard"]
        assert dash["pages"]["lights"]["devices"] > 0 and dash["screen"]["width"] == 1440
        assert any(v.get("card_style") == "glance" for v in dash["devices"].values())
        assert id_ not in json.dumps(dash)
        page.evaluate(f"""async () => {{ await api('/api/devices/' + encodeURIComponent({json.dumps(id_)}), {{ method: 'PATCH', body: JSON.stringify({{ card_style: '' }}) }}); }}""")
        assert not [e for e in errors if "400" not in e], errors
    finally:
        page.context.close()
