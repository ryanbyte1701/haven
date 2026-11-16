"""TP-Link Tapo smart plugs and power strips (P100, P105, P110, P110M, P115, P125M, P300, P304M, P306,
P316M, TP15/TP25…), controlled over your local network with python-kasa (the library behind Home
Assistant's TP-Link integration).

Only power products are added: plugs, outdoor plugs and power strips (each outlet of a strip
becomes its own device). Tapo bulbs, light strips, cameras, hubs, sensors and robot vacuums are
skipped, as are Kasa-branded devices.

Tapo firmware requires your Tapo account email and password even for local control; they are used
only to talk to the plugs on your network. Haven finds plugs by broadcast discovery, which only
reaches the LAN when the container uses host networking, so list the plugs' IP addresses (give
them fixed IPs in your router) when Haven runs on a Docker bridge network (the default).
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
from typing import Any

from ..models import Device, cap
from .base import Adapter

POWER_TYPES = {"Plug", "Strip", "StripSocket"}


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
        self.discovery = bool(cfg.get("discovery", True))
        self.poll_seconds = max(10, int(cfg.get("poll_seconds") or 30))
        self.parents: dict[str, Any] = {}     # host -> kasa Device (plug or strip)
        self.devs: dict[str, Any] = {}        # haven id -> kasa Device or child socket

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

    async def _find(self) -> None:
        """Connect to listed hosts and (optionally) discovered ones; keep only Tapo power products."""
        from kasa import Discover
        found: dict[str, Any] = {}
        if self.discovery:
            try:
                found.update(await Discover.discover(credentials=self._creds(), discovery_timeout=5))
            except Exception as e:  # noqa: BLE001
                self.log.info("tapo discovery failed: %s", e)
        for h in self.hosts:
            if h in found or h in self.parents:
                continue
            try:
                d = await Discover.discover_single(h, credentials=self._creds(), discovery_timeout=5)
                if d is not None:
                    found[h] = d
            except Exception as e:  # noqa: BLE001
                self.log.warning("tapo %s: %s", h, e)
                self.poll_failed(f"host:{h}", e, [f"host:{h}" for h in self.hosts] or None)
        for host, d in found.items():
            if host in self.parents:
                continue
            try:
                await d.update()
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "auth" in msg.lower() or "credential" in msg.lower():
                    self.status = "error: Tapo rejected the email/password"
                self.log.warning("tapo %s: %s", host, e)
                await _quiet_close(d)
                continue
            if not is_tapo_power(d):
                self.log.info("tapo: skipping %s (%s, not a plug or strip)", d.alias, d.model)
                await _quiet_close(d)
                continue
            self.parents[host] = d
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
            for host, d in list(self.parents.items()):
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
            missing = [h for h in self.hosts if h not in self.parents]
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
            "IP in your router and list them below; discovery alone only works with host networking. "
            "Power, today's and this month's energy are shown for models with energy monitoring "
            "(P110, P115, P304M…).",
    "fields": [
        {"key": "username", "label": "Tapo email", "type": "text"},
        {"key": "password", "label": "Tapo password", "type": "password"},
        {"key": "hosts", "label": "Plug IP addresses (comma-separated)", "type": "text"},
        {"key": "discovery", "label": "Also look for plugs automatically", "type": "bool", "default": True},
        {"key": "poll_seconds", "label": "Refresh every (seconds)", "type": "number", "default": 30},
    ],
}
