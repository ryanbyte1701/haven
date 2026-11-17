"""Ring doorbells, cameras, floodlights and chimes via the community ring_doorbell library
(the same one Home Assistant's Ring integration uses).

Sign in once from Settings > Ring (handles the 2-step verification code). Tokens are kept in
/data/ring_token.json and refreshed automatically. Motion and doorbell presses arrive instantly
over Ring's push channel; battery and settings are polled.

Ring Alarm (contact/motion sensors, keypad, alarm mode) isn't covered by this library. Run
the ring-mqtt bridge and those devices appear through the MQTT adapter automatically.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..config import DATA_DIR
from ..models import Device, cap
from .base import Adapter

USER_AGENT = "Haven-1.0"
TOKEN_FILE = "ring_token.json"  # noqa: S105 - a file name, not a password
FCM_FILE = "ring_fcm.json"
EVENT_HOLD = 60   # seconds motion / ding stay "active" after an event


def _load(name: str) -> dict | None:
    try:
        return json.loads((DATA_DIR / name).read_text())
    except Exception:  # noqa: BLE001
        return None


def _save(name: str, data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / name
    from ..config import write_private
    write_private(p, json.dumps(data))


async def login(email: str, password: str, code: str | None = None) -> dict:
    """Returns {"ok": True} or {"need_code": True}. Raises on bad credentials."""
    from ring_doorbell import Auth, Requires2FAError
    auth = Auth(USER_AGENT, None, lambda t: _save(TOKEN_FILE, t))
    try:
        await auth.async_fetch_token(email, password, code or None)
        return {"ok": True}
    except Requires2FAError:
        return {"need_code": True}
    finally:
        await auth.async_close()


def has_token() -> bool:
    return bool(_load(TOKEN_FILE))


class RingAdapter(Adapter):
    source = "ring"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.poll_seconds = int(cfg.get("poll_seconds", 120))
        self.ring = None
        self.auth = None
        self.listener = None
        self.devs: dict[str, Any] = {}           # our id -> ring device
        self._clear: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._snap: dict[str, tuple[float, bytes]] = {}

    async def start(self):
        self.spawn("main", self._main)

    async def _close_session(self):
        for closer in (lambda: self.listener and self.listener.stop(), lambda: self.auth and self.auth.async_close()):
            try:
                c = closer()
                if c:
                    await c
            except Exception:  # noqa: BLE001
                pass
        self.listener = self.auth = None

    async def stop(self):
        await super().stop()
        await self._close_session()

    async def _main(self):
        from ring_doorbell import Auth, Ring
        await self._close_session()          # previous attempt's session/listener, if any
        token = _load(TOKEN_FILE)
        if not token:
            self.status = "needs sign-in (Settings > Ring)"
            while not _load(TOKEN_FILE):
                await asyncio.sleep(10)
            token = _load(TOKEN_FILE)
        self.auth = Auth(USER_AGENT, token, lambda t: _save(TOKEN_FILE, t))
        self.ring = Ring(self.auth)
        await self.ring.async_create_session()
        await self.ring.async_update_data()
        self._sync_devices()
        self.status = "ok"
        self._tasks.append(asyncio.create_task(self._listen()))
        while True:
            await asyncio.sleep(self.poll_seconds)
            await self.ring.async_update_devices()
            self._sync_devices()

    async def _listen(self):
        try:
            from ring_doorbell import RingEventListener
            self.listener = RingEventListener(self.ring, _load(FCM_FILE), lambda c: _save(FCM_FILE, c))
            self.listener.add_notification_callback(self._on_event)
            if not await self.listener.start():
                self.log.warning("Ring push events unavailable; motion/ding will not be instant")
        except Exception as e:  # noqa: BLE001
            self.log.warning("Ring event listener failed: %s", e)

    def _sync_devices(self):
        for d in self.ring.devices().all_devices:
            did = self.dev_id(str(d.id))
            caps = {}
            if d.battery_life is not None:
                caps["battery"] = cap("battery")
            fam = getattr(d, "family", "")
            if fam in ("doorbots", "authorized_doorbots", "stickup_cams"):
                caps["motion"] = cap("motion")
            if fam in ("doorbots", "authorized_doorbots"):
                caps["ding"] = cap("ding")
            if d.has_capability("light"):
                caps["light"] = cap("light")
            if d.has_capability("siren"):
                caps["action"] = cap("action", options=["siren"])
            if did not in self.devs:
                self.hub.upsert_device(Device(id=did, name=d.name, source=self.source, model=d.model,
                                              capabilities=caps))
            self.devs[did] = d
            vals: dict[str, Any] = {}
            if d.battery_life is not None:
                vals["battery"] = int(d.battery_life)
            if "light" in caps:
                vals["light"] = bool(getattr(d, "light", False))
            online = getattr(d, "connection_status", "online") != "offline"
            self.hub.set_attributes(did, {"ring_kind": d.kind, "family": fam, "wifi": d.wifi_name,
                                          "wifi_signal": d.wifi_signal_strength,
                                          "has_snapshot": fam in ("doorbots", "authorized_doorbots", "stickup_cams")})
            self.hub.set_values(did, vals, online=online)

    def _on_event(self, ev):
        did = self.dev_id(str(ev.doorbot_id))
        if did not in self.devs:
            return
        kind = "ding" if ev.kind in ("ding", "intercom_ding") else "motion" if ev.kind == "motion" else None
        if not kind:
            return
        self.hub.set_attributes(did, {f"last_{kind}": time.time()})
        self.hub.set_values(did, {kind: True})
        key = (did, kind)
        if key in self._clear:
            self._clear[key].cancel()
        loop = asyncio.get_running_loop()
        self._clear[key] = loop.call_later(EVENT_HOLD, lambda: self.hub.set_values(did, {kind: False}))

    async def snapshot(self, device_id: str) -> bytes | None:
        d = self.devs.get(device_id)
        if not d or not hasattr(d, "async_get_snapshot"):
            return None
        cached = self._snap.get(device_id)
        if cached and time.time() - cached[0] < 30:
            return cached[1]
        img = await d.async_get_snapshot(retries=5, delay=1)
        if img:
            self._snap[device_id] = (time.time(), img)
        return img

    async def command(self, device: Device, capability: str, value: Any):
        d = self.devs[device.id]
        if capability == "light":
            await d.async_set_light(bool(value))
            self.hub.set_values(device.id, {"light": bool(value)})
        elif capability == "action" and value == "siren":
            await d.async_set_siren(30)
        else:
            raise ValueError(capability)
