"""Fake devices so you can try the dashboard before wiring real credentials."""
from __future__ import annotations

import asyncio
import random
from typing import Any

from ..models import Device, cap
from .base import Adapter

DEVICES = [
    ("living_th", "Living Room", "Living Room", ["temperature", "humidity", "battery"], {"temperature": 22.4, "humidity": 31, "battery": 100}),
    ("bed_th", "Primary Bedroom", "Bedroom", ["temperature", "humidity", "battery"], {"temperature": 21.1, "humidity": 34, "battery": 75}),
    ("outdoor", "Backyard Weather", "Outdoor", ["temperature", "humidity", "wind_speed", "rain_daily"], {"temperature": 29.5, "humidity": 12, "wind_speed": 4.2, "rain_daily": 0.0}),
    ("garden_soil", "Garden Bed Soil", "Outdoor", ["moisture", "battery"], {"moisture": 38, "battery": 100}),
    ("front_door", "Front Door", "Entry", ["contact", "battery"], {"contact": "closed", "battery": 100}),
    ("front_lock", "Front Door Lock", "Entry", ["lock", "battery"], {"lock": "locked", "battery": 80}),
    ("garage_door", "Garage Side Door", "Garage", ["contact", "battery"], {"contact": "open", "battery": 25}),
    ("water_heater_leak", "Water Heater Leak", "Garage", ["leak", "battery"], {"leak": False, "battery": 100}),
    ("sink_leak", "Kitchen Sink Leak", "Kitchen", ["leak", "battery"], {"leak": False, "battery": 100}),
    ("main_valve", "Main Water Valve", "Garage", ["valve"], {"valve": "open"}),
    ("drip_valve", "Drip Irrigation", "Outdoor", ["valve"], {"valve": "closed"}),
    ("office_presence", "Office Presence", "Office", ["presence", "illuminance"], {"presence": True, "illuminance": 240}),
    ("hall_smoke", "Hallway Smoke/CO", "Hallway", ["smoke", "co", "co_ppm", "battery", "action"], {"smoke": False, "co": False, "co_ppm": 0, "battery": 100}),
    ("patio_lights", "Patio Lights", "Outdoor", ["switch"], {"switch": False}),
]
# (id, name, room, caps, values, attributes)
LIGHTS = [
    ("lr_lamp", "Floor Lamp", "Living Room", ["light", "brightness", "color_temp", "color"],
     {"light": True, "brightness": 70, "color_temp": 2700, "color": "#ffb46b"}, {"kind": "light", "ct_min": 2000, "ct_max": 6535}),
    ("lr_ceiling", "Ceiling Lights", "Living Room", ["light", "brightness", "color_temp"],
     {"light": True, "brightness": 45, "color_temp": 3000}, {"kind": "light", "ct_min": 2200, "ct_max": 6500}),
    ("porch", "Porch Light", "Entry", ["light", "brightness"], {"light": False, "brightness": 100}, {"kind": "light"}),
]
LIGHT_GROUP = ("lr_group", "Living Room lights", "Living Room", ["light", "brightness", "color_temp", "color"],
               {"kind": "group", "group_type": "room", "ct_min": 2200, "ct_max": 6500})
SOURCES = ["yolink", "yolink", "ecowitt", "ecowitt", "aqara", "yolink", "aqara", "yolink", "mqtt", "yolink", "mqtt", "aqara", "xsense", "mqtt"]


class DemoAdapter(Adapter):
    source = "demo"

    async def start(self):
        for (nid, name, room, caps, vals), src in zip(DEVICES, SOURCES):
            capd = {c: cap(c, options=["test", "mute"]) if c == "action" else cap(c) for c in caps}
            if "rain_daily" in capd:
                capd["rain_daily"].unit = "in"
            if "wind_speed" in capd:
                capd["wind_speed"].unit = "mph"
            self.hub.upsert_device(Device(id=self.dev_id(nid), name=name, source=self.source,
                                          model=f"demo ({src})", room=room, capabilities=capd))
            self.hub.set_values(self.dev_id(nid), vals)
        for nid, name, room, caps, vals, attrs in LIGHTS:
            did = self.dev_id(nid)
            self.hub.upsert_device(Device(id=did, name=name, source=self.source, model="demo (hue)", room=room,
                                          capabilities={c: cap(c) for c in caps}))
            self.hub.set_attributes(did, attrs)
            self.hub.set_values(did, vals)
        gid, gname, groom, gcaps, gattrs = LIGHT_GROUP
        members = [self.dev_id(l[0]) for l in LIGHTS if l[2] == groom]
        self.hub.upsert_device(Device(id=self.dev_id(gid), name=gname, source=self.source, model="demo (hue)",
                                      room=groom, capabilities={c: cap(c) for c in gcaps}))
        self.hub.set_attributes(self.dev_id(gid), {**gattrs, "members": members})
        self._sync_group()
        # seed 24h of history so sparklines have something to draw
        import time
        now = time.time()
        for dev in self.hub.devices.values():
            for c in dev.capabilities.values():
                if c.name in ("temperature", "humidity", "moisture") and isinstance(c.value, (int, float)):
                    buf = self.hub.history.setdefault((dev.id, c.name), __import__("collections").deque(maxlen=288))
                    v = c.value
                    for i in range(288, 0, -1):
                        v += random.uniform(-0.25, 0.25) if c.name == "temperature" else random.uniform(-0.6, 0.6)
                        buf.append((now - i * 300, round(v, 2)))
        self.status = "ok"
        self.spawn("wiggle", self._wiggle)

    async def _wiggle(self):
        while True:
            await asyncio.sleep(5)
            for dev in list(self.hub.devices.values()):
                if dev.source != self.source:
                    continue
                upd = {}
                for c in dev.capabilities.values():
                    if c.name in ("temperature", "humidity", "moisture") and isinstance(c.value, (int, float)):
                        upd[c.name] = round(c.value + random.uniform(-0.15, 0.15), 2)
                if upd:
                    self.hub.set_values(dev.id, upd)

    def _sync_group(self):
        g = self.hub.devices.get(self.dev_id(LIGHT_GROUP[0]))
        if not g:
            return
        mem = [self.hub.devices[m] for m in g.attributes.get("members", []) if m in self.hub.devices]
        on = [m for m in mem if m.capabilities["light"].value]
        vals = {"light": bool(on)}
        if on:
            vals["brightness"] = round(sum(m.capabilities["brightness"].value or 0 for m in on) / len(on))
            lead = on[0].capabilities
            for k in ("color_temp", "color"):
                if k in lead and lead[k].value is not None:
                    vals[k] = lead[k].value
        self.hub.set_values(g.id, vals)

    async def command(self, device: Device, capability: str, value: Any):
        await asyncio.sleep(0.4)
        if capability in ("light", "brightness", "color_temp", "color"):
            targets = [device.id]
            if device.attributes.get("kind") == "group":
                targets = [m for m in device.attributes.get("members", []) if m in self.hub.devices]
            for t in targets:
                caps = self.hub.devices[t].capabilities
                vals: dict[str, Any] = {"light": bool(value) if capability == "light" else True}
                if capability != "light" and capability in caps:
                    vals[capability] = value if capability != "brightness" else int(value)
                self.hub.set_values(t, vals)
            if device.attributes.get("kind") == "group":
                self._sync_group()
                self.hub.set_values(device.id, {capability: value if capability != "light" else bool(value)})
            else:
                self._sync_group()
            return
        state = {"lock": "locked", "unlock": "unlocked", "open": "open", "close": "closed"}.get(value, value)
        if capability != "action":
            self.hub.set_values(device.id, {capability: state})
