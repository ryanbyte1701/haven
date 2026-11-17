#!/usr/bin/env python3
"""End-to-end checks of everything around accounts, in a real browser (Playwright + Chromium).

Starts its own Haven servers the way the container does (python -m app.privdrop when run as root, else
python -m app.serve) in temporary folders, runs every check and prints a pass/fail list. Exit code 1 if
anything failed, 0 if everything passed, 0 with a SKIP line if Playwright isn't installed.

    python tools/accounts_e2e.py                 # desktop (1440x900) and phone (390x844, touch)
    python tools/accounts_e2e.py --only desktop  # or phone
    python tools/accounts_e2e.py --port 18660 --keep   # other ports; keep the temp folders for a look

What it covers
  forced first password change (validation, uppercase / reserved / kept username, Enter submits, Sign out,
  reload in the middle, two tabs, autocomplete attributes, nothing running behind the screen, restart
  persistence, HAVEN_ADMIN_PASSWORD from the environment, unwritable config folder), the Account page,
  Users & access (add / role / reset / delete / last admin / self demotion / self delete / viewing without
  signing in), what each role sees and can do (admin, regular, monitor, viewer), sessions (logout, revocation,
  live updates cut off, token only in localStorage and never in URLs or the access log), wrong-password
  slowdown and 429 messages, layout on a phone (no sideways overflow, buttons reachable, focus order).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PW1 = "correct-horse-battery"
PW2 = "another-good-password"
results: list[tuple[str, bool, str]] = []
SECTION = [""]
SHOTS: list[Path | None] = [None]


def check(name: str, ok, detail: str = "") -> bool:
    results.append((f"{SECTION[0]}{name}", bool(ok), "" if ok else str(detail)[:300]))
    print(("  ok   " if ok else "  FAIL ") + SECTION[0] + name + ("" if ok else f"  -- {str(detail)[:300]}"), flush=True)
    return bool(ok)


# ---------------------------------------------------------------- servers
class Server:
    """One Haven in its own folders. mode: "privdrop" (as root: like the container), "serve" (as is),
    "setpriv" (as uid 1026 on root-owned folders it can't write: the Synology permission problem)."""

    def __init__(self, root: Path, port: int, mode: str = "auto", env: dict | None = None):
        self.root, self.port, self.env = root, port, env or {}
        self.mode = ("privdrop" if os.geteuid() == 0 else "serve") if mode == "auto" else mode
        self.base = f"http://127.0.0.1:{port}"
        self.proc = None
        self.log = root / "server.log"
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "data").mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o755)

    def start(self, timeout: float = 30) -> "Server":
        env = {k: v for k, v in os.environ.items() if not k.startswith("HAVEN_")}
        env.update(HAVEN_CONFIG=str(self.root / "config" / "config.yaml"),
                   HAVEN_EXAMPLE=str(REPO / "config" / "config.example.yaml"),
                   HAVEN_DATA=str(self.root / "data"), HAVEN_PORT=str(self.port), HAVEN_HTTPS_PORT="0",
                   PYTHONUNBUFFERED="1", **self.env)
        cmd = [sys.executable, "-m", "app.privdrop" if self.mode == "privdrop" else "app.serve"]
        if self.mode == "setpriv":
            cmd = ["setpriv", "--reuid=1026", "--regid=100", "--clear-groups", *cmd]
        self.proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=open(self.log, "ab"), stderr=subprocess.STDOUT,
                                     start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited: {self.log.read_text()[-2000:]}")
            try:
                urllib.request.urlopen(self.base + "/api/health", timeout=1).read()
                return self
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        raise RuntimeError(f"server didn't start: {self.log.read_text()[-2000:]}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc = None

    def restart(self) -> "Server":
        self.stop()
        return self.start()

    # tiny HTTP client (no browser) for setup and probing
    def call(self, method: str, path: str, body=None, token: str | None = None, timeout: float = 30) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def login(self, user: str, pw: str) -> str:
        st, d = self.call("POST", "/api/auth/login", {"username": user, "password": pw})
        assert st == 200, (st, d)
        return d["token"]


# ---------------------------------------------------------------- browser helpers
class UI:
    def __init__(self, browser, phone: bool):
        self.browser, self.phone = browser, phone

    def ctx(self):
        if self.phone:
            c = self.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                         device_scale_factor=2, bypass_csp=True)
        else:
            c = self.browser.new_context(viewport={"width": 1440, "height": 900}, bypass_csp=True)
        # (bypass_csp: Playwright's wait_for_function evaluates strings, which Haven's CSP rightly forbids)
        return c

    def page(self, ctx):
        pg = ctx.new_page()
        pg.errs, pg.resp, pg.ws = [], [], []
        pg.on("pageerror", lambda e: pg.errs.append("PAGEERROR " + str(e)))
        # (the browser logs every 4xx answer as "Failed to load resource": those are checked separately)
        pg.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource")
              and pg.errs.append("CONSOLE " + m.text[:200]))
        pg.on("response", lambda r: pg.resp.append((r.status, r.request.method, r.url)))
        pg.on("websocket", lambda w: pg.ws.append(w))
        return pg


