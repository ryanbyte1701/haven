from __future__ import annotations

import asyncio
import logging
import re
import secrets
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import settings as settings_mod
from .adapters import ADAPTERS
from .config import DASH_TOKEN, check_data_dir
from . import security as sec
from .history import History, render_metrics
from .hub import CLOSE
from .automations import AutomationEngine
from .hub import Hub
from .timesync import TimeService

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("haven")
# httpx logs full request URLs at INFO (API keys in query strings, webhook tokens): keep it quiet
for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").addFilter(sec.RedactAccessLog())
hub = Hub()
history_store = History(hub)
hub.recorder = history_store
time_service = TimeService()
automations: AutomationEngine | None = None      # created at startup (needs the data folder)
STATIC = Path(__file__).parent / "static"


class Manager:
    """Starts, stops and restarts adapters so Settings changes apply without a container restart."""

    def __init__(self) -> None:
        self.cfg: dict = {}
        self.errors: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start_all(self) -> None:
        self.cfg = settings_mod.effective_config()
        for name in ADAPTERS:
            await self._start(name)

    async def _start(self, name: str) -> None:
        section = self.cfg.get(name)
        self.errors.pop(name, None)
        if section is None or not section.get("enabled", True):
            return
        try:
            adapter = ADAPTERS[name](hub, section)
            hub.adapters[name] = adapter
            await adapter.start()
            log.info("started %s", name)
        except Exception as e:  # noqa: BLE001
            failed = hub.adapters.pop(name, None)
            if failed:                         # don't leave half-started background tasks running
                try:
                    await failed.stop()
                except Exception:  # noqa: BLE001
                    pass
            self.errors[name] = f"error: {e}"
            log.error("could not start %s: %s", name, e)

    async def restart(self, name: str) -> None:
        async with self._lock:
            old = hub.adapters.pop(name, None)
            if old:
                try:
                    await old.stop()
                except Exception as e:  # noqa: BLE001 - a failing stop must not leave the integration gone
                    log.warning("stopping %s failed: %s", name, e)
            self.cfg = settings_mod.effective_config()
            section = self.cfg.get(name)
            # Drop the old devices so a changed account / removed UPS doesn't leave ghosts.
            # Ecowitt is push-based (devices only reappear on the next upload), so keep those
            # unless it was turned off.
            if name != "ecowitt" or section is None or not section.get("enabled", True):
                hub.remove_source(name)
            await self._start(name)

    async def stop_all(self) -> None:
        for name, a in list(hub.adapters.items()):
            try:
                await a.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("stopping %s: %s", name, e)

    def secrets(self) -> list[str]:
        """Every configured secret value, so it can be blanked out of messages shown in the UI."""
        out: list[str] = []
        def walk(x, key=""):
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(v, str(k))
            elif isinstance(x, list):
                for v in x:
                    walk(v, key)
            elif isinstance(x, str) and re.search(r"pass|secret|token|key", key, re.I) and len(x) >= 4:
                out.append(x)
        walk(self.cfg)
        if DASH_TOKEN:
            out.append(DASH_TOKEN)
        return out

    def statuses(self) -> dict[str, str]:
        raw = {**self.errors, **{n: a.status for n, a in hub.adapters.items()}, "history": history_store.status,
               "time": time_service.status_line}
        hidden = self.secrets()
        return {k: sec.redact(v, hidden) if isinstance(v, str) else v for k, v in raw.items()}


manager = Manager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_data_dir()
    if not DASH_TOKEN:
        log.warning("No dashboard password (DASH_TOKEN) is set: anyone on your network can use Haven. "
                    "Account settings, sign-ins and automations can't be changed until you set one%s.",
                    " (HAVEN_ALLOW_NO_PASSWORD=1 overrides this)" if NO_PW_OK else "")
    elif len(DASH_TOKEN) < 12:
        log.warning("DASH_TOKEN is shorter than 12 characters: use a longer password")
    global automations
    from .config import DATA_DIR
    await history_store.configure(settings_mod.effective_config().get("history"))   # before adapters report
    await time_service.configure(settings_mod.effective_config().get("time"))
    automations = AutomationEngine(hub, time_service, DATA_DIR)
    hub.listeners.append(automations.on_change)
    await automations.start()
    await manager.start_all()
    yield
    await manager.stop_all()
    await automations.stop()
    await time_service.stop()
    await history_store.stop()


