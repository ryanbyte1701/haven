"""Awair air-quality monitors.

Primary path: the Awair LOCAL API (no cloud, instant). Enable it per device in the Awair Home
app → the device → Awair+ → Developer Option → Local API, then list the devices' IP addresses.
Optional: the Awair cloud API with an access token from developer.getawair.com (Hobbyist tier).
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..models import Device, cap
from .base import Adapter

CLOUD = "https://developer-apis.awair.is/v1"


def score_status(score: Any) -> str | None:
    """Awair's own bands: 80+ good, 60-79 fair, below 60 poor."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    return "Good" if s >= 80 else "Fair" if s >= 60 else "Poor"


def _num(v: Any) -> float | None:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def parse_local(d: dict) -> dict[str, Any]:
    """Payload of GET http://<ip>/air-data/latest."""
    out = {
        "air_score": _num(d.get("score")),
        "temperature": _num(d.get("temp", d.get("temperature"))),
        "humidity": _num(d.get("humid", d.get("humidity"))),
        "co2": _num(d.get("co2")),
        "voc": _num(d.get("voc")),
        "pm25": _num(d.get("pm25")),
        "pm10": _num(d.get("pm10_est", d.get("estimated_pm10", d.get("pm10")))),
    }
    out["status_text"] = score_status(out["air_score"])
    return {k: v for k, v in out.items() if v is not None}


CLOUD_COMP = {"temp": "temperature", "humid": "humidity", "co2": "co2", "voc": "voc", "pm25": "pm25", "pm10_est": "pm10"}


def parse_cloud(d: dict) -> dict[str, Any]:
    """Payload of the cloud .../air-data/latest endpoint: {"data": [{"score", "sensors": [{comp, value}]}]}."""
    rows = d.get("data") or []
    if not rows:
        return {}
    row = rows[0]
    out: dict[str, Any] = {"air_score": _num(row.get("score"))}
    for s in row.get("sensors", []):
        k = CLOUD_COMP.get(s.get("comp"))
        if k:
            out[k] = _num(s.get("value"))
    out["status_text"] = score_status(out["air_score"])
    return {k: v for k, v in out.items() if v is not None}


MONITOR_CAPS = {
    "air_score": dict(label="Air score", unit=None),
    "temperature": {}, "humidity": {}, "co2": {},
    "voc": dict(label="VOC", unit="ppb"), "pm25": {}, "pm10": dict(unit="µg/m³", label="PM10"),
    "status_text": dict(label="Air"),
}


def monitor_caps() -> dict:
    return {k: cap(k, **kw) for k, kw in MONITOR_CAPS.items()}


class AwairAdapter(Adapter):
    source = "awair"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.hosts: list[str] = [h.strip() for h in (cfg.get("hosts") or []) if str(h).strip()]
        self.token: str = cfg.get("access_token") or ""
        self.poll_seconds = max(10, int(cfg.get("poll_seconds") or 60))
        self.http = httpx.AsyncClient(timeout=10)

    async def start(self):
        if not self.hosts and not self.token:
            self.status = "add device IPs (Local API) or a cloud token"
            return
        if self.hosts:
            self.spawn("local", self._local_loop, max_delay=60)
        if self.token:
            self.spawn("cloud", self._cloud_loop, max_delay=300)

    async def stop(self):
        await super().stop()
        await self.http.aclose()

    async def _local_loop(self):
        known: set[str] = set()
        while True:
            ok = 0
            for host in self.hosts:
                try:
                    data = (await self.http.get(f"http://{host}/air-data/latest")).raise_for_status().json()
                except Exception as e:  # noqa: BLE001
                    self.log.debug("awair %s: %s", host, e)
                    did = self.dev_id(f"local_{host}")
                    if did in self.hub.devices:
                        self.hub.set_values(did, {}, online=False)
                    continue
                did = self.dev_id(f"local_{host}")
                if did not in known:
                    name, model = f"Awair ({host})", "Awair"
                    try:
                        conf = (await self.http.get(f"http://{host}/settings/config/data")).json()
                        model = (conf.get("device_uuid") or "awair").split("_")[0].replace("awair-", "Awair ").title()
                        name = f"{model} ({host})"
                    except Exception:  # noqa: BLE001
                        pass
                    self.hub.upsert_device(Device(id=did, name=name, source=self.source, model=model, capabilities=monitor_caps()))
                    self.hub.set_attributes(did, {"kind": "air_monitor", "host": host})
                    known.add(did)
                self.hub.set_values(did, parse_local(data), online=True)
                ok += 1
            self.status = "ok" if ok else "no Awair answered (is Local API enabled?)"
            await asyncio.sleep(self.poll_seconds)

    async def _cloud_loop(self):
        h = {"Authorization": f"Bearer {self.token}"}
        devs = (await self.http.get(f"{CLOUD}/users/self/devices", headers=h)).raise_for_status().json().get("devices", [])
        for d in devs:
            did = self.dev_id(f"cloud_{d['deviceType']}_{d['deviceId']}")
            self.hub.upsert_device(Device(id=did, name=d.get("name") or d["deviceType"], source=self.source,
                                          model=d.get("deviceType"), capabilities=monitor_caps()))
            self.hub.set_attributes(did, {"kind": "air_monitor"})
        self.status = "ok"
        # Hobbyist tier allows ~300 calls/day per device for latest data: keep cloud polling slow
        interval = max(self.poll_seconds, 300)
        while True:
            for d in devs:
                did = self.dev_id(f"cloud_{d['deviceType']}_{d['deviceId']}")
                try:
                    r = await self.http.get(f"{CLOUD}/users/self/devices/{d['deviceType']}/{d['deviceId']}/air-data/latest",
                                            headers=h, params={"fahrenheit": "false"})
                    self.hub.set_values(did, parse_cloud(r.raise_for_status().json()), online=True)
                except Exception as e:  # noqa: BLE001
                    self.log.debug("awair cloud %s: %s", did, e)
            await asyncio.sleep(interval)


ADAPTER = AwairAdapter
SETTINGS = {
    "id": "awair", "label": "Awair", "icon": "co2",
    "help": "Air-quality monitors, read locally. In the Awair Home app open each device → Awair+ → Developer "
            "Option → Local API → Enable, then add the device's IP address below (give it a fixed IP in your "
            "router). No local access? Paste a cloud access token from developer.getawair.com instead.",
    "fields": [
        {"key": "hosts", "label": "Device IP addresses (comma-separated)", "type": "list"},
        {"key": "access_token", "label": "Cloud access token (optional)", "type": "password"},
        {"key": "poll_seconds", "label": "Read every (seconds)", "type": "number", "default": 60},
    ],
}
