"""X-Sense cloud (smoke / CO / heat / water-leak / temp-humidity / door / motion) via the
community python-xsense library — the same one the Home Assistant integration uses.

X-Sense only allows one active login per account. Create a second X-Sense account,
share your devices to it from the main app, and use that account here so your phone
doesn't get logged out.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..models import Device, cap
from .base import Adapter

# xsense EntityType value -> capabilities
KIND_CAPS = {
    "smoke": ["smoke", "battery", "action"],
    "co": ["co", "co_ppm", "battery", "action"],
    "combi": ["smoke", "co", "co_ppm", "battery", "action"],
    "heat": ["heat", "battery", "action"],
    "water": ["leak", "battery", "action"],
    "temperature": ["temperature", "humidity", "battery"],
    "door": ["contact", "battery"],
    "motion": ["motion", "battery"],
    "mailbox": ["contact", "battery"],
}


def normalize(kind: str, d: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "temperature" in d:
        t = d["temperature"]
        out["temperature"] = round((t - 32) * 5 / 9, 2) if str(d.get("temperatureUnit")) == "1" else t
    if "humidity" in d:
        out["humidity"] = d["humidity"]
    if "batInfo" in d:                      # 0-3 bars
        out["battery"] = min(100, int(d["batInfo"]) * 100 // 3)
    if "coPpm" in d:
        out["co_ppm"] = d["coPpm"]
    alarm = bool(d.get("alarmStatus"))
    if kind == "smoke":
        out["smoke"] = alarm
    elif kind == "co":
        out["co"] = alarm
    elif kind == "combi":   # combo alarm reports one alarm flag; CO ppm tells which
        co_high = int(d.get("coPpm") or 0) >= 50
        out["co"] = alarm and co_high
        out["smoke"] = alarm and not co_high
    if kind == "heat":
        out["heat"] = alarm
    if kind == "water":
        out["leak"] = alarm
    if kind in ("door", "mailbox") and "isOpen" in d:
        out["contact"] = "open" if str(d["isOpen"]) == "1" else "closed"
    if kind == "motion":
        out["motion"] = alarm
    return out


class XSenseAdapter(Adapter):
    source = "xsense"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.username, self.password = cfg["username"], cfg["password"]
        self.poll_seconds = int(cfg.get("poll_seconds", 120))
        self.api = None
        self.entities: dict[str, Any] = {}    # our device id -> xsense Device/Station

    async def start(self):
        self.spawn("poll", self._poll_loop)

    async def _login(self):
        from xsense import AsyncXSense
        self.api = AsyncXSense()
        await self.api.init()
        await self.api.login(self.username, self.password)
        await self.api.load_all()

    def _register(self, ent, room_names: dict):
        kind = getattr(ent.entity_type, "value", None)
        caps = KIND_CAPS.get(kind)
        if not caps:
            return None
        did = self.dev_id(ent.sn)
        if did not in self.entities:
            from xsense.entity_map import entities as defs
            actions = [a["action"] for a in defs.get(ent.type, {}).get("actions", [])]
            capd = {c: cap(c, options=actions or ["test"]) if c == "action" else cap(c)
                    for c in caps if c != "action" or actions}
            self.hub.upsert_device(Device(id=did, name=ent.name or ent.sn, source=self.source,
                                          model=ent.type, room=room_names.get(ent.room_id),
                                          capabilities=capd))
            self.entities[did] = ent
        return did, kind

    async def _poll_loop(self):
        await self._login()
        self.status = "ok"
        while True:
            for house in self.api.houses.values():
                rooms = {rid: r.get("roomName") if isinstance(r, dict) else getattr(r, "name", None)
                         for rid, r in (getattr(house, "rooms", {}) or {}).items()}
                for station in house.stations.values():
                    try:
                        await self.api.get_state(station)
                    except Exception as e:  # noqa: BLE001
                        if "expired" in str(e).lower():
                            await self.api.refresh()
                        self.log.debug("state %s: %s", station.name, e)
                        continue
                    for ent in [station, *station.devices.values()]:
                        reg = self._register(ent, rooms)
                        if reg:
                            did, kind = reg
                            self.hub.set_values(did, normalize(kind, ent.data),
                                                online=ent.online if ent.online is not None else True)
            await asyncio.sleep(self.poll_seconds)

    async def command(self, device: Device, capability: str, value: Any):
        ent = self.entities[device.id]
        await self.api.action(ent, value)       # "test", "mute", "firedrill"
