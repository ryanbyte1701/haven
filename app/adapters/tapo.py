"""TP-Link Tapo smart plugs and power strips (P100, P105, P110, P110M, P115, P125M, P300, P304M, P306,
P316M, TP15/TP25…), controlled over your local network with python-kasa (the library behind Home
Assistant's TP-Link integration).

Only power products are added: plugs, outdoor plugs and power strips (each outlet of a strip
becomes its own device). Tapo bulbs, light strips, cameras, hubs, sensors and robot vacuums are
skipped, as are Kasa-branded devices.

Tapo firmware requires your Tapo account email and password even for local control; they are used
only to talk to the plugs on your network. List the plugs' IP addresses (give them fixed IPs in your
router). Broadcast discovery is off by default: it only reaches the LAN with host networking, and any
device answering it would be sent a login. When it is on, Haven only logs in to devices whose
(unauthenticated) discovery reply says they are Tapo plugs using KLAP, and remembers devices it skipped.
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
import time
from typing import Any

from ..models import Device, cap
from .base import Adapter

POWER_TYPES = {"Plug", "Strip", "StripSocket"}
SKIP_SECONDS = 6 * 3600       # a device found not to be a Tapo plug isn't contacted again for this long


def _family(dev) -> str:
    try:
        return str(getattr(dev.config.connection_type.device_family, "value", "")).upper()
    except AttributeError:
        return ""


def _encryption(dev) -> str:
    try:
        return str(getattr(dev.config.connection_type.encryption_type, "value", "")).upper()
    except AttributeError:
        return ""


def may_log_in(dev, listed: bool) -> bool:
    """Before sending the Tapo email/password (update()): the unauthenticated discovery reply must say this is a
    Tapo plug/strip. Discovered (not listed) devices must also use KLAP, which never sends the password itself."""
    if _family(dev) != "SMART.TAPOPLUG":
        return False
    return listed or _encryption(dev) == "KLAP"


def is_tapo_power(dev) -> bool:
    """True for Tapo plugs/strips (not bulbs, cameras, hubs, sensors, vacuums or Kasa devices)."""
    try:
        family = str(getattr(dev.config.connection_type.device_family, "value", "")).upper()
    except AttributeError:
        family = ""
    dtype = getattr(getattr(dev, "device_type", None), "name", "")
    return family == "SMART.TAPOPLUG" and dtype in POWER_TYPES


def _num(v: Any, nd: int = 2) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def read_energy(dev) -> dict[str, Any]:
    from kasa import Module
    em = dev.modules.get(Module.Energy) if hasattr(dev, "modules") else None
    if em is None:
        return {}
    out: dict[str, Any] = {}
    for key, attr, nd in (("power", "current_consumption", 1), ("energy_today", "consumption_today", 3),
                          ("energy_month", "consumption_this_month", 2), ("voltage", "voltage", 1),
                          ("current", "current", 3)):
        try:
            v = _num(getattr(em, attr, None), nd)
        except Exception:  # noqa: BLE001  (a module may not support every reading)
            v = None
        if v is not None:
            out[key] = v
    return out


def plug_caps(dev, energy: dict) -> dict:
    caps = {"switch": cap("switch", label="Power")}
    if "power" in energy:
        caps["power"] = cap("power", label="Using", unit="W")
    if "energy_today" in energy:
        caps["energy_today"] = cap("energy_today", label="Today", unit="kWh")
    if "energy_month" in energy:
        caps["energy_month"] = cap("energy_month", label="This month", unit="kWh")
    if "voltage" in energy:
        caps["voltage"] = cap("voltage", label="Voltage", unit="V")
    if "current" in energy:
        caps["current"] = cap("current", label="Current", unit="A")
    return caps


def parse_hosts(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else str(raw or "").replace(";", ",").replace("\n", ",").split(",")
    out = []
    for h in (str(x).strip() for x in items):
        if not h:
            continue
        try:
            ipaddress.ip_address(h)
        except ValueError:
            # hostnames are fine too, but nothing that could be a URL or path
            if not all(c.isalnum() or c in "-." for c in h) or len(h) > 253:
                continue
        out.append(h)
    return out[:64]


class TapoAdapter(Adapter):
    source = "tapo"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.username = cfg.get("username") or ""
        self.password = cfg.get("password") or ""
        self.hosts = parse_hosts(cfg.get("hosts"))
        self.discovery = bool(cfg.get("discovery", False))
        self.poll_seconds = max(10, int(cfg.get("poll_seconds") or 30))
        self.parents: dict[str, Any] = {}     # IP -> kasa Device (plug or strip)
        self.devs: dict[str, Any] = {}        # haven id -> kasa Device or child socket
        self.skipped: dict[str, float] = {}   # IP -> when it was found not to be a Tapo plug
        self.host_ips: dict[str, str] = {}    # configured host -> resolved IP

    async def start(self):
        if not self.username or not self.password:
            self.status = "enter your Tapo email and password"
            return
        if not self.hosts and not self.discovery:
            self.status = "enter the plugs' IP addresses (or turn on discovery)"
            return
        self.spawn("poll", self._loop, max_delay=120)

    async def stop(self):
        await super().stop()
        for d in list(self.parents.values()):
            try:
                await d.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.parents.clear()

    def _creds(self):
        from kasa import Credentials
        return Credentials(self.username, self.password)

    async def _resolve(self, h: str) -> str:
        """Configured host -> IP, so a plug listed by name and also found by discovery is used once."""
        try:
            ipaddress.ip_address(h)
            return h
        except ValueError:
            pass
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(h, None, family=socket.AF_INET, type=socket.SOCK_DGRAM)
            ip = infos[0][4][0]
        except (OSError, IndexError):
            return self.host_ips.get(h, h)
        self.host_ips[h] = ip
        return ip

    def _skip(self, ip: str, why: str) -> None:
        if ip not in self.skipped:
            self.log.info("tapo: skipping %s (%s)", ip, why)
        self.skipped[ip] = time.monotonic()

    def _is_skipped(self, ip: str) -> bool:
        t = self.skipped.get(ip)
        if t is None:
            return False
        if time.monotonic() - t > SKIP_SECONDS:
            self.skipped.pop(ip, None)
            return False
        return True

    async def _find(self) -> None:
        """Connect to listed hosts and (optionally) discovered ones; keep only Tapo power products. Nothing is
        sent the Tapo login unless its discovery reply says it is a Tapo plug (see may_log_in)."""
        from kasa import Discover
        listed: dict[str, str] = {}                # IP -> configured host
        for h in self.hosts:
            listed.setdefault(await self._resolve(h), h)
        found: dict[str, Any] = {}
        if self.discovery:
            try:
                found.update(await Discover.discover(credentials=self._creds(), discovery_timeout=5))
            except Exception as e:  # noqa: BLE001
                self.log.info("tapo discovery failed: %s", e)
        for ip, h in listed.items():
            if ip in found or ip in self.parents or self._is_skipped(ip):
                continue
            try:
                d = await Discover.discover_single(ip, credentials=self._creds(), discovery_timeout=5)
                if d is not None:
                    found[ip] = d
            except Exception as e:  # noqa: BLE001
                self.log.warning("tapo %s: %s", h, e)
                self.poll_failed(f"host:{h}", e, [f"host:{x}" for x in self.hosts] or None)
        for ip, d in found.items():
            if ip in self.parents or self._is_skipped(ip):
                if ip not in self.parents:
                    await _quiet_close(d)
                continue
            if not may_log_in(d, listed=ip in listed):
                self._skip(ip, f"{_family(d) or 'unknown device'}/{_encryption(d) or '?'}, not a Tapo plug using KLAP")
                await _quiet_close(d)
                continue
            try:
                await d.update()
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "auth" in msg.lower() or "credential" in msg.lower():
                    self.status = "error: Tapo rejected the email/password"
                self.log.warning("tapo %s: %s", ip, e)
                await _quiet_close(d)
                continue
            if not is_tapo_power(d):
                self._skip(ip, f"{d.alias}, {d.model}: not a plug or strip")
                await _quiet_close(d)
                continue
            self.parents[ip] = d
            self._register(d)

    def _register(self, d) -> None:
        targets = list(d.children) if getattr(d, "children", None) else [d]
        strip_energy = read_energy(d)
        for t in targets:
            did = self.dev_id(t.device_id or f"{d.host}-{t.alias}")
            energy = read_energy(t) or ({} if t is not d else strip_energy)
            self.devs[did] = t
            model = f"Tapo {d.model}" + (" outlet" if t is not d else "")
            self.hub.upsert_device(Device(id=did, name=t.alias or d.alias or d.model, source=self.source,
                                          model=model, capabilities=plug_caps(t, energy)))
            self.hub.set_attributes(did, {"kind": "smart_plug", "host": d.host})
            self.hub.set_values(did, {"switch": bool(t.is_on), **energy}, online=True)

    async def _loop(self):
        await self._find()
        n = len(self.devs)
        if not n and not self.status.startswith("error"):
            self.status = "no Tapo plugs found yet" + ("" if self.hosts else " (add their IP addresses)")
        elif n:
            self.status = "ok"
        rescan = 0
        while True:
            await asyncio.sleep(self.poll_seconds)
            for d in list(self.parents.values()):
                ids = [i for i, t in self.devs.items() if t is d or getattr(t, "parent", None) is d]
                try:
                    await d.update()
                    for did in ids:
                        t = self.devs[did]
                        energy = read_energy(t) if t is not d or not d.children else {}
                        self.hub.set_values(did, {"switch": bool(t.is_on), **energy}, online=True)
                        self.poll_ok(did)
                except Exception as e:  # noqa: BLE001
                    for did in ids:
                        self.poll_failed(did, e, list(self.devs))
            rescan += self.poll_seconds
            missing = [h for h in self.hosts if self.host_ips.get(h, h) not in self.parents
                       and not self._is_skipped(self.host_ips.get(h, h))]
            if rescan >= 600 or (missing and rescan >= 120):   # pick up new or late plugs
                rescan = 0
                before = len(self.devs)
                await self._find()
                if len(self.devs) != before and self.devs:
                    self.status = "ok"

    async def command(self, device: Device, capability: str, value: Any):
        t = self.devs.get(device.id)
        if t is None:
            raise RuntimeError("device not found")
        if capability != "switch":
            raise ValueError(capability)
        await (t.turn_on() if value else t.turn_off())
        self.hub.set_values(device.id, {"switch": bool(value)})


async def _quiet_close(d) -> None:
    try:
        await d.disconnect()
    except Exception:  # noqa: BLE001
        pass


ADAPTER = TapoAdapter
SETTINGS = {
    "id": "tapo", "label": "TP-Link Tapo plugs", "icon": "outlet",
    "help": "Tapo smart plugs and power strips (P100, P105, P110, P115, P125M, P300, P304M, P306, P316M, "
            "TP25…), controlled on your local network. Bulbs, cameras and sensors are not added. Tapo needs "
            "the email and password of your Tapo app account even for local control. Give each plug a fixed "
            "IP in your router and list them below. Automatic discovery (off by default) only works with host "
            "networking, and only logs in to devices that say they are Tapo plugs. "
            "Power, today's and this month's energy are shown for models with energy monitoring "
            "(P110, P115, P304M…).",
    "fields": [
        {"key": "username", "label": "Tapo email", "type": "text"},
        {"key": "password", "label": "Tapo password", "type": "password"},
        {"key": "hosts", "label": "Plugs", "type": "hostlist", "item_label": "Plug IP address", "add_label": "Add plug",
         "help": "One plug or power strip per row, e.g. 192.168.1.61."},
        {"key": "discovery", "label": "Also look for plugs automatically (host networking only)", "type": "bool",
         "default": False},
        {"key": "poll_seconds", "label": "Refresh every (seconds)", "type": "number", "default": 30},
    ],
}
