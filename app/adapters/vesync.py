"""Levoit air purifiers (Core 200S/300S/400S/600S, Vital 100S/200S, EverestAir, LV-PUR131S…) via the
VeSync cloud, using pyvesync 3.x (the library behind Home Assistant's VeSync integration).

Sign in with the email and password of the VeSync app. The session token is cached in
/data/vesync_auth.json so Haven doesn't log in on every restart.
"""
from __future__ import annotations

import os
import asyncio
from typing import Any

from ..config import DATA_DIR
from ..models import Device, cap
from .base import Adapter

AUTH_FILE = "vesync_auth.json"
COUNTRIES = ["US", "CA", "GB", "DE", "FR", "IT", "ES", "NL", "AU", "NZ", "JP", "IE", "BE", "AT", "CH", "SE", "PL"]


def _enum(v: Any) -> Any:
    return getattr(v, "value", v)


def read_state(dev) -> dict[str, Any]:
    """Map a pyvesync purifier to capability values."""
    st = dev.state
    out: dict[str, Any] = {}
    status = str(_enum(getattr(st, "device_status", "")) or "").lower()
    out["switch"] = status in ("on", "running")
    level = getattr(st, "fan_level", None)
    if level is not None:
        out["fan_speed"] = str(level)
    mode = str(_enum(getattr(st, "mode", "")) or "").lower()
    if mode and mode != "unknown":
        out["mode"] = mode
    for attr, key in (("pm25", "pm25"), ("pm10", "pm10"), ("voc", "voc"), ("co2", "co2"),
                      ("humidity", "humidity"), ("filter_life", "filter_life")):
        v = getattr(st, attr, None)
        if isinstance(v, (int, float)) and v >= 0:
            out[key] = v
    t = getattr(st, "temperature", None)
    if isinstance(t, (int, float)):
        # VeSync reports °F on most purifiers that have a sensor
        out["temperature"] = round((t - 32) * 5 / 9, 1) if t > 45 else t
    has_aq = getattr(dev, "supports_air_quality", False)
    # pyvesync 3 keeps readings on dev.state; older versions had them on the device itself
    aq = (getattr(st, "air_quality_value", None) if st is not None else None) if has_aq else None
    if aq is None and has_aq:
        aq = getattr(dev, "air_quality_value", None)
    if isinstance(aq, (int, float)) and aq >= 0 and "pm25" not in out:
        out["pm25"] = aq
    label = None
    try:
        label = (getattr(st, "air_quality_string", None) or getattr(dev, "air_quality_string", None)) if has_aq else None
    except Exception:  # noqa: BLE001
        pass
    out["status_text"] = (str(label).replace("_", " ").title() if label else None) or ("Running" if out["switch"] else "Off")
    conn = str(_enum(getattr(st, "connection_status", "online")) or "online").lower()
    out["_online"] = conn != "offline"
    return out


def purifier_caps(dev) -> dict:
    levels = [str(x) for x in (getattr(dev, "fan_levels", None) or [])]
    modes = [str(_enum(m)).lower() for m in (getattr(dev, "modes", None) or [])]
    caps = {"switch": cap("switch", label="Power"), "status_text": cap("status_text", label="Air")}
    if levels:
        caps["fan_speed"] = cap("fan_speed", writable=True, options=levels, label="Fan speed", unit=None)
    if modes:
        caps["mode"] = cap("mode", writable=True, options=modes, label="Mode")
    for k, kw in (("pm25", {}), ("pm10", dict(unit="µg/m³", label="PM10")), ("filter_life", dict(unit="%", label="Filter"))):
        caps[k] = cap(k, **kw)
    return caps


class VeSyncAdapter(Adapter):
    source = "vesync"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.username = cfg.get("username") or ""
        self.password = cfg.get("password") or ""
        self.country = (cfg.get("country") or "US").upper()
        self.poll_seconds = max(30, int(cfg.get("poll_seconds") or 60))
        self.manager = None
        self.devs: dict[str, Any] = {}

    async def start(self):
        if not self.username or not self.password:
            self.status = "enter your VeSync email and password"
            return
        self.spawn("poll", self._loop, max_delay=300)

    async def stop(self):
        await super().stop()
        await self._close()

    async def _close(self):
        if self.manager is not None:
            try:
                await self.manager.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self.manager = None

    async def _login(self):
        from pyvesync import VeSync
        await self._close()
        self.manager = VeSync(self.username, self.password, country_code=self.country)
        auth = DATA_DIR / AUTH_FILE
        loaded = False
        if auth.exists():
            try:
                loaded = await self.manager.load_credentials_from_file(auth)
            except Exception:  # noqa: BLE001
                loaded = False
        if not loaded:
            if not await self.manager.login():
                raise RuntimeError("VeSync sign-in failed (check email/password)")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            old = os.umask(0o077)               # pyvesync writes the file itself
            try:
                await self.manager.save_credentials(auth)
            finally:
                os.umask(old)
            try:
                auth.chmod(0o600)
            except OSError:
                pass

    async def _loop(self):
        await self._login()
        try:
            await self.manager.get_devices()
        except Exception:  # noqa: BLE001 - any failure: sign in fresh once
            # cached token may have expired: drop it and log in fresh once
            (DATA_DIR / AUTH_FILE).unlink(missing_ok=True)
            await self._login()
            await self.manager.get_devices()
        purifiers = list(self.manager.devices.air_purifiers)
        for d in purifiers:
            did = self.dev_id(d.cid)
            self.devs[did] = d
            self.hub.upsert_device(Device(id=did, name=d.device_name, source=self.source,
                                          model=f"Levoit {d.device_type}", capabilities=purifier_caps(d)))
            self.hub.set_attributes(did, {"kind": "air_purifier"})
        self.status = "ok" if purifiers else "signed in, but no air purifiers found"
        while True:
            for did, d in list(self.devs.items()):
                try:
                    await d.update()
                    vals = read_state(d)
                    online = vals.pop("_online", True)
                    self.hub.set_values(did, vals, online=online)
                    self.poll_ok(did)
                except Exception as e:  # noqa: BLE001
                    self.log.debug("vesync %s: %s", did, e)
                    self.poll_failed(did, e, list(self.devs))
            await asyncio.sleep(self.poll_seconds)

    async def command(self, device: Device, capability: str, value: Any):
        d = self.devs.get(device.id)
        if d is None:
            raise RuntimeError("device not found")
        if capability == "switch":
            ok = await (d.turn_on() if value else d.turn_off())
        elif capability == "fan_speed":
            ok = await d.set_fan_speed(int(value))
        elif capability == "mode":
            ok = await d.set_mode(str(value))
        else:
            raise ValueError(capability)
        if ok is False:
            raise RuntimeError("VeSync rejected the command")
        self.hub.set_values(device.id, {capability: value})


ADAPTER = VeSyncAdapter
SETTINGS = {
    "id": "vesync", "label": "Levoit (VeSync)", "icon": "air",
    "help": "Levoit air purifiers through the VeSync cloud. Use the email and password you sign in to the "
            "VeSync app with. Power, fan level and mode can be controlled; PM2.5 and filter life are shown "
            "where the model reports them.",
    "fields": [
        {"key": "username", "label": "VeSync email", "type": "text"},
        {"key": "password", "label": "VeSync password", "type": "password"},
        {"key": "country", "label": "Account country", "type": "select", "default": "US", "options": COUNTRIES},
        {"key": "poll_seconds", "label": "Refresh every (seconds)", "type": "number", "default": 60},
    ],
}
