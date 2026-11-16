"""YoLink cloud API v2 (UAC credentials) + YoLink MQTT push.

Get credentials in the YoLink app: Settings > Account > Advanced Settings > User Access Credentials.
Docs: http://doc.yosmart.com
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiomqtt
import httpx

from ..models import Device, cap
from .base import Adapter

API = "https://api.yosmart.com"
MQTT_HOST, MQTT_PORT = "api.yosmart.com", 8003

# device type -> capabilities it exposes
TYPE_CAPS: dict[str, list[str]] = {
    "THSensor": ["temperature", "humidity", "battery"],
    "DoorSensor": ["contact", "battery"],
    "LeakSensor": ["leak", "battery"],
    "MotionSensor": ["motion", "battery"],
    "VibrationSensor": ["vibration", "battery"],
    "COSmokeSensor": ["smoke", "co", "battery"],
    "SoilThcSensor": ["moisture", "temperature", "battery"],
    "Lock": ["lock", "battery"],
    "LockV2": ["lock", "contact", "battery"],
    "Manipulator": ["valve", "battery"],          # YoLink valve controller
    "WaterMeterController": ["valve", "battery"],
    "Outlet": ["switch"],
    "Switch": ["switch"],
}


def normalize(dtype: str, s: dict) -> dict[str, Any]:
    """Turn a YoLink state dict (from getState or an MQTT report) into capability values."""
    out: dict[str, Any] = {}
    if "temperature" in s:
        out["temperature"] = s["temperature"]
    if "humidity" in s:
        out["humidity"] = s["humidity"]
    if "moisture" in s:
        out["moisture"] = s["moisture"]
    if isinstance(s.get("battery"), (int, float)):
        out["battery"] = min(100, int(s["battery"]) * 25)   # YoLink reports 0-4

    st = s.get("state")
    if isinstance(st, dict):          # LockV2 style {"lock": "locked", "door": "closed"}
        if "lock" in st:
            out["lock"] = st["lock"]
        if "door" in st:
            out["contact"] = st["door"]
        return out

    if dtype == "DoorSensor" and st:
        out["contact"] = st
    elif dtype == "LeakSensor" and st:
        out["leak"] = st in ("alert", "full")
    elif dtype in ("MotionSensor", "VibrationSensor") and st:
        out["motion" if dtype == "MotionSensor" else "vibration"] = st == "alert"
    elif dtype == "COSmokeSensor":
        a = s.get("state") or {}
        if isinstance(a, dict):
            out["smoke"], out["co"] = bool(a.get("smokeAlarm")), bool(a.get("gasAlarm"))
    elif dtype == "Lock" and st:
        out["lock"] = st
    elif dtype == "Manipulator" and st:
        out["valve"] = st
    elif dtype == "WaterMeterController":
        v = s.get("valve") or st
        if v:
            out["valve"] = v
    elif dtype in ("Outlet", "Switch") and st:
        out["switch"] = st == "open"
    return out


def command_params(dtype: str, capability: str, value: Any) -> dict:
    if capability == "lock":
        return {"state": {"lock": value}} if dtype == "LockV2" else {"state": value}
    if capability == "valve":
        return {"valve": value} if dtype == "WaterMeterController" else {"state": value}
    if capability == "switch":
        return {"state": "open" if value else "close"}
    raise ValueError(capability)


class YoLinkAdapter(Adapter):
    source = "yolink"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.uaid = cfg["uaid"]
        self.secret = cfg["secret_key"]
        self.poll_seconds = int(cfg.get("poll_seconds", 900))
        self.token: str | None = None
        self.token_exp = 0.0
        self.home_id: str | None = None
        self.raw: dict[str, dict] = {}     # deviceId -> yolink device record
        self.http = httpx.AsyncClient(timeout=20)

    async def _get_token(self) -> str:
        if self.token and time.time() < self.token_exp - 300:
            return self.token
        r = await self.http.post(f"{API}/open/yolink/token", data={
            "grant_type": "client_credentials", "client_id": self.uaid, "client_secret": self.secret})
        r.raise_for_status()
        j = r.json()
        self.token, self.token_exp = j["access_token"], time.time() + int(j.get("expires_in", 7200))
        return self.token

    async def _call(self, method: str, **body) -> dict:
        token = await self._get_token()
        r = await self.http.post(f"{API}/open/yolink/v2/api",
                                 headers={"Authorization": f"Bearer {token}"},
                                 json={"method": method, "time": int(time.time() * 1000), **body})
        r.raise_for_status()
        j = r.json()
        if j.get("code") != "000000":
            raise RuntimeError(f"{method}: {j.get('desc')} ({j.get('code')})")
        return j.get("data") or {}

    async def stop(self):
        await super().stop()
        await self.http.aclose()

    async def start(self):
        self.spawn("poll", self._poll_loop)
        self.spawn("mqtt", self._mqtt_loop)

    async def _discover(self):
        self.home_id = (await self._call("Home.getGeneralInfo"))["id"]
        for d in (await self._call("Home.getDeviceList"))["devices"]:
            caps = TYPE_CAPS.get(d["type"])
            if not caps:
                continue
            self.raw[d["deviceId"]] = d
            self.hub.upsert_device(Device(
                id=self.dev_id(d["deviceId"]), name=d["name"], source=self.source,
                model=d.get("modelName") or d["type"],
                capabilities={c: cap(c) for c in caps}))

    async def _poll_loop(self):
        await self._discover()
        self.status = "ok"
        while True:
            for did, d in list(self.raw.items()):
                try:
                    data = await self._call(f"{d['type']}.getState",
                                            targetDevice=did, token=d["token"])
                    self.hub.set_values(self.dev_id(did), normalize(d["type"], data.get("state") or {}),
                                        online=data.get("online", True))
                except Exception as e:  # noqa: BLE001
                    self.log.debug("state %s: %s", d["name"], e)
                await asyncio.sleep(1)      # stay well under YoLink rate limits
            await asyncio.sleep(self.poll_seconds)

    async def _mqtt_loop(self):
        while not self.home_id:
            await asyncio.sleep(2)
        token = await self._get_token()
        topic = f"yl-home/{self.home_id}/+/report"
        # Reconnect shortly before the 2-hour token expires
        try:
            async with asyncio.timeout(max(60, self.token_exp - time.time() - 300)):
                async with aiomqtt.Client(MQTT_HOST, MQTT_PORT, username=token, password="") as client:
                    await client.subscribe(topic)
                    self.log.info("subscribed to %s", topic)
                    async for msg in client.messages:
                        self._on_report(json.loads(msg.payload))
        except TimeoutError:
            self.token = None   # force refresh, loop restarts via spawn()

    def _on_report(self, m: dict):
        did = m.get("deviceId")
        d = self.raw.get(did)
        if d:
            self.hub.set_values(self.dev_id(did), normalize(d["type"], m.get("data") or {}))

    async def command(self, device: Device, capability: str, value: Any):
        did = device.id.split(":", 1)[1]
        d = self.raw[did]
        data = await self._call(f"{d['type']}.setState", targetDevice=did, token=d["token"],
                                params=command_params(d["type"], capability, value))
        vals = normalize(d["type"], data if "state" in data else {"state": data})
        self.hub.set_values(device.id, vals)