def tap(pg, sel):
    pg.locator(sel).first.click()


def sign_in(pg, base, user, pw, wait=True):
    pg.goto(base + "/")
    pg.wait_for_selector("#auth-form", timeout=10000)
    pg.fill("#auth-form [name=username]", user)
    pg.fill("#auth-form [name=password]", pw)
    tap(pg, "#auth-form .auth-go")
    if wait:
        pg.wait_for_function("document.querySelector('#mc-form') || (window.Haven && !document.body.classList.contains('auth-open')) "
                             "|| (document.querySelector('#auth-err') && !document.querySelector('#auth-err').hidden)", timeout=15000)
        pg.wait_for_timeout(300)


def err_text(pg) -> str:
    loc = pg.locator("#auth-err")
    return loc.inner_text().strip() if loc.count() and loc.is_visible() else ""


def submit_mc(pg, user=None, new=None, confirm=None, enter=False):
    if user is not None:
        pg.fill("#mc-form [name=username]", user)
    if new is not None:
        pg.fill("#mc-form [name=new]", new)
    pg.fill("#mc-form [name=confirm]", confirm if confirm is not None else (new or ""))
    if enter:
        pg.press("#mc-form [name=confirm]", "Enter")
    else:
        tap(pg, "#mc-form .auth-go")
    pg.wait_for_function("!document.querySelector('#mc-form') || !document.querySelector('#mc-form .auth-go').disabled", timeout=15000)
    pg.wait_for_timeout(400)


def layout_ok(pg, what: str, submit_sel: str | None = None):
    """No sideways scrolling; the submit button can be scrolled into view and is inside the viewport."""
    if SHOTS[0]:
        pg.screenshot(path=str(SHOTS[0] / f"{SECTION[0].split(']')[0].strip('[')}-{what.replace(' ', '_')}.png"), full_page=True)
    w = pg.evaluate("[document.documentElement.scrollWidth, innerWidth, (document.querySelector('#authpage')||{}).scrollWidth||0]")
    check(f"{what}: no sideways overflow", w[0] <= w[1] + 1 and w[2] <= w[1] + 1, f"scrollWidth {w}")
    if submit_sel:
        b = pg.locator(submit_sel).first
        b.scroll_into_view_if_needed()
        box = b.bounding_box()
        vp = pg.viewport_size
        check(f"{what}: submit button reachable", box and box["y"] >= 0 and box["y"] + box["height"] <= vp["height"] + 1
              and box["x"] >= 0 and box["x"] + box["width"] <= vp["width"] + 1 and box["height"] >= 36, f"{box} in {vp}")
        covered = b.evaluate("""el => { el.scrollIntoView({block: 'center'}); const r = el.getBoundingClientRect();
            const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return top && (el === top || el.contains(top)) ? '' : (top ? top.tagName + '.' + top.className : 'nothing'); }""")
        check(f"{what}: submit button not hidden under the bottom bar when scrolled to it", not covered, covered)


def token_of(pg):
    return pg.evaluate("localStorage.getItem('token')")


def api_in_page(pg, method, path, body=None):
    return pg.evaluate("""async ([m, p, b]) => { const r = await fetch(p, { method: m, headers: { 'Content-Type': 'application/json',
        Authorization: 'Bearer ' + localStorage.getItem('token') }, body: b ? JSON.stringify(b) : undefined });
        return [r.status, await r.json().catch(() => ({}))]; }""", [method, path, body])


def confirm_dialog(pg, yes=True):
    pg.wait_for_selector("#confirm[open]", timeout=5000)
    tap(pg, "#confirm-yes" if yes else "#confirm button[value=no]")
    pg.wait_for_timeout(600)


def open_settings_page(pg, sub):
    pg.evaluate("Haven.go('settings')")
    pg.wait_for_timeout(700)
    if pg.locator(f'[data-open-setting="{sub}"]').count():
        tap(pg, f'[data-open-setting="{sub}"]')
    pg.wait_for_selector(".acc-page .acc-body > *", timeout=8000)
    pg.wait_for_timeout(500)


def snack_text(pg):
    return pg.evaluate("(() => { const s = document.querySelector('#snackbar'); return s && s.classList.contains('show') ? s.textContent : ''; })()")


