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
        page.evaluate("Haven.go('controls')")
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
