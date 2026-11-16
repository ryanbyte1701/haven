"""Dyson purifiers / purifier-fans (Pure Cool, Purifier Cool, Hot+Cool, Humidify+Cool, Big+Quiet).

Uses libdyson-neon (the library behind the Home Assistant "Dyson Local" integration):
1. Sign in once with your MyDyson account (email + password, then a code Dyson emails you).
   This only fetches each device's LOCAL credential; it's saved in /data/dyson.json.
2. Haven then talks to every purifier directly over your LAN (MQTT on port 1883 of the device).
   Docker usually can't do mDNS discovery, so list each device as SERIAL=IP (the serial is on the
   sticker under the machine and in the MyDyson app; give the device a fixed IP in your router).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..config import DATA_DIR
from ..models import Device, cap
from .base import Adapter

CRED_FILE = "dyson.json"
REGIONS = ["US", "GB", "CA", "AU", "NZ", "IE", "DE", "FR", "IT", "ES", "NL", "BE", "CH", "AT", "SE", "NO", "DK",
           "FI", "PL", "JP", "KR", "SG", "HK", "IN", "AE", "SA", "CN"]
_pending: dict[str, tuple[float, Any]] = {}   # email -> (time, verify callable) between the two sign-in steps


def _save(data_dir, data: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / CRED_FILE
    p.write_text(json.dumps(data, indent=2))
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _load(data_dir=None) -> dict:
    try:
        return json.loads(((data_dir or DATA_DIR) / CRED_FILE).read_text())
    except Exception:  # noqa: BLE001
        return {}


def parse_hosts(entries: list[str]) -> dict[str, str]:
    """["SERIAL=192.168.1.5", ...] -> {"SERIAL": "192.168.1.5"} (serials compared case-insensitively)."""
    out = {}
    for e in entries or []:
        if "=" in str(e):
            serial, host = str(e).split("=", 1)
            if serial.strip() and host.strip():
                out[serial.strip().upper()] = host.strip()
    return out


def _env(v: Any) -> Any:
    """libdyson reports sensor states as negative sentinels (-1 off, -2 initialising, -3 failed)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return None if float(v) < 0 else v
    except (TypeError, ValueError):
        return None


def _attr(dev, name):
    try:
        return getattr(dev, name)
    except Exception:  # noqa: BLE001 - some models lack a property or it isn't populated yet
        return None


def read_state(dev) -> dict[str, Any]:
    """Map a libdyson fan device to capability values."""
    out: dict[str, Any] = {}
    on = _attr(dev, "is_on")
    if on is not None:
        out["switch"] = bool(on)
    speed = _attr(dev, "speed")
    if isinstance(speed, int):
        out["fan_speed"] = speed
    auto, night = _attr(dev, "auto_mode"), _attr(dev, "night_mode")
    out["mode"] = "auto" if auto else "night" if night else "manual"
    osc = _attr(dev, "oscillation")
    if osc is not None:
        out["oscillation"] = bool(osc)
    t = _env(_attr(dev, "temperature"))           # Kelvin
    if t is not None:
        out["temperature"] = round(float(t) - 273.15, 1)
    h = _env(_attr(dev, "humidity"))
    if h is not None:
        out["humidity"] = h
    for prop, key in (("particulate_matter_2_5", "pm25"), ("particulate_matter_10", "pm10"),
                      ("volatile_organic_compounds", "voc"), ("nitrogen_dioxide", "no2")):
        v = _env(_attr(dev, prop))
        if v is not None:
            out[key] = round(float(v), 1)
    fl = _attr(dev, "hepa_filter_life")
    if isinstance(fl, (int, float)) and fl >= 0:
        out["filter_life"] = int(fl)
    err = _attr(dev, "error_code")
    out["status_text"] = f"Error {err}" if err and str(err).upper() not in ("NONE", "OFF") else (
        "Running" if out.get("switch") else "Off")
    return out


def purifier_caps(has_osc: bool = True) -> dict:
    caps = {
        "switch": cap("switch", label="Power"),
        "fan_speed": cap("fan_speed", writable=True, min=1, max=10, step=1, label="Fan speed", unit=None),
        "mode": cap("mode", writable=True, options=["auto", "manual", "night"], label="Mode"),
        "pm25": cap("pm25"), "pm10": cap("pm10", unit="µg/m³", label="PM10"),
        "voc": cap("voc", label="VOC", unit=None), "no2": cap("no2", label="NO₂", unit=None),
        "temperature": cap("temperature"), "humidity": cap("humidity"),
        "filter_life": cap("filter_life", unit="%", label="Filter"),
        "status_text": cap("status_text", label="Status"),
    }
    if has_osc:
        caps["oscillation"] = cap("oscillation", writable=True, label="Oscillation")
    return caps