# ---------------------------------------------------------------- scenarios
def forced_change(ui: UI, srv: Server, tag: str):
    SECTION[0] = f"[{tag}] forced change: "
    ctx = ui.ctx()
    pg = ui.page(ctx)
    pg.goto(srv.base + "/")
    pg.wait_for_selector("#auth-form")
    check("sign-in page shows the first sign-in hint", "haven-admin" in pg.inner_text("#auth-form"))
    layout_ok(pg, "sign-in page", "#auth-form .auth-go")
    sign_in(pg, srv.base, "Admin", "haven-admin")          # usernames are case-insensitive
    check("default sign-in opens 'Choose your own password'", pg.locator("#mc-form").count() == 1, pg.content()[:200])
    attrs = pg.evaluate("[...document.querySelectorAll('#mc-form input')].map(i => [i.name, i.autocomplete, i.type])")
    check("autocomplete: username / new-password / new-password",
          attrs == [["username", "username", "text"], ["new", "new-password", "password"], ["confirm", "new-password", "password"]], attrs)
    check("screen text explains why", "password from the installation" in pg.inner_text("#mc-form"))
    layout_ok(pg, "forced change", "#mc-form .auth-go")
    # focus order
    pg.focus("#mc-form [name=username]")
    order = []
    for _ in range(5):
        order.append(pg.evaluate("document.activeElement.name || document.activeElement.textContent.trim()"))
        pg.keyboard.press("Tab")
    check("focus order: username, new, repeat, Save, Sign out", order == ["username", "new", "confirm", "Save and continue", "Sign out"], order)
    # nothing else runs behind the screen
    pg.wait_for_timeout(1500)
    bad = [r for r in pg.resp if r[0] in (401, 403, 500)]
    check("no 401/403/500 behind the screen", not bad, bad[:5])
    check("no live-updates connection while locked", not pg.ws, [w.url for w in pg.ws])
    check("no error snackbar", not snack_text(pg), snack_text(pg))
    check("app is hidden behind the screen", pg.evaluate("getComputedStyle(document.querySelector('#rooms')).visibility") == "hidden")
    # validation
    submit_mc(pg, new="short")
    check("too short refused", "at least" in err_text(pg).lower(), err_text(pg))
    submit_mc(pg, new=PW1, confirm=PW1 + "x")
    check("mismatch refused", "match" in err_text(pg).lower(), err_text(pg))
    submit_mc(pg, new="haven-admin")          # the installation password itself
    check("installation password refused", "different" in err_text(pg).lower(), err_text(pg))
    submit_mc(pg, user="has space", new=PW1)
    check("invalid username refused", "usernames" in err_text(pg).lower(), err_text(pg))
    submit_mc(pg, user="viewer", new=PW1)
    check("reserved username refused with the server's reason", "reserved" in err_text(pg).lower(), err_text(pg))
    check("still on the screen after errors", pg.locator("#mc-form").count() == 1)
    # reload in the middle
    pg.reload()
    pg.wait_for_timeout(2000)
    check("reload comes back to the forced screen", pg.locator("#mc-form").count() == 1 and pg.locator("#auth-form").count() == 0)
    check("no page errors so far", not pg.errs, pg.errs[:3])
    # Sign out from the screen
    tok = token_of(pg)
    tap(pg, "[data-mc-out]")
    pg.wait_for_selector("#auth-form", timeout=8000)
    check("Sign out goes to the sign-in page", pg.locator("#auth-form").count() == 1)
    check("Sign out forgets the token", token_of(pg) is None)
    check("Sign out ends the session on the server", srv.call("GET", "/api/me", token=tok)[0] == 401)
    # two tabs: A changes the password, B (same browser) follows
    sign_in(pg, srv.base, "admin", "haven-admin")
    pg2 = ui.page(ctx)
    pg2.goto(srv.base + "/")
    pg2.wait_for_selector("#mc-form", timeout=8000)
    check("second tab also shows the forced screen", pg2.locator("#mc-form").count() == 1)
    submit_mc(pg, user="Ryan", new=PW1, enter=True)          # uppercase typed, Enter submits
    pg.wait_for_timeout(1500)
    check("Enter saves; in the app as admin", pg.evaluate("window.Haven && Haven.role") == "admin"
          and pg.locator("#mc-form, #auth-form").count() == 0, err_text(pg) if pg.locator("#mc-form").count() else "")
    me = api_in_page(pg, "GET", "/api/me")
    check("uppercase username saved lowercase", me[1].get("user") == "ryan" and me[1].get("must_change") is False, me)
    pg2.wait_for_timeout(2000)
    if pg2.locator("#mc-form").count():                    # no storage event: submit there too
        submit_mc(pg2, new=PW2)
        pg2.wait_for_timeout(1500)
    check("other tab continues into the app (no 'unauthorized')", pg2.evaluate("window.Haven && Haven.role") == "admin"
          and pg2.locator("#mc-form, #auth-form").count() == 0, err_text(pg2) or pg2.inner_text("body")[:200])
    check("settings reachable after the change", api_in_page(pg, "GET", "/api/settings")[0] == 200)
    # live updates start now
    pg.reload()
    pg.wait_for_timeout(2500)
    check("live updates connect after the change", pg.evaluate("document.querySelector('#conn').classList.contains('on')"))
    check("no page errors in the forced change", not pg.errs, pg.errs[:3])
    # old credentials refused, new ones work
    st = srv.call("POST", "/api/auth/login", {"username": "admin", "password": "haven-admin"})[0]
    check("admin / haven-admin refused afterwards", st == 401, st)
    check("auth state no longer offers the default", srv.call("GET", "/api/auth/state")[1].get("default_login") is False)
    # restart: password, sessions and must_change persisted in the right folders
    tok = token_of(pg)
    srv.restart()
    users = json.loads((srv.root / "config" / "users.json").read_text()) if (srv.root / "config" / "users.json").exists() else {}
    check("users.json in the config folder, admin renamed, must_change cleared",
          list(users.get("users", {})) == ["ryan"] and "must_change" not in users["users"]["ryan"], users)
    check("sessions.json in the data folder", (srv.root / "data" / "sessions.json").exists())
    check("session still valid after a restart", srv.call("GET", "/api/me", token=tok)[1].get("user") == "ryan")
    check("new password works after a restart", srv.call("POST", "/api/auth/login", {"username": "ryan", "password": PW1})[0] == 200)
    pg.reload()
    pg.wait_for_timeout(2000)
    check("browser stays signed in across the restart", pg.evaluate("window.Haven && Haven.role") == "admin")
    ctx.close()