app = FastAPI(title="Haven", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Request bodies: gateways send a few KB, the UI a few hundred KB at most
app.add_middleware(sec.BodyLimit, limits=[("/data/report", 64 * 1024), ("/ecowitt", 64 * 1024)], default=1024 * 1024)
NO_PW_OK = os.environ.get("HAVEN_ALLOW_NO_PASSWORD") == "1"
auth_limiter = sec.AuthLimiter()
ecowitt_limiter = sec.RateLimiter(rate=1.0, burst=5)
# with no password, these writes would hand your accounts to anyone on the LAN: refuse them
NEEDS_PASSWORD = ("/api/settings", "/api/automations", "/api/ring/", "/api/aqara/", "/api/hue/", "/api/ecowitt/")


def _token_ok(given: str | None, expected: str) -> bool:
    # compare bytes: compare_digest on str raises TypeError for non-ASCII passwords
    return given is not None and secrets.compare_digest(given.encode(), expected.encode())


def _authorized(request_token: str | None) -> bool:
    return not DASH_TOKEN or _token_ok(request_token, DASH_TOKEN)


def _request_token(request) -> str | None:
    # header only: tokens in URLs end up in access logs, browser history and proxy logs
    return request.headers.get("authorization", "").removeprefix("Bearer ").strip() or None


def _client_ip(scope_or_request) -> str:
    c = getattr(scope_or_request, "client", None)
    return (c.host if c else None) or "?"


def _trusted_proxy(ip: str) -> bool:
    allow = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
    return "*" in allow or ip in [a.strip() for a in allow.split(",")]


def _same_origin(headers, peer: str | None = None) -> bool:
    """Browsers send Origin on cross-site requests and WebSockets. Reject any whose host differs
    from the Host we were reached on (blocks other websites and DNS-rebinding pages)."""
    origin = headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    from urllib.parse import urlsplit
    o = urlsplit(origin)
    fwd = headers.get("x-forwarded-host") if peer is not None and _trusted_proxy(peer) else None
    host = (fwd or headers.get("host") or "").split(",")[0].strip().lower()
    return bool(o.netloc) and o.netloc.lower() == host


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "connect-src 'self' ws: wss:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'")