class DysonAdapter(Adapter):
    source = "dyson"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.hosts = parse_hosts(cfg.get("hosts") or [])
        self.devs: dict[str, Any] = {}       # our id -> libdyson device
        self.loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    async def signin(cls, values: dict, data_dir) -> dict:
        from libdyson.cloud import DysonAccount, DysonAccountCN
        email, password = (values.get("email") or "").strip(), values.get("password") or ""
        region = (values.get("region") or "US").upper()
        if not email or not password:
            raise ValueError("Enter your MyDyson email and password")
        code = (values.get("code") or "").strip()
        if not code:
            account = DysonAccountCN() if region == "CN" else DysonAccount()
            verify = await asyncio.to_thread(account.login_email_otp, email, region)
            _pending[email.lower()] = (time.time(), (account, verify))
            return {"need_code": True, "message": f"Dyson emailed a verification code to {email}. Enter it here."}
        started, (account, verify) = _pending.get(email.lower(), (0, (None, None)))
        if not verify or time.time() - started > 900:
            _pending.pop(email.lower(), None)
            raise ValueError("The code expired. Start the sign-in again.")
        await asyncio.to_thread(verify, code, password)
        devices = await asyncio.to_thread(account.devices)
        _pending.pop(email.lower(), None)
        saved = []
        for d in devices:
            try:
                dtype = d.get_device_type()
            except Exception:  # noqa: BLE001
                dtype = d.product_type
            saved.append({"serial": d.serial, "name": d.name, "product_type": d.product_type,
                          "device_type": dtype, "credential": d.credential})
        # only local device credentials are kept; the account password is not stored
        _save(data_dir, {"devices": saved, "region": region, "signed_in": time.time()})
        return {"ok": True, "devices": len(saved)}

    async def start(self):
        self.loop = asyncio.get_running_loop()
        creds = _load().get("devices") or []
        if not creds:
            self.status = "needs sign-in (Settings > Dyson)"
            return
        missing = []
        for c in creds:
            host = self.hosts.get(str(c["serial"]).upper())
            if not host:
                missing.append(c["serial"])
                continue
            self.spawn(f"dev_{c['serial']}", lambda c=c, host=host: self._run_device(c, host), max_delay=120)
        self.status = "ok" if len(missing) < len(creds) else f"add IP addresses for: {', '.join(missing)}"
        if missing:
            self.log.warning("Dyson devices without an IP (add SERIAL=IP): %s", ", ".join(missing))

    async def _run_device(self, c: dict, host: str):
        from libdyson import get_device
        dev = get_device(c["serial"], c["credential"], c["device_type"])
        if dev is None or not hasattr(dev, "turn_on") or not hasattr(dev, "speed"):
            self.log.info("Dyson %s (%s) isn't a purifier/fan; skipped", c["name"], c["product_type"])
            return
        did = self.dev_id(c["serial"])
        self.hub.upsert_device(Device(id=did, name=c.get("name") or c["serial"], source=self.source,
                                      model=f"Dyson {c.get('product_type', '')}".strip(),
                                      capabilities=purifier_caps(hasattr(dev, "enable_oscillation"))))
        self.hub.set_attributes(did, {"kind": "air_purifier", "host": host, "serial": c["serial"]})

        def on_message(_msg_type=None):
            if self.loop:
                self.loop.call_soon_threadsafe(lambda: self.hub.set_values(did, read_state(dev), online=True))

        await asyncio.to_thread(dev.connect, host)      # paho-mqtt runs its own network thread
        dev.add_message_listener(on_message)
        self.devs[did] = dev
        on_message()
        try:
            while True:
                await asyncio.sleep(60)
                if not dev.is_connected:
                    self.hub.set_values(did, {}, online=False)
                    raise ConnectionError(f"lost connection to {host}")
                await asyncio.to_thread(dev.request_environmental_data)
        finally:
            self.devs.pop(did, None)
            try:
                dev.remove_message_listener(on_message)
                await asyncio.to_thread(dev.disconnect)
            except Exception:  # noqa: BLE001
                pass

    async def command(self, device: Device, capability: str, value: Any):
        dev = self.devs.get(device.id)
        if dev is None:
            raise RuntimeError("device not connected")
        if capability == "switch":
            await asyncio.to_thread(dev.turn_on if value else dev.turn_off)
        elif capability == "fan_speed":
            await asyncio.to_thread(dev.set_speed, max(1, min(10, int(value))))
        elif capability == "oscillation":
            await asyncio.to_thread(dev.enable_oscillation if value else dev.disable_oscillation)
        elif capability == "mode":
            if value == "auto":
                await asyncio.to_thread(dev.enable_auto_mode)
            elif value == "night":
                await asyncio.to_thread(dev.enable_night_mode)
            elif value == "manual":
                await asyncio.to_thread(dev.disable_auto_mode)
                if _attr(dev, "night_mode"):
                    await asyncio.to_thread(dev.disable_night_mode)
            else:
                raise ValueError(f"unknown mode {value}")
        else:
            raise ValueError(capability)
        self.hub.set_values(device.id, {capability: value})


ADAPTER = DysonAdapter
SETTINGS = {
    "id": "dyson", "label": "Dyson", "icon": "air",
    "help": "Purifiers and purifier-fans, controlled over your local network. Choose Sign in and use your "
            "MyDyson account; Dyson emails you a code. Only each device's local key is saved, not your "
            "password. Then add every device as SERIAL=IP (serial from the sticker or MyDyson app, e.g. "
            "TP7-US-ABC1234A=192.168.1.60) and give it a fixed IP in your router.",
    "login": True,
    "login_fields": [
        {"key": "email", "label": "MyDyson email", "type": "text"},
        {"key": "password", "label": "MyDyson password", "type": "password"},
    ],
    "login_help": "Dyson will email you a verification code. Your password is only used for this sign-in.",
    "fields": [
        {"key": "region", "label": "Account country", "type": "select", "default": "US", "options": REGIONS},
        {"key": "hosts", "label": "Devices as SERIAL=IP (comma-separated)", "type": "list"},
    ],
}
