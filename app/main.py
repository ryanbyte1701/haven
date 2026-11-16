from __future__ import annotations

import asyncio
import logging
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
from .history import History, render_metrics
from .automations import AutomationEngine
from .hub import Hub
from .timesync import TimeService

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("haven")
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
                await old.stop()
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

    def statuses(self) -> dict[str, str]:
        return {**self.errors, **{n: a.status for n, a in hub.adapters.items()}, "history": history_store.status,
                "time": time_service.status_line}


manager = Manager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_data_dir()
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


app = FastAPI(title="Haven", lifespan=lifespan)


def _authorized(request_token: str | None) -> bool:
    return not DASH_TOKEN or (request_token is not None and secrets.compare_digest(request_token, DASH_TOKEN))


@app.middleware("http")
async def auth(request: Request, call_next):
    p = request.url.path
    if p.startswith("/api/") and p not in ("/api/health",):
        tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or \
            request.query_params.get("token")
        if not _authorized(tok):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


# ---------------- Ecowitt push (gateway can't send a token) ----------------
@app.post("/data/report")
@app.post("/data/report/")
@app.post("/ecowitt")
async def ecowitt_push(request: Request):
    adapter = hub.adapters.get("ecowitt")
    if not adapter:
        raise HTTPException(404, "ecowitt adapter not enabled")
    form = dict(await request.form())
    if not adapter.ingest({k: str(v) for k, v in form.items()}):
        raise HTTPException(403, "unknown PASSKEY")
    return PlainTextResponse("OK")


# ---------------- REST API ----------------
@app.get("/api/health")
async def health():
    return {"adapters": manager.statuses(), "devices": len(hub.devices), "auth": bool(DASH_TOKEN)}


@app.get("/api/config")
async def ui_config():
    return {"ui": manager.cfg.get("ui", {}), "adapters": manager.statuses(), "auth": bool(DASH_TOKEN)}


@app.get("/api/devices")
async def devices(all: bool = False):
    return [d.model_dump() for d in hub.devices.values() if all or not d.hidden]


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
        raise HTTPException(502, f"device error: {e}")
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
    rooms: list[str] = []
    order: dict[str, list[str]] = {}
    pinned: list[str] = []
    views: list[dict] = []


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
@app.get("/api/nut/server")
async def nut_server():
    a = hub.adapters.get("nut")
    return a.server_info() if a else {"enabled": False}


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
    """Run now, skipping the trigger and conditions (for testing a rule)."""
    eng = _engine()
    if aid not in eng.items:
        raise HTTPException(404)
    return await eng.run(aid, "run manually", force=True)


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
    """Prometheus scrape endpoint. Not behind DASH_TOKEN; protected by history.metrics_token if set."""
    if not history_store.metrics_enabled:
        raise HTTPException(404, "metrics disabled")
    tok = history_store.metrics_token
    if tok:
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        import hmac
        if not hmac.compare_digest(got, tok):
            return PlainTextResponse("unauthorized\n", status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(render_metrics(hub), media_type="text/plain; version=0.0.4; charset=utf-8")


# ---------------- live updates ----------------
@app.websocket("/ws")
async def ws(socket: WebSocket):
    if not _authorized(socket.query_params.get("token")):
        await socket.close(code=4401)
        return
    await socket.accept()
    q = hub.subscribe()
    try:
        await socket.send_json({"type": "snapshot", "devices": [d.model_dump() for d in hub.devices.values()],
                                "layout": hub.layout})
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
            except asyncio.TimeoutError:
                msg = {"type": "ping"}
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