@app.middleware("http")
async def auth(request: Request, call_next):
    p = request.url.path
    ip = _client_ip(request)
    if not p.startswith(("/data/report", "/ecowitt")):
        h = request.headers.get("x-forwarded-host") if _trusted_proxy(ip) and request.headers.get("x-forwarded-host") else request.headers.get("host", "")
        if not sec.host_allowed(h.split(",")[0]):
            return PlainTextResponse(
                f"Haven doesn't answer to the name {h.split(',')[0][:80]!r}. If you use this name on purpose "
                "(a reverse proxy or your own domain), add it to HAVEN_ALLOWED_HOSTS in docker-compose.yml.\n",
                status_code=421)
    if p.startswith("/api/"):
        writing = request.method in ("POST", "PUT", "PATCH", "DELETE")
        if writing and not _same_origin(request.headers, ip):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        if p != "/api/health":
            if DASH_TOKEN:
                wait = auth_limiter.retry_after(ip)
                if wait:
                    return JSONResponse({"detail": "too many wrong passwords, try again shortly"}, status_code=429,
                                        headers={"Retry-After": str(int(wait) + 1)})
                if not _authorized(_request_token(request)):
                    auth_limiter.failed(ip)
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
                auth_limiter.ok(ip)
            elif writing and not NO_PW_OK and p.startswith(NEEDS_PASSWORD):
                return JSONResponse({"detail": "Set a dashboard password first (DASH_TOKEN in docker-compose.yml). "
                                     "Until then anyone on your network could change your accounts."}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.scheme == "https" and os.environ.get("HAVEN_HTTPS_ONLY") == "1":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


# ---------------- Ecowitt push (gateway can't send a token) ----------------
@app.post("/data/report")
@app.post("/data/report/")
@app.post("/ecowitt")
@app.post("/data/report/{secret}")
async def ecowitt_push(request: Request, secret: str = ""):
    adapter = hub.adapters.get("ecowitt")
    if not adapter:
        raise HTTPException(404, "ecowitt adapter not enabled")
    ip = _client_ip(request)
    if not ecowitt_limiter.allow(ip):
        raise HTTPException(429, "slow down")
    via_secret = bool(secret)
    if via_secret and not adapter.check_secret(secret):
        raise HTTPException(404)
    form = await request.form(max_files=0, max_fields=400, max_part_size=4096)
    result = adapter.ingest({str(k)[:40]: str(v)[:64] for k, v in form.items()}, via_secret=via_secret, ip=ip)
    if result == "full":
        raise HTTPException(403, "unknown gateway")
    return PlainTextResponse("OK")   # also for "pending": the gateway can't do anything with an error


@app.get("/api/ecowitt/gateways")
async def ecowitt_gateways():
    a = hub.adapters.get("ecowitt")
    if not a:
        raise HTTPException(404, "Ecowitt is off")
    return a.gateways()


@app.post("/api/ecowitt/gateways/{gid}/approve")
async def ecowitt_approve(gid: str):
    a = hub.adapters.get("ecowitt")
    if not a or not a.approve(gid):
        raise HTTPException(400, "That gateway isn't waiting any more, or 4 gateways are already trusted")
    return a.gateways()


@app.delete("/api/ecowitt/gateways/{gid}")
async def ecowitt_forget(gid: str):
    a = hub.adapters.get("ecowitt")
    if not a or not a.forget(gid):
        raise HTTPException(404, "not found")
    return a.gateways()


# ---------------- REST API ----------------
@app.get("/api/health")
async def health(request: Request):
    """Docker healthcheck. Details (adapter errors, device count) only with the dashboard password."""
    if DASH_TOKEN and not _authorized(_request_token(request)):
        return {"ok": True, "auth": True}
    return {"ok": True, "adapters": manager.statuses(), "devices": len(hub.devices), "auth": bool(DASH_TOKEN)}


@app.get("/api/config")
async def ui_config():
    t = manager.cfg.get("time") or {}
    return {"ui": manager.cfg.get("ui", {}), "adapters": manager.statuses(), "auth": bool(DASH_TOKEN),
            "time_format": t.get("time_format") or "12h",
            "timezone": time_service.status().get("timezone") if time_service else None}


@app.get("/api/devices")
async def devices(all: bool = False):
    return [d.model_dump() for d in hub.devices.values() if all or not d.hidden]


@app.get("/api/devices/{device_id}/history")
async def device_history(device_id: str, range: str = "24h"):  # noqa: A002
    """Every chartable capability of one device in one call (for the device's History tab)."""
    from .history import STATE_NUMBERS
    dev = hub.devices.get(device_id)
    if dev is None:
        raise HTTPException(404, "unknown device")
    caps = [k for k, c in dev.capabilities.items()
            if isinstance(c.value, (int, float)) or k in STATE_NUMBERS or c.value is None and k in STATE_NUMBERS]
    caps = caps[:40]
    results = await asyncio.gather(*(history_store.query(device_id, k, range) for k in caps), return_exceptions=True)
    series = {k: r for k, r in zip(caps, results) if isinstance(r, list)}
    return {"range": range, "series": series, "states": {k: v for k, v in STATE_NUMBERS.items() if k in series}}


@app.get("/api/devices/{device_id}/history/{capability}")
async def history(device_id: str, capability: str, range: str = "24h"):  # noqa: A002
    return await history_store.query(device_id, capability, range)


class Command(BaseModel):
    capability: str
    value: Any


@app.post("/api/devices/{device_id}/command")
async def command(device_id: str, cmd: Command):
    try:
        await hub.command(device_id, cmd.capability, cmd.value)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("command failed")
        raise HTTPException(502, f"device error: {sec.redact(e, manager.secrets())}")
    return {"ok": True}


TAB_IDS = {"climate", "lights", "water", "doors", "security", "controls"}


class DevicePatch(BaseModel):
    name: str | None = None
    room: str | None = None
    hidden: bool | None = None
    categories: list[str] | None = None       # manual tab assignment
    auto_categories: bool | None = None       # true = go back to automatic tabs


@app.patch("/api/devices/{device_id}")
async def patch_device(device_id: str, patch: DevicePatch):
    fields = patch.model_dump()
    if fields["categories"] is not None:
        bad = set(fields["categories"]) - TAB_IDS
        if bad:
            raise HTTPException(400, f"unknown tab(s): {', '.join(sorted(bad))}")
    dev = hub.set_override(device_id, **fields)
    if not dev:
        raise HTTPException(404)
    return dev.model_dump()


# ---------------- Layout ----------------
@app.get("/api/layout")
async def get_layout():
    return hub.layout


class Layout(BaseModel):
    # None = "leave unchanged" (e.g. resetting the order must not delete custom views)
    rooms: list[str] | None = None
    order: dict[str, list[str]] | None = None
    pinned: list[str] | None = None
    views: list[dict] | None = None


@app.put("/api/layout")
async def put_layout(body: Layout):
    return hub.set_layout(body.model_dump())


# ---------------- Settings ----------------
@app.get("/api/settings")
async def get_settings():
    return {"sections": settings_mod.public_view(manager.cfg, manager.statuses()),
            "auth": bool(DASH_TOKEN), "file": str(settings_mod.SETTINGS_FILE)}


class SectionSave(BaseModel):
    enabled: bool
    values: dict[str, Any] = {}


@app.put("/api/settings/{section}")
async def put_settings(section: str, body: SectionSave):
    if section not in settings_mod.SCHEMA_BY_ID:
        raise HTTPException(404)
    try:
        settings_mod.save_section(section, body.enabled, body.values)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, f"invalid value: {e}")
    if section == "history":   # not an adapter: reconfigure the history store in place
        manager.cfg = settings_mod.effective_config()
        await history_store.configure(manager.cfg.get("history"))
    elif section == "time":    # not an adapter: reconfigure NTP/timezone in place
        manager.cfg = settings_mod.effective_config()
        await time_service.configure(manager.cfg.get("time"))
        await asyncio.sleep(1.2)   # usually enough for a first NTP answer, so the status is meaningful
    else:
        await manager.restart(section)
    await asyncio.sleep(0.3)
    sec = next(s for s in settings_mod.public_view(manager.cfg, manager.statuses()) if s["id"] == section)
    return sec


