"""Matter: devices from a Matter controller server (the one Home Assistant uses), over its WebSocket API.

Haven has no Matter stack of its own. It talks to a separate *Matter server* container:

- matterjs-server (``ghcr.io/matter-js/matterjs-server``), the Open Home Foundation's current Matter
  server, or
- python-matter-server (``ghcr.io/home-assistant-libs/python-matter-server``), its predecessor
  (archived June 2026; the WebSocket API is the same).

Both run with host networking and listen on ``ws://<host>:5580/ws``. Protocol (verified against
matterjs-server docs/websockets_api.md, schema 15, and python-matter-server 8.1.2 sources):

- on connect the server sends ``server_info`` (fabric_id, schema_version, sdk_version, ...), no request;
- requests are ``{"message_id": str, "command": str, "args": {...}}``; answers carry the same message_id
  with ``result``, or ``error_code`` + ``details`` (+ ``reason`` for commissioning failures);
- ``start_listening`` returns every node and then streams events ``{"event": name, "data": ...}``:
  ``node_added``/``node_updated`` (node dict), ``node_removed`` (node id),
  ``attribute_updated`` (``[node_id, "endpoint/cluster/attribute", value]``), ``server_shutdown``...;
- a node is ``{"node_id", "available", "is_bridge", "attributes": {"ep/cluster/attr": value}}``, structs
  inside attribute values are keyed by field tag number (``{"0": 22, "1": 3}``);
- ``device_command`` (node_id, endpoint_id, cluster_id, command_name, payload, timed_request_timeout_ms):
  command names in PascalCase like the CHIP SDK ("On", "MoveToLevelWithOnOff"), payload fields in
  camelCase; both servers accept this form;
- ``write_attribute`` (node_id, attribute_path, value), ``commission_with_code`` (code, network_only).

Each Matter node becomes one Haven device. A node with several controllable endpoints of its own (a power
strip, a double relay) becomes one Haven device per endpoint, and every device behind a Matter bridge
becomes its own Haven device.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import socket
import struct
from typing import Any

from ..color import hs_to_hex, kelvin_to_mirek, mirek_to_kelvin, xy_to_hex, hex_to_xy
from ..models import Device, cap
from .base import Adapter

DEFAULT_PORT = 5580
COMMISSION_TIMEOUT = 330          # matter-server searches for up to 3 min, then commissions and interviews
ACTION_WAIT = 25                  # the Settings button waits this long for a result before answering

# ---- cluster ids (Matter Application Cluster Library) ----
DESCRIPTOR, BASIC_INFO, BRIDGED_INFO = 29, 40, 57
POWER_SOURCE, BOOLEAN_STATE, AIR_QUALITY, SMOKE_CO = 47, 69, 91, 92
ONOFF, LEVEL, COLOR = 6, 8, 768
DOOR_LOCK, WINDOW_COVERING, THERMOSTAT, FAN = 257, 258, 513, 514
ILLUMINANCE, TEMPERATURE, PRESSURE, HUMIDITY, OCCUPANCY = 1024, 1026, 1027, 1029, 1030
POWER_MEASUREMENT, ENERGY_MEASUREMENT = 144, 145
FEATURE_MAP = 0xFFFC

ACTUATORS = (ONOFF, DOOR_LOCK, WINDOW_COVERING, FAN, THERMOSTAT)

# ---- device type ids ----
DT_ROOT, DT_AGGREGATOR, DT_BRIDGED_NODE = 0x0016, 0x000E, 0x0013
DT_LIGHTS = {0x0100, 0x0101, 0x010C, 0x010D}            # on/off, dimmable, color temperature, extended color
DT_PLUGS = {0x010A, 0x010B, 0x010F, 0x0110}             # plug-in units, mounted on/off / dimmable load controls
DT_CONTACT, DT_LEAK, DT_RAIN, DT_FREEZE = 0x0015, 0x0043, 0x0044, 0x0041
DT_AIR_PURIFIER = 0x002D

# Concentration measurement clusters -> (capability, label)
CONCENTRATION = {
    1037: ("co2", "CO₂"), 1066: ("pm25", "PM2.5"), 1069: ("pm10", "PM10"), 1068: ("pm1", "PM1"),
    1070: ("voc", "TVOC"), 1036: ("co_ppm", "CO"), 1043: ("no2", "NO₂"), 1045: ("ozone", "Ozone"),
    1067: ("formaldehyde", "Formaldehyde"), 1071: ("radon", "Radon"),
}
CONC_UNITS = {0: "ppm", 1: "ppb", 2: "ppt", 3: "mg/m³", 4: "µg/m³", 5: "ng/m³", 6: "p/m³", 7: "Bq/m³"}
LEVELS = {1: "Low", 2: "Medium", 3: "High", 4: "Critical"}
AIR_QUALITY_TEXT = {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very poor", 6: "Extremely poor"}
SYSTEM_MODES = {0: "Off", 1: "Auto", 3: "Cool", 4: "Heat", 5: "Emergency heat", 6: "Precooling", 7: "Fan only",
                8: "Dry", 9: "Sleep"}
LOCK_STATES = {0: "jammed", 1: "locked", 2: "unlocked", 3: "unlocked"}   # 0 = NotFullyLocked
ERROR_CODES = {1: "commissioning failed", 2: "the device didn't answer the interview", 3: "the device is offline",
               4: "the device can't be reached", 5: "the device is no longer in the Matter server",
               8: "invalid request", 9: "the Matter server doesn't know this command"}
COMMISSION_REASONS = {
    "not-discovered": "no device in pairing mode was found. Open pairing/sharing for it in the app that controls it, "
                      "then press Add device while the code is valid",
    "passcode-mismatch": "the device rejected the code. Check it, or create a new one in the other app",
    "invalid-payload": "that isn't a valid Matter pairing code",
    "attestation-rejected": "the device's Matter certificate wasn't accepted",
    "timeout": "the device was found but stopped answering. Try again, closer to the router or border router",
}


class MatterError(RuntimeError):
    def __init__(self, code: int, details: str | None, reason: str | None = None):
        self.code, self.details, self.reason = code, details or "", reason
        super().__init__(self.friendly())

    def friendly(self) -> str:
        if self.reason in COMMISSION_REASONS:
            return COMMISSION_REASONS[self.reason]
        base = ERROR_CODES.get(self.code, "the Matter server reported an error")
        d = re.sub(r"\s+", " ", self.details).strip()
        return f"{base}: {d[:160]}" if d and self.code not in (3, 5) else base


# ---------------------------------------------------------------- pairing codes
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
      [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
      [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
      [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def _verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def normalize_code(raw: Any) -> str:
    """A Matter setup code as the server wants it: the QR payload "MT:..." (as printed under the QR code or
    copied from an app) or the 11/21-digit manual code without dashes or spaces. ValueError if it isn't one."""
    s = str(raw or "").strip()
    if not s:
        raise ValueError("enter the device's Matter pairing code first, then press Save and Add device")
    if s[:3].upper() == "MT:":
        body = s[3:].strip().upper()
        if not re.fullmatch(r"[0-9A-Z.\-*]{10,}", body):
            raise ValueError("that QR code text doesn't look right: it should be MT: followed by letters and digits")
        return "MT:" + body
    digits = re.sub(r"[\s\-.]", "", s)
    if not digits.isdigit():
        raise ValueError("a Matter pairing code is 11 or 21 digits (dashes are fine) or the QR text starting with MT:")
    if len(digits) not in (11, 21):
        raise ValueError(f"a Matter pairing code has 11 or 21 digits, this one has {len(digits)}")
    if not _verhoeff_ok(digits):
        raise ValueError("that pairing code has a typo (its check digit doesn't match): check every digit")
    return digits


