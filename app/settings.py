"""Account settings edited from the Settings page.

Saved to /data/settings.json (readable only by the container user) and layered over
config.yaml: a value saved here wins over the same value in config.yaml. Passwords and keys
are never sent back to the browser; the UI only learns whether one is set.
"""
from __future__ import annotations

import copy
import math
import os
import json
from typing import Any

from .config import DATA_DIR, load_config, write_private

SETTINGS_FILE = DATA_DIR / "settings.json"
MASK = "********"

# Field types: text | password | number | bool | select | list (comma-separated) | servers
SCHEMA: list[dict] = [
    {"id": "yolink", "label": "YoLink", "icon": "sensors",
     "help": "YoLink app → Settings → Account → Advanced Settings → User Access Credentials → +. "
             "Copy the UAID and Secret Key.",
     "fields": [
         {"key": "uaid", "label": "UAID", "type": "text"},
         {"key": "secret_key", "label": "Secret Key", "type": "password"},
         {"key": "poll_seconds", "label": "Full refresh (seconds)", "type": "number", "default": 900},
     ]},
    {"id": "aqara", "label": "Aqara", "icon": "home_iot_device",
     "help": "developer.aqara.com → Console → Project Management → create a project, then copy App ID, "
             "App Key and Key ID. Save, then sign in below with your Aqara Home account.",
     "login": "aqara",
     "fields": [
         {"key": "region", "label": "Region", "type": "select", "default": "usa",
          "options": ["usa", "eu", "cn", "kr", "ru", "sg"]},
         {"key": "app_id", "label": "App ID", "type": "text"},
         {"key": "app_key", "label": "App Key", "type": "password"},
         {"key": "key_id", "label": "Key ID", "type": "text"},
         {"key": "poll_seconds", "label": "Poll every (seconds)", "type": "number", "default": 60},
     ]},
    {"id": "ecowitt", "label": "Ecowitt", "icon": "partly_cloudy_day",
     "help": "No account needed. In the Ecowitt / WS View Plus app → Customized: Protocol Ecowitt, "
             "Server = this server's IP, Port = the port you open Haven on, Path /data/report, Interval 60.",
     "push_info": True,
     "fields": [
         {"key": "passkeys", "label": "Only accept these gateway PASSKEYs (optional)", "type": "list"},
     ]},
    {"id": "xsense", "label": "X-Sense", "icon": "detector_smoke",
     "help": "Use a second X-Sense account that your devices are shared to. X-Sense allows one login "
             "per account, so your main account would log your phone out.",
     "fields": [
         {"key": "username", "label": "Email", "type": "text"},
         {"key": "password", "label": "Password", "type": "password"},
         {"key": "poll_seconds", "label": "Poll every (seconds)", "type": "number", "default": 120},
     ]},
    {"id": "ring", "label": "Ring", "icon": "videocam",
     "help": "Doorbells, cameras, floodlights and chimes. Turn on, save, then sign in below with your "
             "Ring account (you'll get a verification code). For Ring Alarm sensors, run ring-mqtt "
             "and enable MQTT.",
     "login": "ring",
     "fields": [
         {"key": "poll_seconds", "label": "Refresh battery and settings every (seconds)", "type": "number", "default": 120},
     ]},
    {"id": "hue", "label": "Philips Hue", "icon": "lightbulb",
     "help": "Lights, rooms and zones from your Hue Bridge, controlled locally. Choose Pair bridge: Haven "
             "looks for the bridge on your network (or type its IP), then asks you to press the round "
             "link button on top of the bridge.",
     "login": "hue",
     "fields": [
         {"key": "host", "label": "Bridge IP address", "type": "text"},
     ]},
    {"id": "nut", "label": "UPS (NUT)", "icon": "battery_charging_full",
     "help": "UniFi UPS: UniFi Network → the UPS → Settings → enable NUT Server (set a static IP; "
             "login credentials optional). Add each UPS's IP below. The built-in NUT server lets your "
             "NAS and servers shut down safely using Haven as their UPS server.",
     "fields": [
         {"key": "servers", "label": "UPS / NUT servers", "type": "servers"},
         {"key": "poll_seconds", "label": "Poll every (seconds)", "type": "number", "default": 5},
         {"key": "server.enabled", "label": "Share UPS status as a NUT server (port 3493)", "type": "bool", "default": True},
         {"key": "server.user", "label": "NUT server username", "type": "text", "default": "monuser",
          "help": "Synology expects monuser / secret and UPS name \"ups\"."},
         {"key": "server.password", "label": "NUT server password", "type": "password", "default": "secret"},
         {"key": "server.primary_alias", "label": "Also publish the first UPS as", "type": "text", "default": "ups"},
         {"key": "server.allow", "label": "Allowed client networks (optional, e.g. 192.168.1.0/24)", "type": "list",
         "help": "Empty = private networks only (10.x, 172.16–31.x, 192.168.x, loopback, IPv6 ULA/link-local)."},
        {"key": "server.allow_fsd", "label": "Let clients force a shutdown (FSD / primary)", "type": "bool", "default": False,
         "help": "Off: clients can read status and log in as secondaries only. On: a client that logs in with the "
                 "username and password above may shut down every machine on this UPS. Needs a username."},
     ]},
    {"id": "mqtt", "label": "MQTT", "icon": "lan",
     "help": "Zigbee2MQTT, ESPHome, Tasmota, Shelly and ring-mqtt devices appear automatically via "
             "Home Assistant discovery. Custom topics are set in config.yaml.",
     "fields": [
         {"key": "host", "label": "Broker host", "type": "text", "default": "mosquitto"},
         {"key": "port", "label": "Port", "type": "number", "default": 1883},
         {"key": "username", "label": "Username", "type": "text"},
         {"key": "password", "label": "Password", "type": "password"},
         {"key": "discovery", "label": "Auto-discover devices", "type": "bool", "default": True},
         {"key": "discovery_prefix", "label": "Discovery prefix", "type": "text", "default": "homeassistant"},
     ]},
    {"id": "time", "label": "Date & time", "icon": "timer", "default_enabled": True,
     "help": "Haven checks its clock against NTP time servers and uses the corrected time for automations and "
             "schedules. A container can't change the computer's own clock, so if the offset shown here is large, "
             "fix the time on the host (Synology: Control Panel → Regional Options → Time → sync with an NTP server). "
             "The timezone decides when \"at 22:00\" happens; use an IANA name such as America/Phoenix.",
     "fields": [
         {"key": "timezone", "label": "Timezone", "type": "text", "default": os.getenv("TZ") or "UTC",
          "help": "IANA name like America/Phoenix, Europe/London or UTC."},
         {"key": "time_format", "label": "Clock", "type": "select", "default": "12h", "options": ["12h", "24h"]},
         {"key": "ntp_servers", "label": "NTP servers", "type": "list", "default": ["pool.ntp.org", "time.cloudflare.com"],
          "help": "Comma-separated. Your router or NAS works too, e.g. 192.168.1.1."},
         {"key": "sync_minutes", "label": "Check every (minutes)", "type": "number", "default": 60},
     ]},
    {"id": "history", "label": "History & metrics", "icon": "speed", "default_enabled": True,
     "help": "Where readings are kept for charts. SQLite (recommended) saves to the data folder with no "
             "extra setup. Memory keeps only the last 24 hours and forgets on restart. Prometheus reads "
             "history from a Prometheus server that scrapes Haven's /metrics endpoint (start it with the "
             "\"metrics\" profile in docker-compose.yml). /metrics also works with Grafana or any "
             "Prometheus-compatible tool.",
     "fields": [
         {"key": "backend", "label": "Store history in", "type": "select", "default": "sqlite",
          "options": ["sqlite", "memory", "prometheus"]},
         {"key": "sample_seconds", "label": "Save a reading at most every (seconds)", "type": "number", "default": 60,
          "help": "Door, lock, leak and other on/off changes are always saved immediately."},
         {"key": "retention_days", "label": "Keep history for (days, SQLite)", "type": "number", "default": 365},
         {"key": "prometheus_url", "label": "Prometheus URL", "type": "text", "default": "http://prometheus:9090"},
         {"key": "metrics_enabled", "label": "Publish /metrics for Prometheus", "type": "bool", "default": True},
         {"key": "metrics_token", "label": "/metrics bearer token (optional)", "type": "password",
          "help": "If set, Prometheus must send it (authorization: credentials: in prometheus.yml)."},
     ]},
    {"id": "demo", "label": "Demo devices", "icon": "science",
     "help": "Sample devices for trying Haven. Turn off once your own devices show up.", "fields": []},
]
def _add_plugin_sections() -> None:
    """Integrations that register themselves (see app/adapters/__init__.py) bring their own section."""
    from .adapters import PLUGIN_SETTINGS
    pos = next((i for i, s in enumerate(SCHEMA) if s["id"] in ("mqtt", "history", "demo")), len(SCHEMA))
    for sec in sorted(PLUGIN_SETTINGS, key=lambda x: x["label"].lower()):
        if all(s["id"] != sec["id"] for s in SCHEMA):
            SCHEMA.insert(pos, sec)
            pos += 1


