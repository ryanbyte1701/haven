"""Flo by Moen smart water monitor / shutoff + Flo water sensors ("pucks").

Uses aioflo (the library behind Home Assistant's Flo integration). Flo has no token-only sign-in,
so the email and password are kept in Haven's settings (owner-only file, never sent to the browser).
Accounts created in the newer Moen app use Moen's sign-in: tick "Moen account".
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from ..models import Device, cap, f_to_c
from .base import Adapter

MODES = ["home", "away", "sleep"]
SLEEP_MINUTES = 120          # Flo only accepts 120, 1440 or 4320


def is_puck(info: dict) -> bool:
    return "puck" in str(info.get("deviceType", "")).lower()


def monitor_values(info: dict, mode: str | None) -> dict[str, Any]:
    """Shutoff/monitor state from /devices/{id}."""
    cur = (info.get("telemetry") or {}).get("current") or {}
    valve = ((info.get("valve") or {}).get("lastKnown") or "").lower()
    pending = ((info.get("notifications") or {}).get("pending") or {})
    leak = int(pending.get("criticalCount") or 0) > 0
    out: dict[str, Any] = {"leak": leak}
    if valve in ("open", "closed"):
        out["valve"] = valve
    if cur.get("gpm") is not None:
        out["flow_rate"] = round(float(cur["gpm"]), 2)
    if cur.get("psi") is not None:
        out["water_pressure"] = round(float(cur["psi"]), 1)
    if cur.get("tempF") is not None:
        out["temperature"] = f_to_c(cur["tempF"])
    if mode in MODES:
        out["mode"] = mode
    if leak:
        out["status_text"] = "Leak alert"
    elif valve == "closed":
        out["status_text"] = "Water shut off"
    elif valve == "intransition":
        out["status_text"] = "Valve moving…"
    else:
        out["status_text"] = f"Normal · {mode}" if mode else "Normal"
    return out


def puck_values(info: dict) -> dict[str, Any]:
    cur = (info.get("telemetry") or {}).get("current") or {}
    fw = info.get("fwProperties") or {}
    out: dict[str, Any] = {"leak": bool(fw.get("telemetry_water"))}
    if cur.get("tempF") is not None:
        out["temperature"] = f_to_c(cur["tempF"])
    if cur.get("humidity") is not None:
        out["humidity"] = round(float(cur["humidity"]))
    batt = cur.get("batteryPercent", fw.get("telemetry_battery_percent"))
    if batt is not None:
        out["battery"] = int(batt)
    return out


def usage_today(consumption: dict) -> float | None:
    agg = (consumption or {}).get("aggregations") or {}
    v = agg.get("sumTotalGallonsConsumed")
    return round(float(v), 1) if v is not None else None


class FloAdapter(Adapter):
    source = "flo"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.email, self.password = cfg.get("email") or "", cfg.get("password") or ""
        self.use_sso = bool(cfg.get("use_sso"))
        self.poll_seconds = max(30, int(cfg.get("poll_seconds") or 60))
        self.api = None
        self.session = None
        self.location_of: dict[str, str] = {}      # flo device id -> location id
        self._refresh = asyncio.Event()

    async def start(self):
        if not (self.email and self.password):
            self.status = "needs email and password (Settings > Flo)"
            return
        self.spawn("poll", self._poll_loop)

    async def stop(self):
        await super().stop()
        if self.session:
            await self.session.close()
            self.session = None

    async def _connect(self):
        from aiohttp import ClientSession
        from aioflo import async_get_api
        if self.session:
            await self.session.close()
        self.session = ClientSession()
        self.api = await async_get_api(self.email, self.password, session=self.session, use_sso=self.use_sso)

    async def refresh(self):
        if not self.api:
            await self._connect()
        user = await self.api.user.get_info(include_location_info=True)
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        for loc in user.get("locations", []):
            loc_id = loc["id"]
            loc_info = await self.api.location.get_info(loc_id)
            mode = ((loc_info.get("systemMode") or {}).get("target")
                    or (loc_info.get("systemMode") or {}).get("lastKnown"))
            try:
                used = usage_today(await self.api.water.get_consumption_info(loc_id, midnight, datetime.now().astimezone()))
            except Exception:  # noqa: BLE001 - usage is nice-to-have
                used = None
            for d in loc.get("devices", []):
                fid = d["id"]
                info = await self.api.device.get_info(fid)
                self.location_of[fid] = loc_id
                did = self.dev_id(fid)
                name = info.get("nickname") or loc.get("nickname") or ("Flo Water Sensor" if is_puck(info) else "Flo")
                if is_puck(info):
                    if did not in self.hub.devices:
                        self.hub.upsert_device(Device(id=did, name=name, source=self.source,
                                                      model=info.get("deviceModel") or "Flo Water Sensor",
                                                      capabilities={c: cap(c) for c in ("leak", "temperature", "humidity", "battery")}))
                    self.hub.set_attributes(did, {"kind": "leak_sensor", "flo_id": fid})
                    self.hub.set_values(did, puck_values(info), online=bool(info.get("isConnected", True)))
                    continue
                if did not in self.hub.devices:
                    self.hub.upsert_device(Device(id=did, name=name, source=self.source,
                                                  model=info.get("deviceModel") or "Flo by Moen", capabilities={
                        "status_text": cap("status_text", label="Status"),
                        "valve": cap("valve"),
                        "leak": cap("leak"),
                        "flow_rate": cap("flow_rate", unit="gpm", label="Flow"),
                        "water_pressure": cap("water_pressure", unit="psi", label="Pressure"),
                        "temperature": cap("temperature"),
                        "water_usage_today": cap("water_usage_today", unit="gal", label="Used today"),
                        "mode": cap("mode", writable=True, options=MODES, label="Mode"),
                    }))
                vals = monitor_values(info, mode)
                if used is not None:
                    vals["water_usage_today"] = used
                self.hub.set_attributes(did, {"kind": "water_monitor", "flo_id": fid, "location_id": loc_id})
                self.hub.set_values(did, vals, online=bool(info.get("isConnected", True)))
        self.status = "ok"

    async def _poll_loop(self):
        try:
            while True:
                await self.refresh()
                self._refresh.clear()
                try:
                    await asyncio.wait_for(self._refresh.wait(), self.poll_seconds)
                    await asyncio.sleep(3)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            self.api = None          # sign in again on the next attempt
            raise

    async def command(self, device: Device, capability: str, value: Any):
        if not self.api:
            await self._connect()
        fid = device.attributes.get("flo_id")
        if capability == "valve":
            if value == "close":
                await self.api.device.close_valve(fid)
            elif value == "open":
                await self.api.device.open_valve(fid)
            else:
                raise ValueError(value)
            self.hub.set_values(device.id, {"valve": "closed" if value == "close" else "open"})
        elif capability == "mode":
            loc = device.attributes.get("location_id") or self.location_of.get(fid)
            if value == "home":
                await self.api.location.set_mode_home(loc)
            elif value == "away":
                await self.api.location.set_mode_away(loc)
            elif value == "sleep":
                await self.api.location.set_mode_sleep(loc, SLEEP_MINUTES, "home")
            else:
                raise ValueError(value)
            self.hub.set_values(device.id, {"mode": value})
        else:
            raise ValueError(f"{capability} can't be controlled")
        self._refresh.set()


ADAPTER = FloAdapter
SETTINGS = {
    "id": "flo", "label": "Flo by Moen", "icon": "valve",
    "help": "Use the email and password you sign in to the Flo (or Moen Smart Water) app with. If you created your "
            "account in the newer Moen app, also tick Moen account. Haven shows the shutoff valve, flow, pressure, "
            "water temperature and today's usage, plus any Flo water sensors. Sleep mode lasts 2 hours, then returns to Home.",
    "fields": [
        {"key": "email", "label": "Email", "type": "text"},
        {"key": "password", "label": "Password", "type": "password"},
        {"key": "use_sso", "label": "Moen account (signed up in the Moen app)", "type": "bool", "default": False},
        {"key": "poll_seconds", "label": "Refresh every (seconds)", "type": "number", "default": 60},
    ],
}