# ---------------------------------------------------------------- addresses
def normalize_url(raw: Any) -> str:
    """'192.168.1.20', '192.168.1.20:5580', 'nas.local', 'ws://host:5580/ws' or 'http://host:5580' -> ws URL."""
    s = str(raw or "").strip()
    if not s:
        return ""
    m = re.fullmatch(r"(?:(wss?|https?)://)?(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.\-]+)(?::(\d{1,5}))?(/[A-Za-z0-9._~/\-]*)?/?", s)
    if not m:
        raise ValueError("the Matter server address should look like ws://192.168.1.20:5580/ws")
    scheme, host, port, path = m.groups()
    scheme = {"https": "wss", "http": "ws", None: "ws"}.get(scheme, scheme)
    port = int(port or DEFAULT_PORT)
    if not 0 < port < 65536:
        raise ValueError("the port must be between 1 and 65535")
    path = (path or "").rstrip("/") or "/ws"
    return f"{scheme}://{host}:{port}{path}"


def default_gateway() -> str | None:
    """The container's default gateway, i.e. the NAS itself when Haven runs on a Docker bridge network."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 3 and parts[1] == "00000000" and int(parts[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    except (OSError, ValueError):
        pass
    return None


def candidate_urls(configured: str) -> list[str]:
    """Where to look for the Matter server: the configured address, or (empty) this machine: 127.0.0.1 when
    Haven uses host networking, the Docker bridge gateway when it doesn't, then host.docker.internal."""
    if configured:
        return [configured]
    out = [f"ws://127.0.0.1:{DEFAULT_PORT}/ws"]
    gw = default_gateway()
    if gw:
        out.append(f"ws://{gw}:{DEFAULT_PORT}/ws")
    out.append(f"ws://host.docker.internal:{DEFAULT_PORT}/ws")
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- data model helpers
def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _field(s: Any, tag: int, *names: str) -> Any:
    """A struct field: servers key structs by tag number ("0"); accept field names too."""
    if not isinstance(s, dict):
        return None
    for k in (str(tag), tag, *names):
        if k in s:
            return s[k]
    return None


def split_attributes(attributes: dict) -> dict[int, dict[int, dict[int, Any]]]:
    """{"1/6/0": True} -> {1: {6: {0: True}}}"""
    eps: dict[int, dict[int, dict[int, Any]]] = {}
    for path, v in (attributes or {}).items():
        try:
            ep, cl, at = (int(x) for x in str(path).split("/"))
        except ValueError:
            continue
        eps.setdefault(ep, {}).setdefault(cl, {})[at] = v
    return eps


def device_types(eps: dict, ep: int) -> set[int]:
    out = set()
    for x in (eps.get(ep, {}).get(DESCRIPTOR, {}).get(0) or []):
        t = _field(x, 0, "deviceType", "device_type")
        if isinstance(t, int):
            out.add(t)
    return out