_add_plugin_sections()
SCHEMA_BY_ID = {s["id"]: s for s in SCHEMA}

# A kept ("********") secret only stays if the address it was meant for didn't change; otherwise
# a changed host would receive the old password. section -> (address fields, secret fields)
SECRET_BOUND_TO = {
    "mqtt": (["host"], ["password"]),
    "history": (["prometheus_url"], ["metrics_token"]),
    "airquality": (["provider"], ["airnow_api_key"]),
    "aqara": (["region"], ["app_key"]),
    "dyson": (["region"], ["password"]),
}


def _get(d: dict, dotted: str, default=None):
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return default
        d = d[part]
    return d


def _set(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_saved() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def effective_config() -> dict:
    """config.yaml with Settings-page values layered on top, then field defaults for anything
    still missing (so e.g. the NUT server password really is "secret" when the UI says so,
    and a cleared number field falls back to its default instead of breaking the adapter)."""
    cfg = _merge(load_config(), load_saved())
    for s in SCHEMA:
        sec = cfg.get(s["id"])
        if not isinstance(sec, dict):
            continue
        for f in s["fields"]:
            if "default" in f and _get(sec, f["key"]) is None:
                _set(sec, f["key"], copy.deepcopy(f["default"]))
    return cfg


def _del(d: dict, dotted: str) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        d = d.get(part)
        if not isinstance(d, dict):
            return
    d.pop(parts[-1], None)


def public_view(cfg: dict, statuses: dict[str, str]) -> list[dict]:
    out = []
    for s in SCHEMA:
        sec = cfg.get(s["id"]) or {}
        values: dict[str, Any] = {}
        for f in s["fields"]:
            v = _get(sec, f["key"], f.get("default"))
            if f["type"] == "password":
                values[f["key"]] = MASK if v else ""
            elif f["type"] == "servers":
                values[f["key"]] = [{**srv, "password": MASK if srv.get("password") else ""} for srv in (v or [])]
            else:
                values[f["key"]] = v
        out.append({**{k: s[k] for k in s if k != "fields"}, "fields": s["fields"], "values": values,
                    "enabled": (sec.get("enabled", True) if cfg.get(s["id"]) is not None else bool(s.get("default_enabled"))),
                    "status": statuses.get(s["id"])})
    return out


def save_section(section: str, enabled: bool, values: dict[str, Any]) -> dict:
    schema = SCHEMA_BY_ID[section]
    saved = load_saved()
    current = effective_config().get(section) or {}
    sec = saved.setdefault(section, {})
    sec["enabled"] = bool(enabled)
    addr_keys, secret_keys = SECRET_BOUND_TO.get(section, ([], []))
    moved = any(k in values and values[k] not in (None, "") and str(values[k]) != str(_get(current, k) or "")
                for k in addr_keys if _get(current, k) not in (None, ""))
    for f in schema["fields"]:
        if f["key"] not in values:
            continue
        v = values[f["key"]]
        if f["type"] == "password":
            if v in (MASK, None):
                if moved and f["key"] in secret_keys:
                    _set(sec, f["key"], "")   # address changed: don't send the old secret to the new one
                continue                      # unchanged
        elif f["type"] == "number":
            if v in ("", None):
                _del(sec, f["key"])           # fall back to the default
                continue
            v = float(v) if "." in str(v) else int(v)
            if not math.isfinite(v):
                raise ValueError(f"{f['label']}: not a number")
        elif f["type"] == "select":
            if v in ("", None):
                _del(sec, f["key"])
                continue
            if v not in f.get("options", []):
                raise ValueError(f"{f['label']}: choose one of {', '.join(map(str, f.get('options', [])))}")
        elif f["type"] in ("text",):
            v = "" if v is None else str(v)[:500]
        elif f["type"] == "bool":
            v = bool(v)
        elif f["type"] == "list":
            v = [x.strip() for x in (v.split(",") if isinstance(v, str) else v or []) if x.strip()]
        elif f["type"] == "servers":
            old = {(o.get("host"), o.get("ups")): o for o in (current.get("servers") or []) if isinstance(o, dict)}
            cleaned = []
            for srv in (v if isinstance(v, list) else []):
                if not isinstance(srv, dict) or not srv.get("host"):
                    continue
                srv = {k: srv.get(k) for k in ("name", "host", "port", "ups", "username", "password", "room") if srv.get(k) not in (None, "")}
                if srv.get("password") == MASK:
                    prev = old.get((srv.get("host"), srv.get("ups")))
                    if prev and prev.get("password"):
                        srv["password"] = prev["password"]
                    else:
                        srv.pop("password")
                srv["port"] = int(srv.get("port") or 3493)
                cleaned.append(srv)
            v = cleaned
        _set(sec, f["key"], v)
    write_private(SETTINGS_FILE, json.dumps(saved, indent=2))
    return effective_config()
