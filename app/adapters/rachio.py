"""Rachio irrigation controllers (public REST API, API key).

Get the key: app.rach.io → Account Settings → GET API KEY.
API: https://api.rach.io/1/public — Rachio allows about 1,700 calls a day, so we poll no faster
than every 5 minutes and refresh once right after a command.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..models import Device, cap
from .base import Adapter

API = "https://api.rach.io/1/public"
MIN_POLL = 300
ZONE_ACTIONS = ["run_5m", "run_10m", "run_15m", "stop"]


def zone_minutes(action: str) -> int | None:
    """'run_10m' -> 10; anything else -> None."""
    if action.startswith("run_") and action.endswith("m"):
        try:
            return int(action[4:-1])
        except ValueError:
            return None
    return None


def controller_values(dev: dict, running: dict | None, now: float | None = None) -> dict[str, Any]:
    """State of a controller from /person/{id} device JSON + /device/{id}/current_schedule."""
    now = now or time.time()
    online = str(dev.get("status", "")).upper() == "ONLINE"
    standby = dev.get("on") is False
    delay_until = (dev.get("rainDelayExpirationDate") or 0) / 1000
    delay_h = max(0, round((delay_until - now) / 3600)) if delay_until > now else 0
    zone_names = {z.get("id"): z.get("name") for z in dev.get("zones", [])}
    if not online:
        text = "Offline"
    elif running and running.get("zoneId"):
        text = f"Watering {zone_names.get(running['zoneId'], 'a zone')}"
    elif standby:
        text = "Standby"
    elif delay_h:
        text = f"Rain delay · {delay_h} h left"
    else:
        text = "Ready"
    return {"status_text": text, "standby": standby, "rain_delay": delay_h}


class RachioAdapter(Adapter):
    source = "rachio"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.api_key = cfg.get("api_key") or ""
        self.poll_seconds = max(MIN_POLL, int(cfg.get("poll_seconds") or MIN_POLL))
        self.http = httpx.AsyncClient(base_url=API, timeout=20,
                                      headers={"Authorization": f"Bearer {self.api_key}"})
        self.person_id: str | None = None
        self.zone_parent: dict[str, str] = {}     # zone id -> controller id
        self._refresh = asyncio.Event()

    async def start(self):
        if not self.api_key:
            self.status = "needs API key (Settings > Rachio)"
            return
        self.spawn("poll", self._poll_loop)

    async def stop(self):
        await super().stop()
        await self.http.aclose()

    async def _get(self, path: str) -> Any:
        r = await self.http.get(path)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _put(self, path: str, body: dict) -> None:
        r = await self.http.put(path, json=body)
        r.raise_for_status()

    async def refresh(self) -> None:
        if not self.person_id:
            self.person_id = (await self._get("/person/info"))["id"]
        person = await self._get(f"/person/{self.person_id}")
        for dev in person.get("devices", []):
            did = dev["id"]
            running = {}
            try:
                running = await self._get(f"/device/{did}/current_schedule")
            except httpx.HTTPError:
                pass
            cid = self.dev_id(did)
            if cid not in self.hub.devices:
                self.hub.upsert_device(Device(
                    id=cid, name=dev.get("name") or "Rachio", source=self.source, model=dev.get("model"),
                    room="Outdoor", capabilities={
                        "status_text": cap("status_text", label="Status"),
                        "standby": cap("standby", writable=True, label="Standby"),
                        "rain_delay": cap("rain_delay", writable=True, unit="h", min=0, max=72, step=12,
                                          label="Rain delay"),
                    }))
            self.hub.set_attributes(cid, {"kind": "irrigation_controller", "rachio_id": did,
                                          "serial": dev.get("serialNumber")})
            self.hub.set_values(cid, controller_values(dev, running),
                                online=str(dev.get("status", "")).upper() == "ONLINE")
            for z in dev.get("zones", []):
                if not z.get("enabled", True):
                    continue
                zid = self.dev_id(z["id"])
                self.zone_parent[z["id"]] = did
                if zid not in self.hub.devices:
                    self.hub.upsert_device(Device(
                        id=zid, name=z.get("name") or f"Zone {z.get('zoneNumber')}", source=self.source,
                        model=f"Zone {z.get('zoneNumber', '')}".strip(), room="Outdoor", capabilities={
                            "watering": cap("watering", label="Watering"),
                            "action": cap("action", options=ZONE_ACTIONS),
                        }))
                last = (z.get("lastWateredDate") or 0) / 1000 or None
                self.hub.set_attributes(zid, {"kind": "irrigation_zone", "rachio_id": z["id"],
                                              "zone_number": z.get("zoneNumber"), "last_watered": last,
                                              "controller": cid})
                self.hub.set_values(zid, {"watering": running.get("zoneId") == z["id"]})
        self.status = "ok"

    async def _poll_loop(self):
        while True:
            await self.refresh()
            self._refresh.clear()
            try:
                await asyncio.wait_for(self._refresh.wait(), self.poll_seconds)
                await asyncio.sleep(3)      # let Rachio apply the command before reading back
            except asyncio.TimeoutError:
                pass

    async def command(self, device: Device, capability: str, value: Any):
        rid = device.attributes.get("rachio_id")
        if capability == "standby":
            on = bool(value)
            await self._put("/device/off" if on else "/device/on", {"id": rid})
            self.hub.set_values(device.id, {"standby": on})
        elif capability == "rain_delay":
            hours = max(0, min(72, int(float(value))))
            # Rachio takes the delay in seconds; 0 cancels an active delay.
            await self._put("/device/rain_delay", {"id": rid, "duration": hours * 3600})
            self.hub.set_values(device.id, {"rain_delay": hours})
        elif capability == "action":
            if value == "stop":
                await self._put("/device/stop_water", {"id": self.zone_parent.get(rid, rid)})
                self.hub.set_values(device.id, {"watering": False})
            else:
                minutes = zone_minutes(str(value))
                if not minutes:
                    raise ValueError(f"unknown action {value}")
                await self._put("/zone/start", {"id": rid, "duration": minutes * 60})
                self.hub.set_values(device.id, {"watering": True})
        else:
            raise ValueError(f"{capability} can't be controlled")
        self._refresh.set()


ADAPTER = RachioAdapter
SETTINGS = {
    "id": "rachio", "label": "Rachio", "icon": "sprinkler",
    "help": "Sign in at app.rach.io → Account Settings (top-right menu) → GET API KEY, and paste the key here. "
            "Each zone becomes its own card with Run and Stop buttons; the controller card has Standby and Rain delay.",
    "fields": [
        {"key": "api_key", "label": "API key", "type": "password"},
        {"key": "poll_seconds", "label": "Refresh every (seconds, 300 minimum)", "type": "number", "default": 300},
    ],
}