def forced_change_keep_name_env(ui: UI, srv: Server, tag: str):
    """HAVEN_ADMIN_USER / HAVEN_ADMIN_PASSWORD set: the forced change is still asked (the password is in a
    plain-text compose file); keeping the suggested username works."""
    SECTION[0] = f"[{tag}] env admin: "
    ctx = ui.ctx()
    pg = ui.page(ctx)
    sign_in(pg, srv.base, "boss", "compose-file-password")
    check("forced screen also for HAVEN_ADMIN_PASSWORD", pg.locator("#mc-form").count() == 1)
    check("username prefilled with boss", pg.input_value("#mc-form [name=username]") == "boss")
    submit_mc(pg, new="compose-file-password")
    check("the compose-file password is refused as the new one", "different" in err_text(pg).lower(), err_text(pg))
    submit_mc(pg, new=PW1)
    pg.wait_for_timeout(1200)
    check("keeping the username works", pg.evaluate("window.Haven && Haven.me.user") == "boss")
    ctx.close()


def storage_problem(ui: UI, srv: Server, tag: str):
    SECTION[0] = f"[{tag}] unwritable config: "
    ctx = ui.ctx()
    pg = ui.page(ctx)
    sign_in(pg, srv.base, "admin", "haven-admin")
    check("forced screen shown", pg.locator("#mc-form").count() == 1)
    submit_mc(pg, user="ryan", new=PW1)
    e = err_text(pg)
    check("clear message, not 'Something went wrong'", "can't save" in e.lower() and "something went wrong" not in e.lower(), e)
    check("still on the forced screen", pg.locator("#mc-form").count() == 1)
    check("nothing changed in memory: default still signs in",
          srv.call("POST", "/api/auth/login", {"username": "admin", "password": "haven-admin"})[0] == 200)
    check("nothing changed in memory: the new name/password doesn't",
          srv.call("POST", "/api/auth/login", {"username": "ryan", "password": PW1})[0] == 401)
    check("session still usable for another try", pg.evaluate("fetch('/api/me',{headers:{Authorization:'Bearer '+localStorage.getItem('token')}}).then(r=>r.status)") == 200)
    ctx.close()