class RingLogin(BaseModel):
    email: str
    password: str
    code: str | None = None


@app.post("/api/settings/ring/login")
async def ring_login(body: RingLogin):
    from .adapters.ring import login
    try:
        res = await login(body.email, body.password, body.code)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ring sign-in failed: {e}")
    if res.get("ok") and "ring" in hub.adapters:
        await manager.restart("ring")
    return res


class AqaraLogin(BaseModel):
    account: str
    code: str | None = None


@app.post("/api/settings/aqara/login")
async def aqara_login(body: AqaraLogin):
    from .aqara_login import exchange_code, request_code
    cfg = manager.cfg.get("aqara") or {}
    try:
        if not body.code:
            await request_code(cfg, body.account)
            return {"need_code": True}
        await exchange_code(cfg, body.account, body.code)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Aqara sign-in failed: {e}")
    if "aqara" in hub.adapters:
        await manager.restart("aqara")
    return {"ok": True}


@app.get("/api/settings/hue/discover")
async def hue_discover():
    from .adapters.hue import discover
    return {"bridges": await discover()}


class HuePair(BaseModel):
    host: str


@app.post("/api/settings/hue/pair")
async def hue_pair(body: HuePair):
    from .adapters.hue import pair
    host = body.host.strip()
    if not host:
        raise HTTPException(400, "Enter the bridge IP address")
    try:
        res = await pair(host)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't pair with the Hue Bridge at {host}: {e}")
    if res.get("ok"):
        settings_mod.save_section("hue", True, {"host": host})
        await manager.restart("hue")
    return res


@app.post("/api/settings/{section}/signin")
async def generic_signin(section: str, body: dict):
    """Generic sign-in for self-registering integrations: the adapter class defines
    `async def signin(cls, values: dict, data_dir) -> dict` returning {"ok": True} or
    {"need_code": True, "message": "..."} (then called again with values["code"])."""
    cls = ADAPTERS.get(section)
    if not cls or not hasattr(cls, "signin"):
        raise HTTPException(404, "this integration has no sign-in")
    from .config import DATA_DIR
    try:
        res = await cls.signin({**(manager.cfg.get(section) or {}), **body}, DATA_DIR)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Sign-in failed: {e}")
    if res.get("ok") and section in hub.adapters:
        await manager.restart(section)
    return res


