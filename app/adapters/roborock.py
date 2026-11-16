"""Roborock robot vacuums via python-roborock (the library Home Assistant core uses).

Sign in once from Settings → Roborock: enter the email of your Roborock app account and
Roborock emails you a code. The resulting account token (not a password) is stored in
data/roborock.json (owner-only). After that the library talks to each vacuum locally when it
can and falls back to Roborock's cloud MQTT.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from ..config import DATA_DIR
from ..models import Device, cap
from .base import Adapter

log = logging.getLogger("roborock")

TOKEN_FILE = "roborock.json"
# Clients waiting for the emailed code, keyed by email (same client must finish the login).
_PENDING: dict[str, Any] = {}

# Consumable lifetimes in hours, as used by the Roborock app / Home Assistant.
LIFETIME_H = {"main_brush_life": ("main_brush_work_time", 300), "side_brush_life": ("side_brush_work_time", 200),
              "filter_life": ("filter_work_time", 150), "sensor_life": ("sensor_dirty_time", 30)}

STATE_GROUPS = {
    "cleaning": {"starting", "cleaning", "spot_cleaning", "zoned_cleaning", "segment_cleaning", "remote_control_active",
                 "manual_mode", "going_to_target", "mapping", "patrol", "washing_the_mop", "washing_the_mop_2",
                 "robot_status_mopping", "clean_mop_cleaning", "clean_mop_mopping", "segment_mopping",
                 "segment_clean_mop_cleaning", "segment_clean_mop_mopping", "zoned_mopping", "emptying_the_bin",
                 "attaching_the_mop", "detaching_the_mop"},
    "returning": {"returning_home", "docking", "going_to_wash_the_mop"},
    "charging": {"charging"},
    "docked": {"charging_complete", "air_drying_stopping"},
    "paused": {"paused"},
    "error": {"error", "charging_problem", "charger_disconnected", "locked"},
}
ACTIONS = {"start": "APP_START", "pause": "APP_PAUSE", "dock": "APP_CHARGE", "locate": "FIND_ME"}


def _save(data_dir: Path, data: dict) -> None:
    p = Path(data_dir) / TOKEN_FILE
    from ..config import write_private
    write_private(p, json.dumps(data))


def _load(data_dir: Path) -> dict:
    try:
        return json.loads((Path(data_dir) / TOKEN_FILE).read_text())
    except Exception:  # noqa: BLE001
        return {}


def _enum_name(v: Any) -> str:
    """RoborockStateCode etc. → lowercase name ('charging_complete'); tolerates ints/strings."""
    if v is None:
        return "unknown"
    name = getattr(v, "name", None) or str(v)
    return name.lower()


def vacuum_state(state: Any, battery: int | None = None) -> str:
    name = _enum_name(state)
    for group, names in STATE_GROUPS.items():
        if name in names:
            if group == "charging" and (battery or 0) >= 100:
                return "docked"
            return group
    return "idle"


def status_values(status: Any) -> dict[str, Any]:
    """Map a python-roborock StatusV2/StatusTrait to Haven capabilities."""
    out: dict[str, Any] = {}
    battery = getattr(status, "battery", None)
    if battery is not None:
        out["battery"] = int(battery)
    st = vacuum_state(getattr(status, "state", None), battery)
    out["vacuum_state"] = st
    text = getattr(status, "state_name", None) or _enum_name(getattr(status, "state", None)).replace("_", " ")
    out["status_text"] = str(text).replace("_", " ").capitalize()
    area = getattr(status, "square_meter_clean_area", None)
    if area is None and getattr(status, "clean_area", None) is not None:
        area = round(status.clean_area / 1_000_000, 1)        # raw value is mm²
    if area is not None:
        out["clean_area"] = area
    secs = getattr(status, "clean_time", None)
    if secs is not None:
        out["clean_time"] = round(secs / 60)
    fan = getattr(status, "fan_speed_name", None)
    if fan:
        out["fan_speed"] = str(fan)
    return out


def consumable_values(cons: Any) -> dict[str, Any]:
    out = {}
    for capname, (attr, hours) in LIFETIME_H.items():
        secs = getattr(cons, attr, None)
        if secs is not None:
            out[capname] = max(0, min(100, round(100 - secs / (hours * 3600) * 100)))
    return out


def fan_options(status: Any) -> list[str]:
    try:
        return [m.value if hasattr(m, "value") else str(m) for m in (getattr(status, "fan_speed_options", None) or [])]
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------ device cache
# python-roborock's FileCache pickles by default; a pickle in the data folder would run code on load
# if anything could write that file. Store JSON instead (CacheData round-trips via as_dict/from_dict),
# in a file only the container user can read.
CACHE_FILE = "roborock_cache.json"


def _cache_file(data_dir):
    import os
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / CACHE_FILE
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)     # created 0600; FileCache later rewrites in place
    os.close(fd)
    os.chmod(p, 0o600)
    return p


def _cache_dumps(value) -> bytes:
    import json
    for exclude in ({"home_map_content"}, {"home_map_content", "trait_data"}):
        try:
            d = value.as_dict(exclude=exclude)
            if "trait_data" in exclude:
                for dev in (d.get("deviceInfo") or {}).values():
                    if isinstance(dev, dict):
                        dev.pop("traitData", None)
            return json.dumps(d).encode()
        except (TypeError, ValueError):
            continue
    log.warning("Roborock cache could not be saved as JSON; starting it empty")
    return b"{}"


def _cache_loads(data: bytes):
    import json
    from roborock.devices.cache import CacheData
    if not data.strip():
        return None
    try:
        return CacheData.from_dict(json.loads(data))
    except Exception as e:  # noqa: BLE001 - a bad cache just means fetching from the cloud again
        log.warning("ignoring unreadable Roborock cache: %s", e)
        return None


class RoborockAdapter(Adapter):
    source = "roborock"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.poll_seconds = int(cfg.get("poll_seconds") or 30)
        self.manager = None
        self.devs: dict[str, Any] = {}          # our id -> RoborockDevice
        self._tick = 0

    async def start(self):
        self.spawn("poll", self._main, max_delay=300)

    async def stop(self):
        await super().stop()
        if self.manager:
            try:
                await self.manager.close()
            except Exception:  # noqa: BLE001
                pass
            self.manager = None

    # ---------- sign-in (Settings → Roborock → Sign in) ----------
    @classmethod
    async def signin(cls, values: dict, data_dir) -> dict:
        from roborock.web_api import RoborockApiClient
        email = (values.get("email") or "").strip()
        if not email:
            raise ValueError("Enter the email of your Roborock app account")
        code = (values.get("code") or "").strip()
        if not code:
            client = RoborockApiClient(username=email)
            await client.request_code_v4()
            _PENDING[email] = client
            return {"need_code": True, "message": f"Roborock emailed a code to {email}. Enter it here."}
        client = _PENDING.pop(email, None) or RoborockApiClient(username=email)
        user_data = await client.code_login_v4(code)
        _save(data_dir, {"email": email, "user_data": user_data.as_dict(), "base_url": await client.base_url})
        return {"ok": True}

    # ---------- runtime ----------
    async def _main(self):
        from roborock.data import UserData
        from roborock.devices.device_manager import UserParams, create_device_manager
        from roborock.devices.file_cache import FileCache
        saved = _load(DATA_DIR)
        if not saved.get("user_data"):
            self.status = "needs sign-in (Settings > Roborock)"
            while not _load(DATA_DIR).get("user_data"):
                await asyncio.sleep(15)
            saved = _load(DATA_DIR)
        if self.manager:
            await self.manager.close()
        params = UserParams(username=saved["email"], user_data=UserData.from_dict(saved["user_data"]),
                            base_url=saved.get("base_url"))
        self.manager = await create_device_manager(params, cache=FileCache(
            _cache_file(DATA_DIR), serialize_fn=_cache_dumps, deserialize_fn=_cache_loads))
        for dev in await self.manager.get_devices():
            props = getattr(dev, "v1_properties", None)
            if props is None:
                self.log.info("skipping %s: model not supported yet", dev.name)
                continue
            did = self.dev_id(dev.duid)
            self.devs[did] = dev
            try:
                await props.status.refresh()
            except Exception as e:  # noqa: BLE001
                self.log.debug("initial status %s: %s", dev.name, e)
            opts = fan_options(props.status)
            caps = {"vacuum_state": cap("vacuum_state"), "battery": cap("battery"), "status_text": cap("status_text"),
                    "action": cap("action", options=list(ACTIONS)),
                    "clean_area": cap("clean_area", unit="m²", label="Last clean"),
                    "clean_time": cap("clean_time", unit="min", label="Clean time"),
                    "filter_life": cap("filter_life", unit="%", label="Filter"),
                    "main_brush_life": cap("main_brush_life", unit="%", label="Main brush"),
                    "side_brush_life": cap("side_brush_life", unit="%", label="Side brush")}
            if opts:
                caps["fan_speed"] = cap("fan_speed", writable=True, options=opts, label="Suction")
            product = getattr(dev, "product", None)
            self.hub.upsert_device(Device(id=did, name=dev.name, source=self.source,
                                          model=getattr(product, "name", None) or getattr(product, "model", None),
                                          capabilities=caps, attributes={"kind": "vacuum"}))
        self.status = "ok" if self.devs else "no supported vacuums on this account"
        while True:
            await self._refresh(consumables=self._tick % 20 == 0)
            self._tick += 1
            await asyncio.sleep(self.poll_seconds)

    async def _refresh(self, consumables: bool = False):
        for did, dev in self.devs.items():
            props = dev.v1_properties
            try:
                await props.status.refresh()
                vals = status_values(props.status)
                if vals.get("vacuum_state") == "error":
                    self.hub.set_attributes(did, {"error_text": getattr(props.status, "error_code_name", None)})
                if consumables:
                    await props.consumables.refresh()
                    vals.update(consumable_values(props.consumables))
                self.hub.set_values(did, vals, online=True)
            except Exception as e:  # noqa: BLE001
                self.log.debug("refresh %s: %s", dev.name, e)
                self.hub.set_values(did, {}, online=getattr(dev, "is_connected", False))

    async def command(self, device: Device, capability: str, value: Any):
        from roborock.roborock_typing import RoborockCommand
        dev = self.devs[device.id]
        props = dev.v1_properties
        if capability == "action":
            if value not in ACTIONS:
                raise ValueError(f"unknown action {value}")
            await props.command.send(getattr(RoborockCommand, ACTIONS[value]))
            optimistic = {"start": "cleaning", "pause": "paused", "dock": "returning"}.get(value)
            if optimistic:
                self.hub.set_values(device.id, {"vacuum_state": optimistic})
        elif capability == "fan_speed":
            mode = next((m for m in (props.status.fan_speed_options or []) if getattr(m, "value", m) == value), None)
            if mode is None:
                raise ValueError(f"unsupported suction level {value}")
            await props.command.send(RoborockCommand.SET_CUSTOM_MODE, params=[mode.code])
            self.hub.set_values(device.id, {"fan_speed": value})
        else:
            raise ValueError(f"{capability} isn't controllable")
        await asyncio.sleep(2)
        await self._refresh()


ADAPTER = RoborockAdapter
SETTINGS = {
    "id": "roborock", "label": "Roborock", "icon": "vacuum",
    "help": "Roborock robot vacuums. Turn on, save, then Sign in with the email of your Roborock app account; "
            "Roborock emails you a code. Haven then talks to the vacuum on your network when it can, and through "
            "Roborock's cloud otherwise. You must have accepted the user agreement in the Roborock app.",
    "login": True,
    "login_fields": [{"key": "email", "label": "Roborock account email"}],
    "login_help": "Roborock will email you a verification code.",
    "fields": [
        {"key": "poll_seconds", "label": "Refresh every (seconds)", "type": "number", "default": 30},
    ],
}