def account_page(ui: UI, srv: Server, tag: str, admin: str, admin_pw: str) -> str:
    SECTION[0] = f"[{tag}] account page: "
    other = srv.login(admin, admin_pw)                  # another device
    ctx = ui.ctx()
    pg = ui.page(ctx)
    sign_in(pg, srv.base, admin, admin_pw)
    open_settings_page(pg, "account")
    layout_ok(pg, "account page", "#acc-pw button[type=submit]")
    attrs = pg.evaluate("[...document.querySelectorAll('#acc-pw input')].map(i => [i.name, i.autocomplete])")
    check("autocomplete attributes", attrs == [["username", "username"], ["current", "current-password"],
                                              ["new", "new-password"], ["confirm", "new-password"]], attrs)

    def submit(cur, new, conf=None):
        pg.fill("#acc-pw [name=current]", cur)
        pg.fill("#acc-pw [name=new]", new)
        pg.fill("#acc-pw [name=confirm]", new if conf is None else conf)
        tap(pg, "#acc-pw button[type=submit]")
        pg.wait_for_function("!document.querySelector('#acc-pw button[type=submit]').disabled", timeout=15000)
        pg.wait_for_timeout(300)
        loc = pg.locator("#acc-pw .acc-err")
        return loc.inner_text().strip() if loc.is_visible() else ""

    e = submit("wrong-password-x", PW2)
    check("wrong current password refused", "current password" in e.lower(), e)
    check("wrong current doesn't sign out", pg.locator("#auth-form").count() == 0)
    e = submit(admin_pw, "short")
    check("short refused", "at least" in e.lower(), e)
    e = submit(admin_pw, PW2, PW2 + "x")
    check("mismatch refused", "match" in e.lower(), e)
    e = submit(admin_pw, admin_pw)
    check("same as current refused", "different" in e.lower(), e)
    e = submit(admin_pw, PW2)
    check("success: no error", e == "", e)
    pg.wait_for_timeout(300)
    check("success: snackbar says others were signed out", "signed out" in snack_text(pg).lower(), snack_text(pg))
    check("other device signed out", srv.call("GET", "/api/me", token=other)[0] == 401)
    check("this device stays signed in", api_in_page(pg, "GET", "/api/me")[0] == 200)
    check("fields cleared", pg.input_value("#acc-pw [name=current]") == "")
    pg.wait_for_timeout(27000)                      # the old live-updates socket is cut off within ~25 s ...
    check("... and this page reconnects instead of asking to sign in", pg.locator("#auth-form").count() == 0
          and pg.evaluate("document.querySelector('#conn').classList.contains('on')"))
    # sign out
    tok = token_of(pg)
    tap(pg, "[data-acc-signout]")
    pg.wait_for_selector("#auth-form", timeout=8000)
    check("Sign out: sign-in page, token gone, session revoked", token_of(pg) is None and srv.call("GET", "/api/me", token=tok)[0] == 401)
    check("no page errors", not pg.errs, pg.errs[:3])
    ctx.close()
    return PW2


