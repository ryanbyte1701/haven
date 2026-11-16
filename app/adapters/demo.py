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


# Newer device kinds (pool, water monitor, irrigation, air, EV, vacuum): (id, name, room, src, kind, caps{name: kwargs}, values)
APPLIANCES = [
    ("pool", "Pool", "Backyard", "waterguru", "pool",
     {"ph": {}, "free_chlorine": {"unit": "ppm"}, "orp": {"unit": "mV"}, "temperature": {}, "cartridge_life": {"unit": "%"}, "battery": {}, "status_text": {}},
     {"ph": 7.4, "free_chlorine": 2.1, "orp": 690, "temperature": 26.5, "cartridge_life": 72, "battery": 90, "status_text": "In range"}),
    ("flo", "Main Water Line", "Garage", "flo", "water_monitor",
     {"valve": {}, "flow_rate": {"unit": "gpm"}, "water_pressure": {"unit": "psi"}, "temperature": {}, "water_usage_today": {"unit": "gal"},
      "mode": {"writable": True, "options": ["home", "away", "sleep"]}},
     {"valve": "open", "flow_rate": 0.0, "water_pressure": 62, "temperature": 21.3, "water_usage_today": 84, "mode": "home"}),
    ("zone_front", "Front Lawn", "Outdoor", "rachio", "irrigation_zone",
     {"watering": {}, "action": {"options": ["run_5m", "run_10m", "stop"]}}, {"watering": False}),
    ("awair", "Office Air", "Office", "awair", "air_monitor",
     {"air_score": {"label": "Air score"}, "temperature": {}, "humidity": {}, "co2": {}, "voc": {"unit": "ppb"}, "pm25": {}, "status_text": {}},
     {"air_score": 88, "temperature": 22.8, "humidity": 34, "co2": 640, "voc": 210, "pm25": 4, "status_text": "Good"}),
    ("dyson", "Bedroom Purifier", "Bedroom", "dyson", "air_purifier",
     {"switch": {}, "fan_speed": {"writable": True, "min": 1, "max": 10, "step": 1}, "mode": {"writable": True, "options": ["auto", "manual", "sleep"]},
      "pm25": {}, "filter_life": {"unit": "%"}},
     {"switch": True, "fan_speed": 4, "mode": "auto", "pm25": 6, "filter_life": 81}),
    ("charger", "Garage Charger", "Garage", "chargepoint", "ev_charger",
     {"charger_status": {}, "power": {}, "energy_session": {"unit": "kWh"}, "charge_time": {"unit": "min"},
      "amperage_limit": {"writable": True, "min": 16, "max": 48, "step": 4, "unit": "A"}, "action": {"options": ["start", "stop"]}},
     {"charger_status": "charging", "power": 7200, "energy_session": 12.4, "charge_time": 104, "amperage_limit": 32}),
    ("roborock", "Downstairs Vacuum", "Living Room", "roborock", "vacuum",
     {"vacuum_state": {}, "battery": {}, "action": {"options": ["start", "pause", "dock", "locate"]},
      "fan_speed": {"writable": True, "options": ["quiet", "balanced", "turbo", "max"]}, "clean_area": {"unit": "m²"}, "filter_life": {"unit": "%"}},
     {"vacuum_state": "docked", "battery": 100, "fan_speed": "balanced", "clean_area": 42, "filter_life": 64}),
    ("plug_desk", "Desk Plug", "Office", "tapo", "smart_plug",
     {"switch": {}, "power": {"label": "Using"}, "energy_today": {"unit": "kWh", "label": "Today"}, "energy_month": {"unit": "kWh", "label": "This month"}},
     {"switch": True, "power": 86.4, "energy_today": 0.71, "energy_month": 18.2}),
    ("plug_heater", "Space Heater", "Bedroom", "tapo", "smart_plug",
     {"switch": {}, "power": {"label": "Using"}, "energy_today": {"unit": "kWh", "label": "Today"}, "energy_month": {"unit": "kWh", "label": "This month"}},
     {"switch": False, "power": 0.0, "energy_today": 1.9, "energy_month": 41.5}),
    ("plug_rack", "Network Rack", "Network Closet", "tapo", "smart_plug",
     {"switch": {}, "power": {"label": "Using"}, "energy_today": {"unit": "kWh", "label": "Today"}, "energy_month": {"unit": "kWh", "label": "This month"}},
     {"switch": True, "power": 142.0, "energy_today": 3.12, "energy_month": 96.7}),
]


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
        for nid, name, room, src, kind, caps, vals in APPLIANCES:
            did = self.dev_id(nid)
            self.hub.upsert_device(Device(id=did, name=name, source=self.source, model=f"demo ({src})", room=room,
                                          capabilities={c: cap(c, **kw) for c, kw in caps.items()}))
            self.hub.set_attributes(did, {"kind": kind})
            self.hub.set_values(did, vals)
        # outdoor AQI station (like the Outdoor air quality integration)
        import time as _t
        sid = self.dev_id("outdoor_aqi")
        aq_caps = {"aqi": {"label": "AQI"}, "pm25": {}, "pm10": {"unit": "µg/m³"}, "ozone": {"label": "Ozone", "unit": "µg/m³"},
                   "no2": {"label": "NO₂", "unit": "µg/m³"}, "so2": {"label": "SO₂", "unit": "µg/m³"}, "co": {"label": "CO", "unit": "µg/m³"},
                   "uv": {"label": "UV index"}, "dust": {"label": "Dust", "unit": "µg/m³"}, "status_text": {}}
        self.hub.upsert_device(Device(id=sid, name="Outdoor air (Phoenix)", source=self.source, model="demo (Open-Meteo)", room="Outdoor",
                                      capabilities={c: cap(c, **kw) for c, kw in aq_caps.items()}))
        hour = int(_t.time() // 3600 * 3600)
        fc = [{"t": hour + i * 3600, "aqi": int(48 + 30 * __import__("math").sin((i + 3) / 4) + (i % 5))} for i in range(0, 25)]
        self.hub.set_attributes(sid, {"kind": "aqi_station", "provider": "Open-Meteo", "place": "Phoenix, AZ", "dominant": "Ozone",
                                      "observed": _t.time() - 900, "forecast": fc})
        self.hub.set_values(sid, {"aqi": 64, "pm25": 9.8, "pm10": 31.2, "ozone": 128.0, "no2": 18.4, "so2": 2.1, "co": 312.0, "uv": 4.1,
                                  "dust": 22.0, "status_text": "Moderate"})
        # local weather + forecast (like the Weather forecast integration)
        wid = self.dev_id("weather")
        wcaps = {"temperature": {}, "feels_like": {"unit": "°C", "label": "Feels like"}, "humidity": {}, "wind_speed": {"unit": "mph", "label": "Wind"},
                 "wind_gust": {"unit": "mph", "label": "Gust"}, "uv": {"label": "UV"}, "pressure": {"unit": "hPa"}, "precipitation": {"unit": "in", "label": "Rain now"},
                 "status_text": {"label": "Conditions"}}
        self.hub.upsert_device(Device(id=wid, name="Weather (Phoenix, AZ)", source=self.source, model="demo (Open-Meteo)", room="Outdoor",
                                      capabilities={c: cap(c, **kw) for c, kw in wcaps.items()}))
        day0 = int(_t.time() // 86400 * 86400) + 7 * 3600
        days = [("Sunny", "clear", 31, 17, 0), ("Partly cloudy", "partly", 29, 16, 5), ("Showers", "rain", 24, 15, 60),
                ("Cloudy", "cloudy", 25, 14, 20), ("Sunny", "clear", 28, 15, 0), ("Mostly clear", "clear", 30, 16, 0)]
        self.hub.set_attributes(wid, {"kind": "weather", "place": "Phoenix, AZ", "icon": "partly", "is_day": True, "wind_dir": 250,
                                      "units": {"wind": "mph", "rain": "in"},
                                      "daily": [{"t": day0 + i * 86400, "text": t, "icon": ic, "hi": hi, "lo": lo, "pop": pop,
                                                 "sunrise": day0 + i * 86400 - 6 * 3600 + 1500, "sunset": day0 + i * 86400 + 4 * 3600 + 1200}
                                                for i, (t, ic, hi, lo, pop) in enumerate(days)]})
        self.hub.set_values(wid, {"temperature": 29.4, "feels_like": 28.1, "humidity": 14, "wind_speed": 6.0, "wind_gust": 13.0, "uv": 6.2,
                                  "pressure": 1012.0, "precipitation": 0.0, "status_text": "Partly cloudy"})
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
                    # walk backwards from the live reading so the seeded curve ends where the card is now
                    v, pts = c.value, []
                    for i in range(1, 289):
                        v += random.uniform(-0.25, 0.25) if c.name == "temperature" else random.uniform(-0.6, 0.6)
                        pts.append((now - i * 300, round(v, 2)))
                    buf.extend(reversed(pts))
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
        if capability == "action":
            effects = {"start": {"vacuum_state": "cleaning"} if device.attributes.get("kind") == "vacuum" else {"charger_status": "charging"},
                       "pause": {"vacuum_state": "paused"}, "dock": {"vacuum_state": "returning"},
                       "stop": {"watering": False} if device.attributes.get("kind") == "irrigation_zone" else {"charger_status": "plugged_in", "power": 0},
                       "run_5m": {"watering": True}, "run_10m": {"watering": True}}
            if value in effects:
                self.hub.set_values(device.id, effects[value])
            return
        state = {"lock": "locked", "unlock": "unlocked", "open": "open", "close": "closed"}.get(value, value)
        if capability != "action":
            self.hub.set_values(device.id, {capability: state})
