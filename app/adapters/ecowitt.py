"""Ecowitt local push receiver.

In the WS View Plus / Ecowitt app: Weather Services > Customized
  Protocol: Ecowitt   Server: <docker host IP>   Port: 8080   Path: /data/report   Interval: 60
The gateway POSTs form data; each sensor group becomes its own device.
"""
from __future__ import annotations

import re
from typing import Any

from ..models import Device, cap, f_to_c
from .base import Adapter

INHG_TO_HPA = 33.8639


def parse(form: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Return {group: {"label": str, "values": {cap: value}, "units": {cap: unit}}}."""
    groups: dict[str, dict] = {}

    def put(group, label, name, value, unit=None):
        g = groups.setdefault(group, {"label": label, "values": {}, "units": {}})
        g["values"][name] = value
        if unit:
            g["units"][name] = unit

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for k, v in form.items():
        if k == "tempinf":
            put("indoor", "Indoor (gateway)", "temperature", f_to_c(v))
        elif k == "humidityin":
            put("indoor", "Indoor (gateway)", "humidity", num(v))
        elif k == "baromrelin":
            if num(v) is not None:
                put("indoor", "Indoor (gateway)", "pressure", round(num(v) * INHG_TO_HPA, 1))
        elif k == "tempf":
            put("outdoor", "Outdoor", "temperature", f_to_c(v))
        elif k == "humidity":
            put("outdoor", "Outdoor", "humidity", num(v))
        elif k in ("windspeedmph", "windgustmph"):
            put("outdoor", "Outdoor", "wind_speed" if k == "windspeedmph" else "wind_gust", num(v), "mph")
        elif k == "winddir":
            put("outdoor", "Outdoor", "wind_dir", num(v), "°")
        elif k == "uv":
            put("outdoor", "Outdoor", "uv", num(v))
        elif k == "solarradiation":
            put("outdoor", "Outdoor", "solar", num(v), "W/m²")
        elif k in ("rainratein", "dailyrainin"):
            put("outdoor", "Outdoor", "rain_rate" if k == "rainratein" else "rain_daily", num(v),
                "in/h" if k == "rainratein" else "in")
        elif k in ("co2", "co2in"):
            put("indoor", "Indoor (gateway)", "co2", num(v))
        elif m := re.fullmatch(r"temp(\d)f", k):
            put(f"ch{m[1]}", f"Channel {m[1]}", "temperature", f_to_c(v))
        elif m := re.fullmatch(r"humidity(\d)", k):
            put(f"ch{m[1]}", f"Channel {m[1]}", "humidity", num(v))
        elif m := re.fullmatch(r"batt(\d)", k):
            put(f"ch{m[1]}", f"Channel {m[1]}", "battery_low", v == "1")
        elif m := re.fullmatch(r"soilmoisture(\d+)", k):
            put(f"soil{m[1]}", f"Soil {m[1]}", "moisture", num(v))
        elif m := re.fullmatch(r"soilbatt(\d+)", k):
            put(f"soil{m[1]}", f"Soil {m[1]}", "battery_low", (num(v) or 0) < 1.2)
        elif m := re.fullmatch(r"leak_ch(\d)", k):
            put(f"leak{m[1]}", f"Leak {m[1]}", "leak", v == "1")
        elif m := re.fullmatch(r"leakbatt(\d)", k):
            put(f"leak{m[1]}", f"Leak {m[1]}", "battery", min(100, int(num(v) or 0) * 20))
        elif m := re.fullmatch(r"pm25_ch(\d)", k):
            put(f"pm25_{m[1]}", f"Air quality {m[1]}", "pm25", num(v))
        elif m := re.fullmatch(r"tf_ch(\d)c?", k):
            put(f"probe{m[1]}", f"Probe {m[1]}", "temperature", f_to_c(v) if not k.endswith("c") else num(v))
    return groups


class EcowittAdapter(Adapter):
    source = "ecowitt"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.names: dict[str, str] = cfg.get("names", {})
        self.rooms: dict[str, str] = cfg.get("rooms", {})
        self.passkeys: list[str] = cfg.get("passkeys", [])
        self.known: set[str] = set()
        self.status = "waiting for gateway push"

    def ingest(self, form: dict[str, str]) -> bool:
        passkey = form.get("PASSKEY", "gateway")
        if self.passkeys and passkey not in self.passkeys:
            return False
        gw = passkey[-6:]
        model = form.get("model") or form.get("stationtype")
        for group, g in parse(form).items():
            did = self.dev_id(f"{gw}_{group}")
            if did not in self.known:
                caps = {n: cap(n, unit=g["units"].get(n) or cap(n).unit) for n in g["values"]}
                self.hub.upsert_device(Device(
                    id=did, name=self.names.get(group, g["label"]), source=self.source,
                    model=model, room=self.rooms.get(group), capabilities=caps))
                self.known.add(did)
            self.hub.set_values(did, g["values"])
        self.status = "ok"
        return True