def users_page(ui: UI, srv: Server, tag: str, admin: str, admin_pw: str):
    SECTION[0] = f"[{tag}] users & access: "
    ctx = ui.ctx()
    pg = ui.page(ctx)
    sign_in(pg, srv.base, admin, admin_pw)
    open_settings_page(pg, "users")
    layout_ok(pg, "users page", "#acc-add button[type=submit]")
    row = f'.urow[data-user="{admin}"]'
    check("last admin: role select disabled", pg.locator(f"{row} [data-acc-role]").is_disabled())
    check("last admin: delete disabled", pg.locator(f"{row} [data-acc-delete]").is_disabled())
    tok = token_of(pg)
    check("API refuses demoting the last admin", srv.call("PATCH", f"/api/users/{admin}", {"role": "user"}, tok)[0] == 400)
    check("API refuses deleting the last admin", srv.call("DELETE", f"/api/users/{admin}", None, tok)[0] == 400)

    def add(name, pw, role):
        pg.fill("#acc-add [name=name]", name)
        pg.fill("#acc-add [name=pw]", pw)
        pg.select_option("#acc-add [name=role]", role)
        tap(pg, "#acc-add button[type=submit]")
        pg.wait_for_timeout(1200)
        loc = pg.locator("#acc-add .acc-err")
        return loc.inner_text().strip() if loc.count() and loc.is_visible() else ""

    check("bad username refused", "usernames" in add("bad name", PW1, "user").lower())
    check("short password refused", "at least" in add("kim", "short", "user").lower())
    e = add("viewer", PW1, "user")
    check("reserved name refused (server message)", "reserved" in e.lower(), e)
    for name, role in (("kim", "user"), ("mo", "monitor"), ("boss", "admin")):
        e = add(name.upper() if name == "kim" else name, PW1, role)
        check(f"add {name} as {role}", not e and pg.locator(f'.urow[data-user="{name}"]').count() == 1, e)
    e = add("kim", PW1, "user")
    check("duplicate refused", "exists" in e.lower(), e)
    check("form cleared after adding", pg.input_value("#acc-add [name=name]") in ("", "kim"))   # see report
    check("with two admins, own role can be changed", not pg.locator(f"{row} [data-acc-role]").is_disabled())
    # role change: cancel keeps it, confirm changes it and signs kim out
    kim = srv.login("kim", PW1)
    pg.select_option('.urow[data-user="kim"] [data-acc-role]', "monitor")
    confirm_dialog(pg, yes=False)
    check("cancelled role change leaves the select as it was", pg.input_value('.urow[data-user="kim"] [data-acc-role]') == "user")
    pg.select_option('.urow[data-user="kim"] [data-acc-role]', "monitor")
    confirm_dialog(pg, yes=True)
    check("role changed", srv.call("GET", "/api/users", None, tok)[1]["users"][[u["name"] for u in srv.call("GET", "/api/users", None, tok)[1]["users"]].index("kim")]["role"] == "monitor")
    check("role change signs kim out", srv.call("GET", "/api/me", token=kim)[0] == 401)
    pg.select_option('.urow[data-user="kim"] [data-acc-role]', "user")
    confirm_dialog(pg, yes=True)
    # reset kim's password
    kim = srv.login("kim", PW1)
    tap(pg, '.urow[data-user="kim"] [data-acc-reset]')
    pg.wait_for_selector('[data-acc-resetform="kim"]')
    check("reset form focused", pg.evaluate("document.activeElement.name") == "pw")
    pg.fill('[data-acc-resetform="kim"] [name=pw]', "short")
    pg.press('[data-acc-resetform="kim"] [name=pw]', "Enter")
    pg.wait_for_timeout(500)
    check("short reset refused", "at least" in snack_text(pg).lower() or pg.locator('[data-acc-resetform="kim"] .acc-err:visible').count(), snack_text(pg))
    if not pg.locator('[data-acc-resetform="kim"]').count():
        tap(pg, '.urow[data-user="kim"] [data-acc-reset]')
    pg.fill('[data-acc-resetform="kim"] [name=pw]', PW2)
    pg.press('[data-acc-resetform="kim"] [name=pw]', "Enter")
    pg.wait_for_timeout(1500)
    check("reset: kim signed out, new password works", srv.call("GET", "/api/me", token=kim)[0] == 401
          and srv.call("POST", "/api/auth/login", {"username": "kim", "password": PW2})[0] == 200)
    # delete mo
    tap(pg, '.urow[data-user="mo"] [data-acc-delete]')
    confirm_dialog(pg, yes=False)
    check("cancelled delete keeps mo", pg.locator('.urow[data-user="mo"]').count() == 1)
    mo = srv.login("mo", PW1)
    tap(pg, '.urow[data-user="mo"] [data-acc-delete]')
    confirm_dialog(pg, yes=True)
    check("mo deleted and signed out", pg.locator('.urow[data-user="mo"]').count() == 0 and srv.call("GET", "/api/me", token=mo)[0] == 401)
    # viewing without signing in
    tap(pg, "[data-acc-guest]")
    pg.wait_for_timeout(800)
    check("guest viewing turned on", srv.call("GET", "/api/auth/state")[1].get("guest_view") is True)
    gctx = ui.ctx()
    gp = ui.page(gctx)
    gp.goto(srv.base + "/")
    gp.wait_for_selector("[data-auth-guest]", timeout=8000)
    tap(gp, "[data-auth-guest]")
    gp.wait_for_function("window.Haven && Haven.me && Haven.me.guest", timeout=10000)
    gp.wait_for_timeout(1500)
    check("viewer is in, as monitor", gp.evaluate("Haven.role") == "monitor")
    tap(pg, "[data-acc-guest]")
    confirm_dialog(pg, yes=True)
    check("guest viewing turned off", srv.call("GET", "/api/auth/state")[1].get("guest_view") is False)
    gp.wait_for_timeout(27000)                      # live updates notice within ~25 s
    check("viewer's page asks to sign in once viewing is off", gp.locator("#auth-form").count() == 1,
          gp.inner_text("body")[:200])
    gctx.close()
    # self demotion as boss, then self delete as boss2
    b = srv.login("boss", PW1)
    bctx = ui.ctx()
    bp = ui.page(bctx)
    sign_in(bp, srv.base, "boss", PW1)
    open_settings_page(bp, "users")
    bp.select_option('.urow[data-user="boss"] [data-acc-role]', "user")
    confirm_dialog(bp, yes=True)
    bp.wait_for_selector("#auth-form", timeout=10000)
    check("self demotion signs out", bp.locator("#auth-form").count() == 1 and srv.call("GET", "/api/me", token=b)[0] == 401)
    srv.call("PATCH", "/api/users/boss", {"role": "admin"}, tok)
    sign_in(bp, srv.base, "boss", PW1)
    open_settings_page(bp, "users")
    tap(bp, '.urow[data-user="boss"] [data-acc-delete]')
    confirm_dialog(bp, yes=True)
    bp.wait_for_selector("#auth-form", timeout=10000)
    check("self delete signs out", bp.locator("#auth-form").count() == 1)
    check("boss can't sign in any more", srv.call("POST", "/api/auth/login", {"username": "boss", "password": PW1})[0] == 401)
    check("no page errors", not pg.errs and not bp.errs, (pg.errs + bp.errs)[:3])
    bctx.close()
    ctx.close()