# ---------------- integration extras ----------------
@app.get("/api/ring/{device_id}/snapshot")
async def ring_snapshot(device_id: str):
    a = hub.adapters.get("ring")
    if not a:
        raise HTTPException(404, "Ring not enabled")
    img = await a.snapshot(device_id)
    if not img:
        raise HTTPException(504, "Camera didn't return a snapshot (battery cameras can be slow or asleep)")
    return Response(img, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/aqara/resources/{model}")
async def aqara_resources(model: str):
    a = hub.adapters.get("aqara")
    if not a:
        raise HTTPException(404, "aqara adapter not enabled")
    return await a.resource_info(model)


# ---------------- automations & time ----------------
class AutomationIn(BaseModel):
    id: str | None = None
    name: str = "Automation"
    enabled: bool = True
    cooldown: float = 0
    allow_unsafe: bool = False
    trigger: dict
    conditions: list[dict] = []
    actions: list[dict] = []


def _engine() -> AutomationEngine:
    if automations is None:
        raise HTTPException(503, "automations not started yet")
    return automations


@app.get("/api/automations")
async def list_automations():
    return list(_engine().items.values())


@app.post("/api/automations")
async def create_automation(body: AutomationIn):
    data = body.model_dump()
    data["id"] = None
    try:
        return _engine().upsert(data)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/automations/{aid}")
async def update_automation(aid: str, body: AutomationIn):
    eng = _engine()
    if aid not in eng.items:
        raise HTTPException(404)
    try:
        return eng.upsert({**body.model_dump(), "id": aid})
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/automations/{aid}")
async def delete_automation(aid: str):
    if not _engine().delete(aid):
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/automations/{aid}/run")
async def run_automation(aid: str):
    """Run now, skipping the trigger and conditions (for testing a rule). Runs in the background
    (a rule with delays can take a long time); the result appears in /api/automations/log."""
    eng = _engine()
    if aid not in eng.items:
        raise HTTPException(404)
    eng.start_run(aid, "run manually", force=True)
    return {"ok": True, "started": True}


@app.get("/api/automations/log")
async def automation_log():
    return list(_engine().runs)


@app.get("/api/notifications")
async def notifications():
    return list(_engine().notifications)


@app.get("/api/time")
async def time_status():
    return time_service.status()


# ---------------- history & Prometheus metrics ----------------
@app.get("/api/history/info")
async def history_info():
    return history_store.info()


@app.get("/metrics")
async def metrics(request: Request):
    """Prometheus scrape endpoint. Protected by history.metrics_token if set, otherwise by the
    dashboard password (DASH_TOKEN) when that is set."""
    if not history_store.metrics_enabled:
        raise HTTPException(404, "metrics disabled")
    tok = history_store.metrics_token or DASH_TOKEN
    if tok:
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not _token_ok(got, tok):
            return PlainTextResponse("unauthorized\n", status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(render_metrics(hub), media_type="text/plain; version=0.0.4; charset=utf-8")


# ---------------- live updates ----------------
@app.websocket("/ws")
async def ws(socket: WebSocket):
    # accept first so the browser actually sees our close code (a close before accept arrives as 1006)
    await socket.accept()
    ip = _client_ip(socket)
    if not _same_origin(socket.headers, ip) or not sec.host_allowed(socket.headers.get("host", "")):
        await socket.close(code=4403)
        return
    if DASH_TOKEN:
        # the password comes in the first message, never in the URL (URLs get logged)
        if auth_limiter.retry_after(ip):
            await socket.close(code=4429)
            return
        try:
            first = await asyncio.wait_for(socket.receive_json(), timeout=10)
        except Exception:  # noqa: BLE001  (timeout, bad JSON, disconnect)
            first = {}
        if not isinstance(first, dict) or first.get("type") != "auth" or not _authorized(str(first.get("token") or "")):
            auth_limiter.failed(ip)
            await socket.close(code=4401)
            return
        auth_limiter.ok(ip)
    q = hub.subscribe()
    try:
        await socket.send_json({"type": "snapshot", "devices": [d.model_dump() for d in hub.devices.values()],
                                "layout": hub.layout})
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
            except asyncio.TimeoutError:
                msg = {"type": "ping"}
            if msg is CLOSE:            # we fell too far behind: the client reconnects for a fresh snapshot
                await socket.close(code=4408)
                break
            await socket.send_json(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(q)


# ---------------- UI ----------------
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)