def parents_of(node: dict, eps: dict) -> dict[int, int]:
    """endpoint -> direct parent endpoint. matterjs-server sends endpoint_parents; otherwise derive it from the
    Descriptor PartsLists (the smallest PartsList that contains an endpoint belongs to its parent)."""
    raw = node.get("endpoint_parents")
    if isinstance(raw, dict) and raw:
        out = {}
        for k, v in raw.items():
            try:
                out[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
        return out
    parts = {ep: set(x for x in (cl.get(DESCRIPTOR, {}).get(3) or []) if isinstance(x, int))
             for ep, cl in eps.items()}
    out = {}
    for ep in eps:
        if ep == 0:
            continue
        owners = [p for p, s in parts.items() if ep in s and p != ep]
        out[ep] = min(owners, key=lambda p: len(parts[p])) if owners else 0
    return out


def build_units(node: dict, eps: dict) -> list[dict]:
    """Split a node into Haven devices: [{"suffix": str|None, "endpoints": [..], "bridged": ep|None}]."""
    nonroot = sorted(e for e in eps if e != 0)
    parents = parents_of(node, eps)

    def descendants(root: int) -> list[int]:
        out = []
        for e in nonroot:
            p, seen = parents.get(e), set()
            while p is not None and p not in seen:
                if p == root:
                    out.append(e)
                    break
                seen.add(p)
                p = parents.get(p)
        return out

    bridged = [e for e in nonroot if BRIDGED_INFO in eps[e] or DT_BRIDGED_NODE in device_types(eps, e)]
    if bridged:
        return [{"suffix": str(b), "endpoints": [b] + [d for d in descendants(b) if d not in bridged], "bridged": b}
                for b in bridged]
    actuators = [e for e in nonroot if any(c in eps[e] for c in ACTUATORS)
                 and not (eps[e].keys() & {ONOFF} and DT_AGGREGATOR in device_types(eps, e))]
    # a sub-endpoint of a controllable endpoint (e.g. a light's own child) isn't a separate device
    actuators = [e for e in actuators if not any(a in actuators for a in _ancestors(e, parents))]
    if len(actuators) <= 1:
        return [{"suffix": None, "endpoints": [0] + nonroot, "bridged": None}]
    units = {a: {"suffix": str(a), "endpoints": [a], "bridged": None} for a in actuators}
    for e in nonroot:
        if e in units:
            continue
        owner = next((a for a in _ancestors(e, parents) if a in units), actuators[0])
        units[owner]["endpoints"].append(e)
    units[actuators[0]]["endpoints"].insert(0, 0)    # node-wide things (battery on endpoint 0) go to the first
    return list(units.values())


def _ancestors(e: int, parents: dict[int, int]) -> list[int]:
    out, p, seen = [], parents.get(e), set()
    while p is not None and p not in seen:
        out.append(p)
        seen.add(p)
        p = parents.get(p)
    return out


def map_endpoint(cl: dict[int, dict[int, Any]], dts: set[int], ep: int) -> list[tuple[str, Any, dict, str | None]]:
    """One endpoint's clusters -> [(capability, value, cap kwargs, control tag)]. Which capabilities exist
    depends only on which attributes exist, so names stay stable while values change."""
    out: list[tuple[str, Any, dict, str | None]] = []
    is_light = bool(dts & DT_LIGHTS) or (COLOR in cl and not dts & DT_PLUGS)
    is_fan = FAN in cl
    # -- on/off, lights --
    if ONOFF in cl and 0 in cl[ONOFF]:
        v = cl[ONOFF][0]
        on = bool(v) if isinstance(v, bool) else None
        if is_light:
            out.append(("light", on, {}, "onoff"))
        else:
            out.append(("switch", on, {"label": "Power"}, "onoff"))
    if LEVEL in cl and 0 in cl[LEVEL] and is_light:
        lv = _num(cl[LEVEL][0])
        out.append(("brightness", max(1, round(lv / 254 * 100)) if lv is not None else None, {"min": 1, "max": 100}, "level"))
    if COLOR in cl and is_light:
        c = cl[COLOR]
        feats = c.get(FEATURE_MAP) if isinstance(c.get(FEATURE_MAP), int) else 0
        has_ct = bool(feats & 0x10) or 7 in c
        has_xy = bool(feats & 0x08) or (3 in c and 4 in c)
        has_hs = bool(feats & 0x01) or (0 in c and 1 in c)
        mode = c.get(8)
        if has_ct:
            m = _num(c.get(7))
            lo, hi = _num(c.get(0x400B)), _num(c.get(0x400C))        # physical min/max mireds
            kw = {}
            if lo and hi:
                kw = {"min": mirek_to_kelvin(hi), "max": mirek_to_kelvin(lo)}
            out.append(("color_temp", mirek_to_kelvin(m) if m and (mode in (2, None) or not (has_xy or has_hs)) else None, kw, "ct"))
        if has_xy or has_hs:
            val = None
            if mode == 1 and _num(c.get(3)) is not None and _num(c.get(4)) is not None:
                val = xy_to_hex(c[3] / 65536, c[4] / 65536)
            elif mode == 0 and _num(c.get(0)) is not None and _num(c.get(1)) is not None:
                val = hs_to_hex(c[0] / 254 * 360, c[1] / 254 * 100)
            out.append(("color", val, {}, "color_xy" if has_xy else "color_hs"))
    # -- fans / air purifiers --
    if is_fan:
        f = cl[FAN]
        if ONOFF not in cl:
            mode = f.get(0)
            out.append(("switch", (mode != 0) if isinstance(mode, int) else None, {"label": "Power"}, "fan_switch"))
        pct = _num(f.get(3)) if f.get(3) is not None else _num(f.get(2))
        out.append(("fan_speed", int(pct) if pct is not None else None,
                    {"writable": True, "min": 0, "max": 100, "step": 1, "unit": "%"}, "fan_speed"))
    # -- locks --
    if DOOR_LOCK in cl:
        d = cl[DOOR_LOCK]
        st = d.get(0)
        out.append(("lock", LOCK_STATES.get(st) if isinstance(st, int) else None, {}, "lock"))
        if 3 in d:                                                 # DoorState (door position sensor)
            ds = d.get(3)
            out.append(("contact", {0: "open", 1: "closed", 3: "open", 5: "open"}.get(ds) if isinstance(ds, int) else None,
                        {"label": "Door"}, None))
    # -- window coverings --
    if WINDOW_COVERING in cl:
        w = cl[WINDOW_COVERING]
        feats = w.get(FEATURE_MAP) if isinstance(w.get(FEATURE_MAP), int) else 0
        p100 = _num(w.get(14))
        pct = p100 / 100 if p100 is not None else _num(w.get(8))
        open_pct = round(100 - pct) if pct is not None else None   # Matter: 0 % = fully open, 100 % = closed
        out.append(("contact", (None if open_pct is None else "closed" if open_pct <= 0 else "open"), {}, None))
        if (feats & 0x05) == 0x05 or 14 in w:                       # lift + position aware
            out.append(("position", open_pct, {"writable": True, "min": 0, "max": 100, "step": 1, "unit": "%",
                                               "label": "Open"}, "position"))
        out.append(("action", None, {"writable": True, "options": ["open", "close", "stop"]}, "cover"))
    # -- thermostats --
    if THERMOSTAT in cl:
        t = cl[THERMOSTAT]
        lt = _num(t.get(0))
        out.append(("temperature", round(lt / 100, 2) if lt is not None else None, {}, None))
        mode = t.get(28)
        text = SYSTEM_MODES.get(mode) if isinstance(mode, int) else None
        running = t.get(41)
        if text and isinstance(running, int) and running:
            act = "heating" if running & 0x09 else "cooling" if running & 0x12 else "fan" if running & 0x64 else None
            text = f"{text} · {act}" if act else text
        out.append(("status_text", text, {"label": "Mode"}, None))
    # -- sensors --
    if TEMPERATURE in cl:
        v = _num(cl[TEMPERATURE].get(0))
        out.append(("temperature", round(v / 100, 2) if v is not None else None, {}, None))
    if HUMIDITY in cl:
        v = _num(cl[HUMIDITY].get(0))
        out.append(("humidity", round(v / 100, 1) if v is not None else None, {}, None))
    if PRESSURE in cl:
        v = _num(cl[PRESSURE].get(0))                               # 0.1 kPa = 1 hPa
        out.append(("pressure", v, {}, None))
    if ILLUMINANCE in cl:
        v = _num(cl[ILLUMINANCE].get(0))
        out.append(("illuminance", (round(10 ** ((v - 1) / 10000), 1) if v and v > 0 else 0) if v is not None else None, {}, None))
    if OCCUPANCY in cl:
        o = cl[OCCUPANCY]
        feats = o.get(FEATURE_MAP) if isinstance(o.get(FEATURE_MAP), int) else 0
        name = "presence" if feats & 0xE0 else "motion"            # radar / RF sensing / vision = presence
        v = o.get(0)
        out.append((name, bool(v & 1) if isinstance(v, int) else None, {}, None))
    if BOOLEAN_STATE in cl:
        v = cl[BOOLEAN_STATE].get(0)
        b = v if isinstance(v, bool) else None
        if DT_CONTACT in dts:      # spec: StateValue TRUE = contact (closed), FALSE = no contact (open)
            out.append(("contact", None if b is None else ("closed" if b else "open"), {}, None))
        elif DT_LEAK in dts:
            out.append(("leak", b, {}, None))
        elif DT_RAIN in dts:
            out.append(("rain", b, {"label": "Rain"}, None))
        elif DT_FREEZE in dts:
            out.append(("freeze", b, {"label": "Freeze"}, None))
        else:
            out.append(("state", b, {"label": "State"}, None))
    if SMOKE_CO in cl:
        s = cl[SMOKE_CO]
        feats = s.get(FEATURE_MAP) if isinstance(s.get(FEATURE_MAP), int) else 0x03
        if feats & 0x01 or 1 in s:
            out.append(("smoke", (s.get(1) > 0) if isinstance(s.get(1), int) else None, {}, None))
        if feats & 0x02 or 2 in s:
            out.append(("co", (s.get(2) > 0) if isinstance(s.get(2), int) else None, {}, None))
        if 3 in s:
            out.append(("battery_low", (s.get(3) > 0) if isinstance(s.get(3), int) else None, {}, None))
        out.append(("action", None, {"writable": True, "options": ["test"]}, "smoke_test"))
    for cid, (name, label) in CONCENTRATION.items():
        if cid not in cl:
            continue
        c = cl[cid]
        if 0 in c:
            v = _num(c.get(0))
            unit = CONC_UNITS.get(c.get(8)) if isinstance(c.get(8), int) else None
            out.append((name, round(v, 1) if v is not None else None, {"label": label, **({"unit": unit} if unit else {})}, None))
        elif 10 in c:
            lv = c.get(10)
            out.append((f"{name}_level", LEVELS.get(lv) if isinstance(lv, int) else None, {"label": label}, None))
    if AIR_QUALITY in cl:
        v = cl[AIR_QUALITY].get(0)
        out.append(("air_quality", AIR_QUALITY_TEXT.get(v) if isinstance(v, int) else None, {"label": "Air quality"}, None))
    if POWER_MEASUREMENT in cl:
        p = cl[POWER_MEASUREMENT]
        if 8 in p:
            v = _num(p.get(8))
            out.append(("power", round(v / 1000, 1) if v is not None else None, {"label": "Using", "unit": "W"}, None))
        if 4 in p:
            v = _num(p.get(4))
            out.append(("voltage", round(v / 1000, 1) if v is not None else None, {"label": "Voltage", "unit": "V"}, None))
        if 5 in p:
            v = _num(p.get(5))
            out.append(("current", round(v / 1000, 3) if v is not None else None, {"label": "Current", "unit": "A"}, None))
    if ENERGY_MEASUREMENT in cl and 1 in cl[ENERGY_MEASUREMENT]:
        e = _num(_field(cl[ENERGY_MEASUREMENT].get(1), 0, "energy"))      # mWh
        out.append(("energy_total", round(e / 1_000_000, 3) if e is not None else None,
                    {"label": "Total", "unit": "kWh"}, None))
    if POWER_SOURCE in cl:
        ps = cl[POWER_SOURCE]
        if 12 in ps:                                                # BatPercentRemaining, in half percent
            v = _num(ps.get(12))
            out.append(("battery", round(v / 2) if v is not None else None, {}, None))
        if 14 in ps:                                                # BatChargeLevel: 0 ok, 1 warning, 2 critical
            v = ps.get(14)
            out.append(("battery_low", (v > 0) if isinstance(v, int) else None, {}, None))
    return out


def unit_kind(eps: dict, endpoints: list[int]) -> str | None:
    dts = set().union(*(device_types(eps, e) for e in endpoints)) if endpoints else set()
    if dts & DT_LIGHTS or any(COLOR in eps.get(e, {}) for e in endpoints):
        return "light"
    if DT_AIR_PURIFIER in dts:
        return "air_purifier"
    if dts & DT_PLUGS or (any(ONOFF in eps.get(e, {}) for e in endpoints) and any(POWER_MEASUREMENT in eps.get(e, {}) for e in endpoints)
                          and not any(c in eps.get(e, {}) for e in endpoints for c in (FAN, THERMOSTAT, DOOR_LOCK, WINDOW_COVERING))):
        return "smart_plug"
    return None


def unit_caps(eps: dict, unit: dict) -> dict[str, tuple[Any, dict, tuple[int, str] | None]]:
    """All capabilities of one Haven device: name -> (value, cap kwargs, (endpoint, control) or None)."""
    caps: dict[str, tuple[Any, dict, tuple[int, str] | None]] = {}
    for ep in unit["endpoints"]:
        cl = eps.get(ep) or {}
        if ep == 0:                                                 # root: only the node's power source
            cl = {k: v for k, v in cl.items() if k == POWER_SOURCE}
        for name, value, kw, control in map_endpoint(cl, device_types(eps, ep), ep):
            if name in caps and name in ("battery", "battery_low"):
                continue                                            # one battery reading per device is enough
            final, n = name, 2
            while final in caps:                                    # e.g. two temperature endpoints
                final, n = f"{name}_{n}", n + 1
            caps[final] = (value, kw, (ep, control) if control else None)
    return caps


def _text(v: Any) -> str:
    return str(v).strip() if isinstance(v, str) else ""


# ---------------------------------------------------------------- adapter
class MatterAdapter(Adapter):
    source = "matter"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        try:
            self.url = normalize_url(cfg.get("url"))
            self.url_error = None
        except ValueError as e:
            self.url, self.url_error = "", str(e)
        self.bluetooth = bool(cfg.get("bluetooth", False))
        self.lock_pin = str(cfg.get("lock_pin") or "").strip()
        self.ws = None
        self.connected_url: str | None = None
        self.server_info: dict = {}
        self._mid = 0
        self._pending: dict[str, asyncio.Future] = {}
        self.nodes: dict[int, dict] = {}                    # node id -> node data (without attributes)
        self.eps: dict[int, dict] = {}                      # node id -> {ep: {cluster: {attr: value}}}
        self.units: dict[str, dict] = {}                    # haven id -> unit (node_id, endpoints, bridged, caps)
        self.node_units: dict[int, list[str]] = {}
        self.controls: dict[tuple[str, str], tuple[int, int, str]] = {}   # (haven id, cap) -> (node, ep, control)
        self._commission: asyncio.Task | None = None
        self.last_added: str | None = None

    async def start(self):
        if self.url_error:
            self.status = f"error: {self.url_error}"
            return
        self.spawn("ws", self._run, max_delay=60)

    async def stop(self):
        if self._commission and not self._commission.done():
            self._commission.cancel()
        await super().stop()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------- websocket ----------------
    async def _send(self, command: str, args: dict | None = None, timeout: float = 30) -> Any:
        if self.ws is None or self.ws.closed:
            raise RuntimeError("not connected to the Matter server")
        self._mid += 1
        mid = f"haven-{self._mid}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg: dict[str, Any] = {"message_id": mid, "command": command}
        if args is not None:
            msg["args"] = args
        try:
            await self.ws.send_str(json.dumps(msg))
            res = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"the Matter server didn't answer {command} in time") from None
        finally:
            self._pending.pop(mid, None)
        if "error_code" in res:
            raise MatterError(int(res.get("error_code") or 0), res.get("details"), res.get("reason"))
        return res.get("result")

    async def _connect(self, session):
        import aiohttp
        tried = []
        last: Exception | None = None
        for url in candidate_urls(self.url):
            tried.append(url)
            try:
                ws = await session.ws_connect(url, heartbeat=30, timeout=aiohttp.ClientWSTimeout(ws_close=10),
                                              max_msg_size=256 * 1024 * 1024)
            except (aiohttp.WSServerHandshakeError, aiohttp.ClientResponseError) as e:
                last = RuntimeError(f"{_hostport(url)} answered, but it isn't a Matter server's WebSocket (HTTP {getattr(e, 'status', '?')})")
                continue
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                last = e
                continue
            return url, ws
        if self.url:
            if isinstance(last, RuntimeError):
                raise last
            raise RuntimeError(f"no Matter server at {_hostport(self.url)}: is the matter-server container running?")
        raise RuntimeError("no Matter server found on this NAS: is the matter-server container running? "
                           f"(tried {', '.join(_hostport(u) for u in tried)})")

    async def _run(self):
        import aiohttp
        # no overall limit (the connection stays open); a host that drops packets must not stall the search
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url, ws = await self._connect(session)
            async with ws:
                try:
                    first = await ws.receive_json(timeout=20)
                except (ValueError, TypeError, asyncio.TimeoutError):
                    raise RuntimeError(f"{_hostport(url)} isn't a Matter server (no server_info)") from None
                if not isinstance(first, dict) or "schema_version" not in first:
                    raise RuntimeError(f"{_hostport(url)} isn't a Matter server (no server_info)")
                self.ws, self.connected_url, self.server_info = ws, url, first
                self.log.info("matter: connected to %s (%s, schema %s)", url, first.get("sdk_version"), first.get("schema_version"))
                reader = asyncio.create_task(self._reader(ws))
                try:
                    nodes = await self._send("start_listening", timeout=120)
                    self._load(nodes or [])
                    await reader
                finally:
                    reader.cancel()
                    for f in self._pending.values():
                        if not f.done():
                            f.set_exception(RuntimeError("connection to the Matter server closed"))
                    self._pending.clear()
                    self.ws = None
        for did in list(self.units):
            if did in self.hub.devices:
                self.hub.set_values(did, {}, online=False)
        raise RuntimeError(f"connection to the Matter server at {_hostport(url)} closed")

    async def _reader(self, ws):
        import aiohttp
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                    break
                continue
            try:
                m = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            try:
                if "message_id" in m and ("result" in m or "error_code" in m):
                    f = self._pending.get(str(m["message_id"]))
                    if f is not None and not f.done():
                        f.set_result(m)
                elif "event" in m:
                    if self._event(m["event"], m.get("data")) == "shutdown":
                        break
            except Exception as e:  # noqa: BLE001 - one odd message must not drop the connection
                self.log.warning("matter: could not handle %s: %s", str(m)[:200], e)

    def _event(self, name: str, data: Any) -> str | None:
        if name in ("node_added", "node_updated") and isinstance(data, dict):
            self._node(data)
            self._update_status()
        elif name == "node_removed":
            nid = data.get("node_id") if isinstance(data, dict) else data
            self._drop_node(int(nid))
            self._update_status()
        elif name == "attribute_updated" and isinstance(data, (list, tuple)) and len(data) == 3:
            self._attribute(int(data[0]), str(data[1]), data[2])
        elif name == "server_shutdown":
            return "shutdown"
        return None

    # ---------------- nodes -> devices ----------------
    def _load(self, nodes: list[dict]):
        seen = set()
        for n in nodes:
            if isinstance(n, dict) and "node_id" in n:
                self._node(n)
                seen.add(int(n["node_id"]))
        for nid in [n for n in self.node_units if n not in seen]:
            self._drop_node(nid)
        self._update_status()

    def _update_status(self):
        if self.ws is None:
            return
        n = len(self.units)
        ver = self.server_info.get("sdk_version")
        if n:
            self.status = f"ok · {n} Matter device{'s' if n != 1 else ''}" + (f" · added {self.last_added}" if self.last_added else "")
        else:
            self.status = "connected to the Matter server · no devices yet: add one with a code below"
        if ver and not n:
            self.status += f" ({ver})"

    def _node(self, node: dict):
        nid = int(node["node_id"])
        attrs = node.get("attributes") or {}
        self.nodes[nid] = {k: v for k, v in node.items() if k != "attributes"}
        self.eps[nid] = split_attributes(attrs)
        self._register(nid)

    def _drop_node(self, nid: int):
        for did in self.node_units.pop(nid, []):
            self.units.pop(did, None)
            for key in [k for k in self.controls if k[0] == did]:
                self.controls.pop(key, None)
            self.hub.remove_device(did)
        self.nodes.pop(nid, None)
        self.eps.pop(nid, None)

    def _names(self, nid: int, unit: dict) -> tuple[str, str]:
        eps = self.eps[nid]
        bi = eps.get(0, {}).get(BASIC_INFO, {})
        vendor, product = _text(bi.get(1)), _text(bi.get(3))
        name = _text(bi.get(5)) or product or f"Matter device {nid}"
        if unit["bridged"] is not None:
            bb = eps.get(unit["bridged"], {}).get(BRIDGED_INFO, {})
            vendor = _text(bb.get(1)) or vendor
            product = _text(bb.get(3)) or ""
            name = _text(bb.get(5)) or product or f"{name} {unit['bridged']}"
        model = " ".join(x for x in (vendor, product) if x) or "Matter device"
        return name, model

    def _register(self, nid: int):
        node, eps = self.nodes[nid], self.eps[nid]
        units = build_units(node, eps)
        multi = len(units) > 1
        old = set(self.node_units.get(nid, []))
        new_ids = []
        idx = 0
        for unit in units:
            caps = unit_caps(eps, unit)
            if not caps:
                continue
            idx += 1
            did = self.dev_id(f"{nid}-{unit['suffix']}" if multi or unit["bridged"] is not None else str(nid))
            name, model = self._names(nid, unit)
            if multi and unit["bridged"] is None:
                name = f"{name} {idx}"
            unit = {**unit, "node_id": nid, "caps": list(caps)}
            self.units[did] = unit
            new_ids.append(did)
            for key in [k for k in self.controls if k[0] == did]:
                self.controls.pop(key, None)
            for cname, (_v, _kw, ctl) in caps.items():
                if ctl:
                    self.controls[(did, cname)] = (nid, ctl[0], ctl[1])
            self.hub.upsert_device(Device(id=did, name=name[:80], source=self.source, model=model[:80],
                                          capabilities={k: cap(k, **kw) for k, (_v, kw, _c) in caps.items()}))
            attrs: dict[str, Any] = {"matter_node_id": nid, "endpoints": [e for e in unit["endpoints"] if e != 0]}
            kind = unit_kind(eps, unit["endpoints"])
            if kind:
                attrs["kind"] = kind
            ct = caps.get("color_temp")
            if ct and ct[1].get("min"):
                attrs.update(ct_min=ct[1]["min"], ct_max=ct[1]["max"])
            bi = eps.get(0, {}).get(BASIC_INFO, {})
            if _text(bi.get(10)):
                attrs["firmware"] = _text(bi.get(10))
            self.hub.set_attributes(did, attrs)
            self.hub.set_values(did, {k: v for k, (v, _kw, _c) in caps.items() if v is not None}, online=self._online(nid, unit))
        self.node_units[nid] = new_ids
        for did in old - set(new_ids):
            self.units.pop(did, None)
            self.hub.remove_device(did)

    def _online(self, nid: int, unit: dict) -> bool:
        if not self.nodes.get(nid, {}).get("available", False):
            return False
        if unit.get("bridged") is not None:
            r = self.eps[nid].get(unit["bridged"], {}).get(BRIDGED_INFO, {}).get(17)
            if r is False:
                return False
        return True

    def _attribute(self, nid: int, path: str, value: Any):
        if nid not in self.eps:
            return
        try:
            ep, cl, at = (int(x) for x in path.split("/"))
        except ValueError:
            return
        eps = self.eps[nid]
        eps.setdefault(ep, {}).setdefault(cl, {})[at] = value    # null = null value or a removed attribute
        if cl == DESCRIPTOR or (cl == BASIC_INFO and at in (1, 3, 5)) or (cl == BRIDGED_INFO and at in (1, 3, 5)):
            self._register(nid)                         # structure or names changed
            return
        for did in self.node_units.get(nid, []):
            unit = self.units.get(did)
            if unit is None or (ep not in unit["endpoints"] and not (cl == BRIDGED_INFO and ep == unit.get("bridged"))):
                continue
            caps = unit_caps(eps, unit)
            if list(caps) != unit["caps"]:
                self._register(nid)                     # a new reading appeared
                return
            self.hub.set_values(did, {k: v for k, (v, _kw, _c) in caps.items() if v is not None}, online=self._online(nid, unit))

    # ---------------- settings buttons ----------------
    async def action(self, name: str) -> str:
        if name == "refresh":
            if self.ws is None:
                raise RuntimeError(self._not_connected())
            nodes = await self._send("get_nodes", {}, timeout=60)
            self._load(nodes or [])
            n = len(self.units)
            return f"Read {n} Matter device{'s' if n != 1 else ''} from the Matter server again."
        if name == "add_device":
            return await self._start_commission()
        raise ValueError(name)

    def _not_connected(self) -> str:
        st = self.status[len("error: "):] if self.status.startswith("error: ") else "still connecting"
        return f"Not connected to the Matter server yet ({st})."

    def _saved_code(self) -> str:
        """The pairing code as saved on the Settings page (re-read, in case it was saved after this adapter started)."""
        try:
            from .. import settings as settings_mod
            sec = settings_mod.load_saved().get(self.source) or {}
            if "pairing_code" in sec:
                return str(sec.get("pairing_code") or "")
        except Exception:  # noqa: BLE001
            pass
        return str(self.cfg.get("pairing_code") or "")

    def _clear_code(self) -> None:
        self.cfg["pairing_code"] = ""
        try:
            from .. import settings as settings_mod
            if self.source in settings_mod.SCHEMA_BY_ID:
                settings_mod.save_section(self.source, True, {"pairing_code": ""})
        except Exception as e:  # noqa: BLE001
            self.log.warning("matter: could not clear the saved pairing code: %s", e)

    async def _start_commission(self) -> str:
        try:
            code = normalize_code(self._saved_code())
        except ValueError as e:
            raise RuntimeError(str(e)[:1].upper() + str(e)[1:] + ".") from None
        if self.ws is None:
            raise RuntimeError(self._not_connected())
        if self._commission and not self._commission.done():
            raise RuntimeError("Already adding a device: wait for it to finish (up to a few minutes).")
        self._commission = asyncio.create_task(self._do_commission(code))
        self._commission.add_done_callback(lambda t: t.cancelled() or t.exception())   # result is in the status
        done, _ = await asyncio.wait({self._commission}, timeout=ACTION_WAIT)
        if done:
            return self._commission.result()             # raises the friendly error if it failed
        return ("Adding the device… This can take a few minutes. It appears on the dashboard when it's done; "
                "the status at the top of this page shows the result.")

    async def _do_commission(self, code: str) -> str:
        self.log.info("matter: commissioning a device (network_only=%s)", not self.bluetooth)
        try:
            node = await self._send("commission_with_code", {"code": code, "network_only": not self.bluetooth},
                                    timeout=COMMISSION_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            self.status = f"error: couldn't add the device: {msg}"
            raise RuntimeError(f"Couldn't add the device: {msg}.") from None
        self._clear_code()
        name = None
        if isinstance(node, dict) and "node_id" in node:
            self._node(node)
            ids = self.node_units.get(int(node["node_id"])) or []
            name = self.hub.devices[ids[0]].name if ids and ids[0] in self.hub.devices else None
        self.last_added = name or "a new device"
        self._update_status()
        return f"Added {name or 'the device'}. You'll find it on the dashboard; rename it or give it a room on the Devices page."

    # ---------------- control ----------------
    async def command(self, device: Device, capability: str, value: Any):
        ctl = self.controls.get((device.id, capability))
        if ctl is None:
            raise ValueError(f"{capability} can't be controlled")
        nid, ep, kind = ctl
        if self.ws is None:
            raise RuntimeError("not connected to the Matter server")

        async def invoke(cluster: int, cmd: str, payload: dict | None = None, timed: int | None = None):
            args: dict[str, Any] = {"node_id": nid, "endpoint_id": ep, "cluster_id": cluster,
                                    "command_name": cmd, "payload": payload or {}}
            if timed:
                args["timed_request_timeout_ms"] = timed
            await self._send("device_command", args, timeout=30)

        async def write(cluster: int, attr: int, val: Any):
            res = await self._send("write_attribute", {"node_id": nid, "attribute_path": f"{ep}/{cluster}/{attr}", "value": val}, timeout=30)
            bad = [r for r in (res or []) if isinstance(r, dict) and r.get("Status") not in (0, None)]
            if bad:
                raise RuntimeError(f"the device refused the change (Matter status {bad[0].get('Status')})")

        opts = {"transitionTime": 0, "optionsMask": 0, "optionsOverride": 0}
        if kind == "onoff":
            await invoke(ONOFF, "On" if _truthy(value) else "Off")
            self.hub.set_values(device.id, {capability: _truthy(value)})
        elif kind == "level":
            pct = max(1, min(100, int(float(value))))
            await invoke(LEVEL, "MoveToLevelWithOnOff", {"level": max(1, min(254, round(pct / 100 * 254))), **opts})
        elif kind == "ct":
            await invoke(COLOR, "MoveToColorTemperature", {"colorTemperatureMireds": kelvin_to_mirek(float(value)), **opts})
        elif kind in ("color_xy", "color_hs"):
            h = str(value).strip()
            if not re.fullmatch(r"#?[0-9a-fA-F]{6}", h):
                raise ValueError("color must be #rrggbb")
            h = "#" + h.lstrip("#")
            if kind == "color_xy":
                x, y = hex_to_xy(h)
                await invoke(COLOR, "MoveToColor", {"colorX": min(65279, round(x * 65536)), "colorY": min(65279, round(y * 65536)), **opts})
            else:
                import colorsys
                r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
                hh, ss, _v = colorsys.rgb_to_hsv(r, g, b)
                await invoke(COLOR, "MoveToHueAndSaturation", {"hue": min(254, round(hh * 254)), "saturation": min(254, round(ss * 254)), **opts})
        elif kind == "lock":
            payload = {"PINCode": base64.b64encode(self.lock_pin.encode()).decode()} if self.lock_pin else {}
            await invoke(DOOR_LOCK, "LockDoor" if value == "lock" else "UnlockDoor", payload, timed=1000)
        elif kind == "cover":
            await invoke(WINDOW_COVERING, {"open": "UpOrOpen", "close": "DownOrClose", "stop": "StopMotion"}[value])
        elif kind == "position":
            open_pct = max(0, min(100, int(float(value))))
            await invoke(WINDOW_COVERING, "GoToLiftPercentage", {"liftPercent100thsValue": (100 - open_pct) * 100})
        elif kind == "fan_speed":
            await write(FAN, 2, max(0, min(100, int(float(value)))))
        elif kind == "fan_switch":
            if _truthy(value):
                last = self.eps.get(nid, {}).get(ep, {}).get(FAN, {}).get(2)
                await write(FAN, 2, int(last) if isinstance(last, (int, float)) and last > 0 else 50)
            else:
                await write(FAN, 2, 0)
        elif kind == "smoke_test":
            await invoke(SMOKE_CO, "SelfTestRequest")
        else:
            raise ValueError(f"{capability} can't be controlled")


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return bool(v)


def _hostport(url: str) -> str:
    m = re.match(r"^wss?://([^/]+)", url or "")
    return m.group(1) if m else str(url)


ADAPTER = MatterAdapter
SETTINGS = {
    "id": "matter", "label": "Matter", "icon": "home_iot_device",
    "help": "Adds Matter devices (plugs, lights, sensors, locks, blinds, thermostats…) through a Matter server, the "
            "same open-source controller Home Assistant uses. 1) Start the matter-server container on the NAS with "
            "host networking (deploy/synology/docker-compose.matter.yml, see docs/integrations/matter.md) and "
            "turn on IPv6 on the NAS's network (Control Panel → Network → Network Interface → Edit → IPv6: Auto). "
            "Wi-Fi Matter devices then just work; Thread devices also need a Thread border router on your network "
            "(Apple TV or HomePod, Google Nest Hub, a recent Echo, SmartThings hub, or Home Assistant's OTBR). "
            "2) Leave the server address empty if the matter-server runs on this NAS. "
            "3) To add a device: set it up first in the app that already controls it (Apple Home, Google Home, "
            "SmartThings, Alexa, Home Assistant), then use that app's \"Turn on pairing mode\" / \"Share\" / \"Linked "
            "Matter apps & services\" option to get a pairing code. Paste the code below, press Save, then Add device. "
            "Matter allows several apps to control a device at once. Brand-new devices can be added directly only if "
            "the matter-server has Bluetooth (rare on a NAS). Devices already in Home Assistant can come in through "
            "Settings → Home Assistant instead.",
    "fields": [
        {"key": "pairing_code", "label": "Pairing code for a new device (11 or 21 digits, or MT:…)", "type": "text",
         "help": "Press Save, then Add device. The code is cleared once the device is added."},
        {"key": "url", "label": "Matter server address (empty = this NAS)", "type": "text", "default": "",
         "help": "Only if the matter-server runs somewhere else, e.g. ws://192.168.1.20:5580/ws"},
        {"key": "bluetooth", "label": "Add brand-new devices over Bluetooth (the matter-server needs Bluetooth set up)",
         "type": "bool", "default": False, "advanced": True},
        {"key": "lock_pin", "label": "Door lock PIN (only if your lock wants one to lock/unlock remotely)",
         "type": "password", "advanced": True},
    ],
    "actions": [{"id": "add_device", "label": "Add device", "icon": "add"},
                {"id": "refresh", "label": "Refresh", "icon": "sync"}],
}