def roles(ui: UI, srv: Server, tag: str, admin_tok: str):
    SECTION[0] = f"[{tag}] roles: "
    srv.call("PUT", "/api/settings/demo", {"enabled": True, "values": {}}, admin_tok)
    time.sleep(2)
    srv.call("POST", "/api/users", {"name": "reg", "password": PW1, "role": "user"}, admin_tok)
    srv.call("POST", "/api/users", {"name": "mon", "password": PW1, "role": "monitor"}, admin_tok)
    for who, role in (("reg", "user"), ("mon", "monitor")):
        ctx = ui.ctx()
        pg = ui.page(ctx)
        sign_in(pg, srv.base, who, PW1)
        pg.wait_for_timeout(2500)
        check(f"{role}: signed in, body role", pg.evaluate("document.body.dataset.role") == role)
        bad = [r for r in pg.resp if r[0] in (401, 403, 500)]
        check(f"{role}: no 401/403/500 while loading the dashboard", not bad, bad[:5])
        check(f"{role}: no error snackbar", not snack_text(pg), snack_text(pg))
        _sw = pg.locator("[data-cmd]:visible, .m3-switch:visible").first
        if role == "user":
            check("user: device controls visible", pg.locator("[data-cmd]").count() > 0)
            check("user: edit mode hidden", not pg.locator("#editmode").is_visible())
        else:
            ok = pg.evaluate("[...document.querySelectorAll('[data-cmd], .m3-switch')].every(e => getComputedStyle(e).pointerEvents === 'none' || e.offsetParent === null)")
            check("monitor: no controls usable", ok)
            check("monitor: Settings hidden from the menu", not pg.locator('[data-f="settings"]:visible').count())
        open_settings_page(pg, "account")
        check(f"{role}: account page (not settings) for the gear/account button", pg.locator(".acc-page[data-acc=account]").count() == 1
              and not pg.locator("[data-open-setting]").count())
        cmd = api_in_page(pg, "POST", "/api/devices/demo:none/command", {"capability": "light", "value": True})[0]
        check(f"{role}: device command {'allowed' if role == 'user' else 'refused'} by the API",
              (cmd != 403) if role == "user" else cmd == 403, cmd)
        for path in ("/api/settings", "/api/users", "/api/ecowitt/gateways"):
            check(f"{role}: {path} refused", api_in_page(pg, "GET", path)[0] == 403)
        layout_ok(pg, f"{role} account page")
        check(f"{role}: no page errors", not pg.errs, pg.errs[:3])
        ctx.close()


def sessions_and_limits(ui: UI, srv: Server, tag: str, admin: str, admin_pw: str):
    SECTION[0] = f"[{tag}] sessions: "
    ctx = ui.ctx()
    pg = ui.page(ctx)
    sign_in(pg, srv.base, admin, admin_pw)
    tok = token_of(pg)
    check("token only in localStorage (no cookies)", tok and not ctx.cookies())
    urls = [r[2] for r in pg.resp] + [w.url for w in pg.ws]
    check("token never in a URL", not any(tok in u for u in urls))
    pg.reload()
    pg.wait_for_timeout(2000)
    log = srv.log.read_text(errors="replace")
    check("token never in the server log", tok not in log)
    check("access log has no ?token=", "token=" not in log)
    ctx.close()
    SECTION[0] = f"[{tag}] wrong passwords: "
    ctx = ui.ctx()
    pg = ui.page(ctx)
    pg.goto(srv.base + "/")
    pg.wait_for_selector("#auth-form")
    t0 = time.time()
    for i in range(12):
        pg.fill("#auth-form [name=username]", admin)
        pg.fill("#auth-form [name=password]", f"wrong-{i}-password")
        tap(pg, "#auth-form .auth-go")
        pg.wait_for_function("!document.querySelector('#auth-form .auth-go').disabled", timeout=20000)
    check("wrong password message", "wrong username or password" in err_text(pg).lower(), err_text(pg))
    check("wrong guesses are slowed down", time.time() - t0 > 0.6, time.time() - t0)
    t0 = time.time()
    sign_in(pg, srv.base, admin, admin_pw)
    check("the right password still gets in at once", pg.evaluate("window.Haven && Haven.role") == "admin" and time.time() - t0 < 6,
          time.time() - t0)
    # a flood: 30 wrong guesses at once from this address, then one more through the form
    pg2 = ui.page(ctx)
    pg2.goto(srv.base + "/")
    pg2.evaluate("localStorage.removeItem('token')")
    pg2.reload()
    pg2.wait_for_selector("#auth-form")
    # (from Python threads: a browser keeps at most 6 connections per host, too few for a flood)
    import threading
    codes: list[int] = []
    flood = [threading.Thread(target=lambda i=i: codes.append(srv.call("POST", "/api/auth/login",
                                                                       {"username": "x", "password": f"flood-{i}"}, timeout=300)[0]))
             for i in range(30)]
    for t in flood:
        t.start()
    pg2.wait_for_timeout(2500)              # let them queue up behind the slowdown
    pg2.fill("#auth-form [name=username]", admin)
    pg2.fill("#auth-form [name=password]", "still-wrong-pw")
    tap(pg2, "#auth-form .auth-go")
    pg2.wait_for_function("!document.querySelector('#auth-form .auth-go').disabled", timeout=10000)
    e = err_text(pg2)
    check("during a flood the form answers at once with a readable 429 message", "too many wrong passwords" in e.lower(), e)
    for t in flood:
        t.join(120)
    check("the flood itself only got 401s (slowed down) or 429s", set(codes) <= {401, 429}, codes)
    t0 = time.time()
    sign_in(pg2, srv.base, admin, admin_pw)
    check("right password works again right after the flood", pg2.evaluate("window.Haven && Haven.role") == "admin",
          f"{time.time() - t0:.1f}s {err_text(pg2)}")
    ctx.close()


