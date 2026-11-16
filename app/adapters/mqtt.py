"""Generic MQTT adapter.

1. Auto-discovers anything that publishes Home Assistant MQTT discovery configs
   (Zigbee2MQTT, Z-Wave JS UI, ESPHome, Tasmota, Shelly, rtl_433, Frigate, ...).
2. Manual devices for arbitrary topics, declared in config.yaml.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

import aiomqtt

from ..color import hex_to_rgb, hex_to_xy, hs_to_hex, kelvin_to_mirek, mirek_to_kelvin, rgb_to_hex, xy_to_hex
from ..models import Device, cap, f_to_c
from .base import Adapter

ABBREV = {
    "stat_t": "state_topic", "cmd_t": "command_topic", "dev_cla": "device_class",
    "val_tpl": "value_template", "unit_of_meas": "unit_of_measurement", "uniq_id": "unique_id",
    "dev": "device", "ids": "identifiers", "mdl": "model", "mf": "manufacturer",
    "pl_on": "payload_on", "pl_off": "payload_off", "pl_lock": "payload_lock",
    "pl_unlk": "payload_unlock", "pl_open": "payload_open", "pl_cls": "payload_close",
    "stat_locked": "state_locked", "stat_unlocked": "state_unlocked", "stat_open": "state_open",
    "stat_clsd": "state_closed", "stat_on": "state_on", "stat_off": "state_off",
    "sug_area": "suggested_area", "obj_id": "object_id",
    "bri_stat_t": "brightness_state_topic", "bri_cmd_t": "brightness_command_topic", "bri_scl": "brightness_scale",
    "clr_temp_stat_t": "color_temp_state_topic", "clr_temp_cmd_t": "color_temp_command_topic",
    "rgb_stat_t": "rgb_state_topic", "rgb_cmd_t": "rgb_command_topic", "xy_stat_t": "xy_state_topic",
    "xy_cmd_t": "xy_command_topic", "hs_stat_t": "hs_state_topic", "hs_cmd_t": "hs_command_topic",
    "sup_clrm": "supported_color_modes", "min_mirs": "min_mireds", "max_mirs": "max_mireds",
    "min_klv": "min_kelvin", "max_klv": "max_kelvin", "clr_temp_klv": "color_temp_kelvin",
    "on_cmd_type": "on_command_type",
    "pl_disarm": "payload_disarm", "pl_arm_home": "payload_arm_home", "pl_arm_away": "payload_arm_away",
}

SENSOR_CLASSES = {
    "temperature": "temperature", "humidity": "humidity", "moisture": "moisture",
    "battery": "battery", "pressure": "pressure", "illuminance": "illuminance",
    "carbon_dioxide": "co2", "pm25": "pm25", "power": "power", "carbon_monoxide": "co_ppm",
}
BINARY_CLASSES = {
    "door": "contact", "window": "contact", "opening": "contact", "garage_door": "contact",
    "moisture": "leak", "occupancy": "presence", "presence": "presence", "motion": "motion",
    "smoke": "smoke", "carbon_monoxide": "co", "heat": "heat", "battery": "battery_low",
    "vibration": "vibration", "tamper": "tamper",
}


@dataclass
class Entity:
    device_id: str
    cap: str
    kind: str                     # sensor | binary | lock | valve | switch | alarm | light_*
    state_topic: str | None = None
    command_topic: str | None = None
    path: list[str] = field(default_factory=list)   # JSON path to the value
    unit: str | None = None
    cfg: dict = field(default_factory=dict)

    def parse(self, payload: bytes) -> Any:
        raw: Any = payload.decode(errors="replace").strip()
        if self.path:
            try:
                raw = json.loads(raw)
                for p in self.path:
                    raw = raw[p] if isinstance(raw, dict) else raw[int(p)]
            except Exception:  # noqa: BLE001
                return None
        c = self.cfg
        s = str(raw).lower()
        if self.kind == "sensor":
            try:
                v = float(raw)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(v):
                return None
            if self.cap == "temperature" and (self.unit or "").endswith("F"):
                return f_to_c(v)
            return v
        if self.kind == "binary":
            on = s == str(c.get("payload_on", "ON")).lower()
            if self.cap == "contact":
                return "open" if on else "closed"
            return on
        if self.kind == "lock":
            if s == str(c.get("state_locked", "LOCKED")).lower():
                return "locked"
            if s == str(c.get("state_unlocked", "UNLOCKED")).lower():
                return "unlocked"
            return s
        if self.kind == "valve":
            if s == str(c.get("state_open", "open")).lower():
                return "open"
            if s == str(c.get("state_closed", "closed")).lower():
                return "closed"
            return s
        if self.kind == "switch":
            return s == str(c.get("state_on", c.get("payload_on", "ON"))).lower()
        if self.kind == "alarm":
            return s
        if self.kind.startswith("light"):
            return self._parse_light(raw, s)
        return raw

    # ---- lights ----
    def _bri_scale(self) -> float:
        return float(self.cfg.get("brightness_scale", 255)) or 255.0

    def _kelvin_mode(self) -> bool:
        return bool(self.cfg.get("color_temp_kelvin"))

    def _parse_light(self, raw: Any, s: str) -> Any:
        c = self.cfg
        if self.kind == "light_json":
            try:
                j = raw if isinstance(raw, dict) else json.loads(raw)
            except (TypeError, ValueError):
                return None
            out: dict[str, Any] = {}
            if "state" in j:
                out["light"] = str(j["state"]).upper() == str(c.get("payload_on", "ON")).upper()
            if j.get("brightness") is not None:
                out["brightness"] = int(round(float(j["brightness"]) / self._bri_scale() * 100))
            if j.get("color_temp") is not None and j.get("color_mode", "color_temp") == "color_temp":
                out["color_temp"] = int(j["color_temp"]) if self._kelvin_mode() else mirek_to_kelvin(j["color_temp"])
            col = j.get("color") or {}
            if col and j.get("color_mode", "xy") != "color_temp":
                if col.get("x") is not None:
                    out["color"] = xy_to_hex(col["x"], col["y"])
                elif col.get("r") is not None:
                    out["color"] = rgb_to_hex(col["r"], col["g"], col["b"])
                elif col.get("h") is not None:
                    out["color"] = hs_to_hex(col["h"], col.get("s", 100))
            return out or None
        try:
            if self.kind == "light_state":
                return {"light": s == str(c.get("state_value_template_on", c.get("payload_on", "ON"))).lower()}
            if self.kind == "light_bri":
                return {"brightness": int(round(float(raw) / self._bri_scale() * 100))}
            if self.kind == "light_ct":
                return {"color_temp": int(float(raw)) if self._kelvin_mode() else mirek_to_kelvin(float(raw))}
            nums = [float(x) for x in str(raw).split(",")]
            if self.kind == "light_rgb":
                return {"color": rgb_to_hex(*nums[:3])}
            if self.kind == "light_xy":
                return {"color": xy_to_hex(nums[0], nums[1])}
            if self.kind == "light_hs":
                return {"color": hs_to_hex(nums[0], nums[1])}
        except (TypeError, ValueError, IndexError):
            return None
        return None

    def command_for(self, capability: str, value: Any) -> tuple[str, str]:
        """(topic, payload) for a command on this entity."""
        c = self.cfg
        if not self.kind.startswith("light"):
            return self.command_topic, self.command_payload(value)
        scale = self._bri_scale()
        modes = set(c.get("supported_color_modes") or [])
        if self.kind == "light_json":
            body: dict[str, Any] = {"state": "ON"}
            if capability == "light":
                body["state"] = "ON" if value else "OFF"
            elif capability == "brightness":
                body["brightness"] = max(1, int(round(float(value) / 100 * scale)))
            elif capability == "color_temp":
                body["color_temp"] = int(value) if self._kelvin_mode() else kelvin_to_mirek(int(value))
            elif capability == "color":
                if "xy" in modes or c.get("xy") or not modes & {"rgb", "rgbw", "rgbww", "hs"}:
                    x, y = hex_to_xy(str(value))
                    body["color"] = {"x": x, "y": y}
                elif modes & {"rgb", "rgbw", "rgbww"} or c.get("rgb"):
                    r, g, b = hex_to_rgb(str(value))
                    body["color"] = {"r": r, "g": g, "b": b}
                else:
                    import colorsys
                    r, g, b = (v / 255 for v in hex_to_rgb(str(value)))
                    h, sat, _ = colorsys.rgb_to_hsv(r, g, b)
                    body["color"] = {"h": round(h * 360, 1), "s": round(sat * 100, 1)}
            return self.command_topic, json.dumps(body)
        # default schema: each capability has its own command topic
        if capability == "light":
            return c.get("command_topic"), str(c.get("payload_on", "ON") if value else c.get("payload_off", "OFF"))
        if capability == "brightness":
            return c.get("brightness_command_topic"), str(max(1, int(round(float(value) / 100 * scale))))
        if capability == "color_temp":
            return c.get("color_temp_command_topic"), str(int(value) if self._kelvin_mode() else kelvin_to_mirek(int(value)))
        if capability == "color":
            if c.get("rgb_command_topic"):
                return c["rgb_command_topic"], ",".join(str(v) for v in hex_to_rgb(str(value)))
            if c.get("xy_command_topic"):
                x, y = hex_to_xy(str(value))
                return c["xy_command_topic"], f"{x},{y}"
            if c.get("hs_command_topic"):
                import colorsys
                r, g, b = (v / 255 for v in hex_to_rgb(str(value)))
                h, sat, _ = colorsys.rgb_to_hsv(r, g, b)
                return c["hs_command_topic"], f"{round(h * 360, 1)},{round(sat * 100, 1)}"
        raise ValueError(f"light has no command for {capability}")

    def command_payload(self, value: Any) -> str:
        c = self.cfg
        if self.kind == "lock":
            return str(c.get("payload_lock", "LOCK") if value == "lock" else c.get("payload_unlock", "UNLOCK"))
        if self.kind == "valve":
            return str(c.get("payload_open", "OPEN") if value == "open" else c.get("payload_close", "CLOSE"))
        if self.kind == "switch":
            return str(c.get("payload_on", "ON") if value else c.get("payload_off", "OFF"))
        if self.kind == "alarm":
            return str(c.get(f"payload_{value}", value.upper()))
        return str(value)


_TPL = re.compile(r"value_json((?:\.\w+|\[['\"]?[^\]'\"]+['\"]?\])+)")


def template_path(tpl: str | None) -> list[str]:
    """Extract a JSON path from common templates like '{{ value_json.temperature }}'."""
    if not tpl:
        return []
    m = _TPL.search(tpl)
    if not m:
        return []
    return [p for p in re.findall(r"\.(\w+)|\[['\"]?([^\]'\"]+)['\"]?\]", m.group(1)) for p in p if p]


def expand(cfg: dict) -> dict:
    out = {ABBREV.get(k, k): (expand(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    base = out.get("~")
    if base:
        for k, v in list(out.items()):
            if k.endswith("_topic") and isinstance(v, str):
                out[k] = v.replace("~", base)
    return out


class MqttAdapter(Adapter):
    source = "mqtt"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.prefix = cfg.get("discovery_prefix", "homeassistant")
        self.discovery = cfg.get("discovery", True)
        self.by_topic: dict[str, list[Entity]] = {}
        self.by_cap: dict[tuple[str, str], Entity] = {}
        self.client: aiomqtt.Client | None = None
        self._pending_subs: set[str] = set()
        self.by_config: dict[str, list[tuple[str, str]]] = {}   # discovery topic -> entities it created
        self._cfg_topic: str | None = None
        for d in cfg.get("devices", []):
            self._add_manual(d)

    # ---------- registration ----------
    @staticmethod
    def valid_topic(t: Any) -> bool:
        return isinstance(t, str) and 0 < len(t) <= 256 and "+" not in t and "#" not in t and "\x00" not in t

    def _register(self, ent: Entity) -> None:
        if ent.state_topic is not None and not self.valid_topic(ent.state_topic):
            self.log.warning("ignoring invalid state_topic %r for %s", ent.state_topic, ent.device_id)
            ent.state_topic = None              # wildcard/empty topics would subscribe to everything
        if ent.command_topic is not None and not self.valid_topic(ent.command_topic):
            ent.command_topic = None
        self.by_cap[(ent.device_id, ent.cap)] = ent
        if self._cfg_topic:
            lst = self.by_config.setdefault(self._cfg_topic, [])
            if (ent.device_id, ent.cap) not in lst:
                lst.append((ent.device_id, ent.cap))
        if ent.state_topic:
            lst = self.by_topic.setdefault(ent.state_topic, [])
            lst[:] = [e for e in lst if not (e.device_id == ent.device_id and e.cap == ent.cap)]
            lst.append(ent)
            self._pending_subs.add(ent.state_topic)

    def _add_manual(self, d: dict) -> None:
        did = self.dev_id(d["id"])
        caps = {}
        for name, c in d.get("capabilities", {}).items():
            kind = c.get("kind") or ("lock" if name == "lock" else "valve" if name == "valve" else
                                     "switch" if name == "switch" else
                                     "binary" if name in BINARY_CLASSES.values() else "sensor")
            path = c["json_key"].split(".") if c.get("json_key") else []
            self._register(Entity(did, name, kind, c.get("state_topic"), c.get("command_topic"),
                                  path, c.get("unit"), c))
            caps[name] = cap(name, writable=bool(c.get("command_topic")) and kind in ("lock", "valve", "switch"))
            if c.get("unit") and name != "temperature":
                caps[name].unit = c["unit"]
        self.hub.upsert_device(Device(id=did, name=d.get("name", d["id"]), source=self.source,
                                      room=d.get("room"), model=d.get("model"), capabilities=caps))

    def _forget_config(self, topic: str) -> None:
        """An empty retained discovery message means "this entity was deleted"."""
        for did, capname in self.by_config.pop(topic, []):
            ent = self.by_cap.pop((did, capname), None)
            if ent and ent.state_topic in self.by_topic:
                lst = [e for e in self.by_topic[ent.state_topic] if e is not ent]
                if lst:
                    self.by_topic[ent.state_topic] = lst
                else:
                    self.by_topic.pop(ent.state_topic, None)
            dev = self.hub.devices.get(did)
            if not dev:
                continue
            dev.capabilities.pop(capname, None)
            if capname == "light":
                for c in ("brightness", "color_temp", "color"):
                    if (did, c) not in self.by_cap:
                        dev.capabilities.pop(c, None)
            if not any(k[0] == did for k in self.by_cap):
                self.hub.remove_device(did)
            else:
                self.hub.upsert_device(dev)

    def handle_discovery(self, topic: str, payload: bytes) -> None:
        if not payload:
            self._forget_config(topic)
            return
        self._cfg_topic = topic
        try:
            self._handle_discovery(topic, payload)
        finally:
            self._cfg_topic = None

    def _handle_discovery(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        component = parts[1] if len(parts) > 2 else ""
        try:
            c = json.loads(payload)
        except ValueError:
            return
        if not isinstance(c, dict):
            return
        c = expand(c)
        dc = c.get("device_class")
        if component == "sensor" and dc in SENSOR_CLASSES:
            kind, name = "sensor", SENSOR_CLASSES[dc]
        elif component == "binary_sensor" and dc in BINARY_CLASSES:
            kind, name = "binary", BINARY_CLASSES[dc]
        elif component in ("lock", "valve", "switch"):
            kind = name = component
        elif component == "alarm_control_panel":
            kind, name = "alarm", "alarm_panel"
        elif component == "light":
            self._discover_light(c, parts)
            return
        else:
            return
        dev = c.get("device") if isinstance(c.get("device"), dict) else {}
        ident = dev.get("identifiers")
        ident = ident[0] if isinstance(ident, list) and ident else ident or c.get("unique_id") or parts[-2]
        did = self.dev_id(str(ident))
        existing = self.hub.devices.get(did)
        capname = name
        if existing and name in existing.capabilities:
            prev = self.by_cap.get((did, name))
            if prev and prev.cfg.get("unique_id") != c.get("unique_id"):
                suffix = re.sub(r"[^A-Za-z0-9_]", "_", str(c.get('object_id') or parts[-2])[-12:])
                capname = f"{name}_{suffix}"
        ent = Entity(did, capname, kind, c.get("state_topic"), c.get("command_topic"),
                     template_path(c.get("value_template")), c.get("unit_of_measurement"), c)
        self._register(ent)
        capobj = cap(name, writable=bool(ent.command_topic) and kind in ("lock", "valve", "switch", "alarm"),
                     label=c.get("name") if capname != name else None)
        if name not in ("temperature",) and c.get("unit_of_measurement"):
            capobj.unit = c["unit_of_measurement"]
        capobj.name = capname
        if existing:
            existing.capabilities.setdefault(capname, capobj)
            self.hub.upsert_device(existing)
        else:
            self.hub.upsert_device(Device(
                id=did, name=dev.get("name") or c.get("name") or str(ident), source=self.source,
                model=" ".join(x for x in (dev.get("manufacturer"), dev.get("model")) if x) or None,
                room=dev.get("suggested_area"), capabilities={capname: capobj}))

    def _discover_light(self, c: dict, parts: list[str]) -> None:
        dev = c.get("device") if isinstance(c.get("device"), dict) else {}
        ident = dev.get("identifiers")
        ident = ident[0] if isinstance(ident, list) and ident else ident or c.get("unique_id") or parts[-2]
        did = self.dev_id(str(ident))
        modes = set(c.get("supported_color_modes") or [])
        json_schema = c.get("schema") == "json"
        caps = {"light": cap("light", writable=bool(c.get("command_topic")))}
        if json_schema:
            ent = Entity(did, "light", "light_json", c.get("state_topic"), c.get("command_topic"), [], None, c)
            self._register(ent)
            has_bri = bool(c.get("brightness")) or bool(modes - {"onoff"})
            has_ct = "color_temp" in modes or bool(c.get("color_temp"))
            has_color = bool(modes & {"xy", "rgb", "rgbw", "rgbww", "hs"}) or any(c.get(k) for k in ("xy", "rgb", "hs"))
            for capname, present in (("brightness", has_bri), ("color_temp", has_ct), ("color", has_color)):
                if present:
                    caps[capname] = cap(capname, writable=bool(c.get("command_topic")))
                    self.by_cap[(did, capname)] = ent
        else:
            self._register(Entity(did, "light", "light_state", c.get("state_topic"), c.get("command_topic"),
                                  template_path(c.get("state_value_template")), None, c))
            for capname, kind, st, ct in (
                    ("brightness", "light_bri", "brightness_state_topic", "brightness_command_topic"),
                    ("color_temp", "light_ct", "color_temp_state_topic", "color_temp_command_topic"),
                    ("color", "light_rgb", "rgb_state_topic", "rgb_command_topic"),
                    ("color", "light_xy", "xy_state_topic", "xy_command_topic"),
                    ("color", "light_hs", "hs_state_topic", "hs_command_topic")):
                if c.get(st) or c.get(ct):
                    if capname in caps and not c.get(st):
                        continue
                    tpl = c.get({"light_bri": "brightness_value_template", "light_ct": "color_temp_value_template",
                                 "light_rgb": "rgb_value_template", "light_xy": "xy_value_template",
                                 "light_hs": "hs_value_template"}[kind])
                    self._register(Entity(did, capname, kind, c.get(st), c.get(ct), template_path(tpl), None, c))
                    caps[capname] = cap(capname, writable=bool(c.get(ct)))
        attrs: dict[str, Any] = {"kind": "light"}
        if "color_temp" in caps:
            if c.get("min_kelvin") or c.get("max_kelvin"):
                attrs.update(ct_min=int(c.get("min_kelvin", 2000)), ct_max=int(c.get("max_kelvin", 6535)))
            else:
                attrs.update(ct_min=mirek_to_kelvin(c.get("max_mireds", 500)), ct_max=mirek_to_kelvin(c.get("min_mireds", 153)))
        existing = self.hub.devices.get(did)
        if existing:
            for k, v in caps.items():
                existing.capabilities.setdefault(k, v)
            self.hub.upsert_device(existing)
        else:
            self.hub.upsert_device(Device(
                id=did, name=dev.get("name") or c.get("name") or str(ident), source=self.source,
                model=" ".join(x for x in (dev.get("manufacturer"), dev.get("model")) if x) or None,
                room=dev.get("suggested_area"), capabilities=caps))
        self.hub.set_attributes(did, attrs, publish=True)

    def handle_message(self, topic: str, payload: bytes) -> None:
        if self.discovery and topic.startswith(self.prefix + "/") and topic.endswith("/config"):
            self.handle_discovery(topic, payload)
            return
        for ent in self.by_topic.get(topic, []):
            v = ent.parse(payload)
            if v is None:
                continue
            if ent.kind.startswith("light"):
                self.hub.set_values(ent.device_id, v)
            else:
                self.hub.set_values(ent.device_id, {ent.cap: v})

    # ---------- connection ----------
    async def start(self):
        self.spawn("mqtt", self._loop)

    async def _loop(self):
        kw = dict(hostname=self.cfg.get("host", "mosquitto"), port=int(self.cfg.get("port", 1883)))
        if self.cfg.get("username"):
            kw.update(username=self.cfg["username"], password=self.cfg.get("password"))
        async with aiomqtt.Client(**kw) as client:
            self.client = client
            self.status = "ok"
            if self.discovery:
                await client.subscribe(f"{self.prefix}/#")
                # Same "birth" message Home Assistant sends: Zigbee2MQTT, ESPHome, Tasmota etc.
                # re-publish their discovery configs, so devices appear even after a broker restart.
                await client.publish(f"{self.prefix}/status", "online")
            self._pending_subs |= set(self.by_topic)
            subber = asyncio.create_task(self._sub_pump(client))
            try:
                async for msg in client.messages:
                    try:
                        self.handle_message(str(msg.topic), msg.payload or b"")
                    except Exception as e:  # noqa: BLE001 - one bad message must not drop the connection
                        self.log.warning("ignored bad MQTT message on %s: %s", msg.topic, e)
            finally:
                subber.cancel()
                self.client = None

    async def _sub_pump(self, client):
        while True:
            while self._pending_subs:
                t = self._pending_subs.pop()
                try:
                    await client.subscribe(t)
                except Exception as e:  # noqa: BLE001 - one rejected topic must not stop the others
                    self.log.warning("could not subscribe to %r: %s", t, e)
            await asyncio.sleep(0.5)

    async def command(self, device: Device, capability: str, value: Any):
        ent = self.by_cap.get((device.id, capability))
        if not ent:
            raise ValueError("no command_topic configured")
        topic, payload = ent.command_for(capability, value)
        if not topic:
            raise ValueError("no command_topic configured")
        if not self.client:
            raise RuntimeError("MQTT not connected")
        await self.client.publish(topic, payload)