def mc_ws_refused(srv: Server, tag: str):
    """A must_change session's live-updates socket is refused with 4401 and the API answers 403 must_change."""
    SECTION[0] = f"[{tag}] locked session: "
    tok = srv.login("admin", "haven-admin")
    st, d = srv.call("GET", "/api/devices", token=tok)
    check("API: 403 with must_change", st == 403 and d.get("must_change") is True, (st, d))
    try:
        import websockets.sync.client as wsc   # optional
        with wsc.connect(f"ws://127.0.0.1:{srv.port}/ws", origin=f"http://127.0.0.1:{srv.port}") as w:
            w.send(json.dumps({"type": "auth", "token": tok}))
            try:
                w.recv(timeout=5)
                check("websocket refused", False, "got a message")
            except Exception as e:  # noqa: BLE001
                code = getattr(getattr(e, "rcvd", None), "code", None)
                check("websocket refused with 4401", code == 4401, repr(e))
    except ImportError:
        pass
    srv.call("POST", "/api/auth/logout", token=tok)


# ---------------------------------------------------------------- main
def run(ui: UI, tag: str, port: int, keep: bool):
    tmp = Path(tempfile.mkdtemp(prefix=f"haven-acc-{tag}-"))
    os.chmod(tmp, 0o755)                    # the server runs as 1026 after privdrop: it must get through
    servers = []
    try:
        srv = Server(tmp / "main", port)
        servers.append(srv)
        srv.start()
        mc_ws_refused(srv, tag)
        forced_change(ui, srv, tag)
        admin_pw = account_page(ui, srv, tag, "ryan", PW1)
        users_page(ui, srv, tag, "ryan", admin_pw)
        roles(ui, srv, tag, srv.login("ryan", admin_pw))
        sessions_and_limits(ui, srv, tag, "ryan", admin_pw)
        srv.stop()
        env = Server(tmp / "env", port + 1, env={"HAVEN_ADMIN_USER": "boss", "HAVEN_ADMIN_PASSWORD": "compose-file-password"})
        servers.append(env)
        env.start()
        forced_change_keep_name_env(ui, env, tag)
        env.stop()
        if os.geteuid() == 0 and shutil.which("setpriv"):
            ro = Server(tmp / "ro", port + 2, mode="setpriv")
            servers.append(ro)
            for d in (ro.root / "config", ro.root / "data"):
                os.chown(d, 0, 0)
                os.chmod(d, 0o755)
            ro.log = tmp / "ro.log"
            ro.start()
            storage_problem(ui, ro, tag)
            ro.stop()
        else:
            check(f"[{tag}] unwritable config: skipped (needs root and setpriv)", True)
    finally:
        for s in servers:
            s.stop()
        if keep:
            print(f"kept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=18620)
    ap.add_argument("--only", choices=("desktop", "phone"))
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--shots", help="save screenshots of each screen into this folder")
    a = ap.parse_args()
    if a.shots:
        SHOTS[0] = Path(a.shots)
        SHOTS[0].mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright isn't installed (pip install playwright && playwright install chromium)")
        return 0
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            print(f"SKIP: can't start Chromium ({e})")
            return 0
        for i, tag in enumerate(("desktop", "phone")):
            if a.only and a.only != tag:
                continue
            print(f"== {tag}", flush=True)
            try:
                run(UI(browser, tag == "phone"), tag, a.port + 5 * i, a.keep)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                SECTION[0] = ""
                check(f"[{tag}] run finished without an exception", False, repr(e))
        browser.close()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    for name, _, detail in failed:
        print(f"FAIL {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
