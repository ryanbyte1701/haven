"""YoLink cloud API v2 (UAC credentials) + YoLink MQTT push.

Get credentials in the YoLink app: Settings > Account > Advanced Settings > User Access Credentials.
Docs: http://doc.yosmart.com  Reference implementation: the `yolink-api` package (YoSmart) and
Home Assistant's yolink integration.

State shapes (the same record comes from <Type>.getState as `data.state` and from MQTT as `data`):
  Manipulator           {"state": "open"|"closed", "battery": 0-4, ...}
  WaterMeterController  {"state": {"valve": "open"|"close", "meter": n, "waterFlowing": b},
                         "battery": 0-4, "powerSupply": "battery"|"PowerLine", "alarm": {...}}
  WaterMeterMultiController  {"state": {"valves": {"0": "open", "1": "close"}, "meters": {...}}, ...}
  WaterLeakController   {"state": {"valve": "open"|"close", ...}, "battery", "powerSupply", "alarm"}
  Sprinkler             {"state": {"mode": "manual", "watering": {"left": m, "total": m}}}
  SprinklerV2           {"state": {"running": bool}, "battery": 0-4}
Batteries are a level 0-4 (sometimes sent as a string) mapped like Home Assistant: 0, 25, 50, 75, 100 %.
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

# device type -> capabilities it always exposes
TYPE_CAPS: dict[str, list[str]] = {
    "THSensor": ["temperature", "humidity", "battery"],
    "DoorSensor": ["contact", "battery"],
    "LeakSensor": ["leak", "battery"],
    "MultiCapsLeakSensor": ["leak", "battery"],
    "MotionSensor": ["motion", "battery"],
    "VibrationSensor": ["vibration", "battery"],
    "COSmokeSensor": ["smoke", "co", "battery"],
    "SmokeAlarm": ["smoke", "battery"],
    "SoilThcSensor": ["moisture", "temperature", "battery"],
    "Lock": ["lock", "battery"],
    "LockV2": ["lock", "contact", "battery"],
    "Finger": ["battery"],                       # robotic button pusher: Home Assistant only reads its battery
    # valves. Their battery is optional (many run on mains / DC with batteries only as backup), so the
    # battery capability is added when the device reports one and isn't on mains (powerSupply).
    "Manipulator": ["valve"],                    # Valve Controller (YS5001/YS5003/YS4909 …)
    "WaterMeterController": ["valve", "leak"],   # FlowSmart meter + valve (YS5008/YS5018 …); YS5007 = meter only
    "WaterMeterMultiController": ["valve", "leak"],   # two valves: the 2nd shows as its own device
    "WaterLeakController": ["valve", "leak"],
    "Sprinkler": ["valve"],
    "SprinklerV2": ["valve"],
    "Outlet": ["switch"],
    "Switch": ["switch"],
}
VALVE_TYPES = {"Manipulator", "WaterMeterController", "WaterMeterMultiController", "WaterLeakController",
               "Sprinkler", "SprinklerV2"}
METER_ONLY_MODELS = ("YS5007",)        # FlowSmart meter without a valve (Home Assistant skips its valve too)
VALVE2 = "-valve2"                     # id suffix of the 2nd valve of a WaterMeterMultiController
TYPE_LABEL = {"Manipulator": "valve controller", "WaterMeterController": "water meter + valve",
              "WaterMeterMultiController": "water meter + 2 valves", "WaterLeakController": "water leak controller",
              "Sprinkler": "sprinkler", "SprinklerV2": "sprinkler", "Finger": "finger"}
# MQTT events whose data carries device state (same list as yolink-api's mqtt_client)
EVENTS = ("Report", "Alert", "StatusChange", "getState", "setState", "DevEvent", "waterReport", "DataRecord")
AUTH_CODES = {"000103", "010104", "010105", "000106"}   # token invalid / expired: get a new one and retry


def caps_for(dtype: str, model: str | None = "") -> list[str]:
    caps = list(TYPE_CAPS.get(dtype, []))
    if dtype == "WaterMeterController" and (model or "").upper().startswith(METER_ONLY_MODELS):
        caps.remove("valve")
    return caps


def _layers(s: Any) -> list[dict]:
    """The record and its nested "state" dicts, outermost first (getState data, its .state, its .state.state)."""
    out = []
    while isinstance(s, dict) and len(out) < 3:
        out.append(s)
        s = s.get("state")
    return out


def _find(layers: list[dict], key: str) -> Any:
    for d in layers:
        if d.get(key) is not None:
            return d[key]
    return None


def battery_pct(v: Any) -> int | None:
    """YoLink battery level 0-4 (int or string) -> percent like Home Assistant (0, 25, 50, 75, 100)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n or n < 0:
        return None
    if n <= 4:
        return int(round(n)) * 25
    return int(min(n, 100))          # a few devices already report percent


def valve_state(v: Any) -> str | None:
    """"open" / "closed" from YoLink's "open" / "close" / "closed" / bool (running)."""
    if isinstance(v, bool):
        return "open" if v else "closed"
    if not isinstance(v, str):
        return None
    v = v.strip().lower()
    if v in ("open", "opened", "on", "start", "running"):
        return "open"
    if v in ("close", "closed", "off", "stop", "end", "stopped"):
        return "closed"
    return None


def is_mains(s: Any) -> bool:
    """True when the device says it runs on mains/DC power (powerSupply "PowerLine", "DC", "usb", …)."""
    p = _find(_layers(s), "powerSupply")
    return isinstance(p, str) and p.strip().lower() not in ("", "battery", "batteries")


def normalize(dtype: str, s: dict) -> dict[str, Any]:
    """Turn a YoLink state record (getState `data.state`, or MQTT `data`) into capability values.

    Works for both shapes YoLink uses: values next to "state" (state is a string) or inside a nested
    "state" dict (state.valve, state.lock, state.temperature …)."""
    if not isinstance(s, dict):
        return {} if s is None else normalize(dtype, {"state": s})
    L = _layers(s)
    out: dict[str, Any] = {}
    prim = next((d["state"] for d in L if isinstance(d.get("state"), str)), None)   # "open", "alert", …

    recs = s.get("records")                       # THSensor.DataRecord: newest record wins
    if isinstance(recs, list) and recs:
        newest = max((r for r in recs if isinstance(r, dict)), key=lambda r: r.get("time", 0), default={})
        L = [newest, *L]

    for k in ("temperature", "humidity"):
        v = _find(L, k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    if dtype == "SoilThcSensor":                  # soil "humidity" is the soil moisture
        if "humidity" in out:
            out["moisture"] = out.pop("humidity")
    if isinstance(_find(L, "moisture"), (int, float)):
        out["moisture"] = _find(L, "moisture")

    if not is_mains(s):
        b = battery_pct(_find(L, "battery"))
        if b is not None:
            out["battery"] = b

    lock, door = _find(L, "lock"), _find(L, "door")
    if isinstance(lock, str):
        out["lock"] = lock
    if isinstance(door, str):
        out["contact"] = door

    if dtype == "DoorSensor" and prim:
        out["contact"] = prim
    elif dtype in ("LeakSensor", "MultiCapsLeakSensor") and prim:
        out["leak"] = prim in ("alert", "full")
    elif dtype in ("MotionSensor", "VibrationSensor") and prim:
        out["motion" if dtype == "MotionSensor" else "vibration"] = prim == "alert"
    elif dtype in ("COSmokeSensor", "SmokeAlarm"):
        a = next((d for d in L if "smokeAlarm" in d or "gasAlarm" in d), None)
        if a is not None:
            out["smoke"] = bool(a.get("smokeAlarm"))
            if dtype == "COSmokeSensor":
                out["co"] = bool(a.get("gasAlarm"))
    elif dtype == "Lock" and prim:
        out["lock"] = prim
    elif dtype == "Manipulator":
        v = valve_state(_find(L, "valve") or prim)
        if v:
            out["valve"] = v
    elif dtype in ("WaterMeterController", "WaterLeakController"):
        v = valve_state(_find(L, "valve"))
        if v:
            out["valve"] = v
    elif dtype == "WaterMeterMultiController":
        vs = _find(L, "valves")
        if isinstance(vs, dict):
            for ch, key in (("0", "valve"), ("1", "valve_2")):
                v = valve_state(vs.get(ch))
                if v:
                    out[key] = v
    elif dtype == "Sprinkler":
        ev = s.get("event")                       # Sprinkler.waterReport {"event": "start" | "end"}
        w = _find(L, "watering")
        if isinstance(ev, str) and valve_state(ev):
            out["valve"] = valve_state(ev)
        elif isinstance(w, dict) and isinstance(w.get("left"), (int, float)):
            out["valve"] = "open" if w["left"] > 0 else "closed"
        elif any("mode" in d for d in L[1:]):     # a full state without "watering" = not watering
            out["valve"] = "closed"
    elif dtype == "SprinklerV2":
        v = _find(L, "running")
        if isinstance(v, bool):
            out["valve"] = valve_state(v)
    elif dtype in ("Outlet", "Switch") and prim:
        out["switch"] = prim == "open"

    if dtype in ("WaterMeterController", "WaterMeterMultiController", "WaterLeakController"):
        alarm = _find(L, "alarm")
        if isinstance(alarm, dict) and isinstance(alarm.get("leak"), bool):
            out["leak"] = alarm["leak"]

    if dtype in TYPE_CAPS:                        # never invent capabilities a device type doesn't have
        allowed = set(TYPE_CAPS[dtype]) | {"battery", "valve_2"}
        out = {k: v for k, v in out.items() if k in allowed}
    return out


def command_method(dtype: str, capability: str) -> str:
    return "setManualWater" if dtype == "Sprinkler" and capability == "valve" else "setState"


def command_params(dtype: str, capability: str, value: Any, channel: int = 0) -> dict:
    if capability == "lock":
        return {"state": {"lock": value}} if dtype == "LockV2" else {"state": value}
    if capability == "valve":
        if dtype in ("WaterMeterController", "WaterLeakController"):
            return {"valve": value}
        if dtype == "WaterMeterMultiController":
            return {"valves": {str(channel): value}}
        if dtype == "SprinklerV2":
            return {"running": value == "open"}
        if dtype == "Sprinkler":
            return {"state": "start" if value == "open" else "stop"}
        return {"state": value}                  # Manipulator: "open" / "close"
    if capability == "switch":
        return {"state": "open" if value else "close"}
    raise ValueError(capability)


class YoLinkAdapter(Adapter):
    source = "yolink"
    FAST_POLL = 60                   # seconds between full refreshes while MQTT (instant updates) is down
    REFRESH_AFTER_COMMAND = (5, 30)  # re-read a device this long after a command (valves take a while to turn)
    MQTT_RETRY = 30

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.uaid = cfg.get("uaid") or ""
        self.secret = cfg.get("secret_key") or ""
        self.poll_seconds = int(cfg.get("poll_seconds", 900) or 900)
        self.token: str | None = None
        self.token_exp = 0.0
        self.home_id: str | None = None
        self.raw: dict[str, dict] = {}     # deviceId -> yolink device record
        self.http = httpx.AsyncClient(timeout=20)
        self.mqtt_connected = False
        self.mqtt_error = ""
        self._wake = asyncio.Event()

    # ---------------------------------------------------------------- cloud API
    async def _get_token(self, force: bool = False) -> str:
        if not force and self.token and time.time() < self.token_exp - 300:
            return self.token
        r = await self.http.post(f"{API}/open/yolink/token", data={
            "grant_type": "client_credentials", "client_id": self.uaid, "client_secret": self.secret})
        r.raise_for_status()
        j = r.json()
        if not j.get("access_token"):
            raise RuntimeError(f"token: {j.get('msg') or j.get('desc') or 'no access token (check UAID / Secret Key)'}")
        self.token, self.token_exp = j["access_token"], time.time() + int(j.get("expires_in", 7200))
        return self.token

    async def _call(self, method: str, **body) -> dict:
        for attempt in (1, 2):
            token = await self._get_token(force=attempt == 2)
            r = await self.http.post(f"{API}/open/yolink/v2/api",
                                     headers={"Authorization": f"Bearer {token}"},
                                     json={"method": method, "time": int(time.time() * 1000), **body})
            if r.status_code == 401 and attempt == 1:
                continue
            r.raise_for_status()
            j = r.json()
            code = str(j.get("code"))
            if code in AUTH_CODES and attempt == 1:
                continue                     # access token expired early: refresh once and retry
            if code != "000000":
                raise RuntimeError(f"{method}: {j.get('desc')} ({code})")
            return j.get("data") or {}
        return {}

    async def stop(self):
        await super().stop()
        await self.http.aclose()

    async def start(self):
        if not (self.uaid and self.secret):
            self.status = "enter your UAID and Secret Key (Settings > YoLink)"
            return
        self.spawn("poll", self._poll_loop)
        self.spawn("mqtt", self._mqtt_loop)

    # ---------------------------------------------------------------- devices
    @staticmethod
    def _split(dev_native: str) -> tuple[str, int]:
        return (dev_native[:-len(VALVE2)], 1) if dev_native.endswith(VALVE2) else (dev_native, 0)

    async def _discover(self):
        self.home_id = (await self._call("Home.getGeneralInfo"))["id"]
        skipped = set()
        for d in (await self._call("Home.getDeviceList"))["devices"]:
            caps = caps_for(d["type"], d.get("modelName"))
            if d["type"] not in TYPE_CAPS:
                skipped.add(d["type"])
                continue
            self.raw[d["deviceId"]] = d
            model = d.get("modelName") or d["type"]
            if d["type"] in TYPE_LABEL:
                model = f"{model} · YoLink {TYPE_LABEL[d['type']]}"
            self._upsert(self.dev_id(d["deviceId"]), d["name"], model, caps, d["type"])
            if d["type"] == "WaterMeterMultiController":
                self._upsert(self.dev_id(d["deviceId"] + VALVE2), f"{d['name']} (valve 2)", model, ["valve"], d["type"])
        if skipped:
            self.log.info("yolink: device types not shown yet: %s", ", ".join(sorted(skipped)))

    def _upsert(self, hid: str, name: str, model: str, caps: list[str], dtype: str):
        old = self.hub.devices.get(hid)   # (re-discovery keeps values, battery, online state)
        dev = Device(id=hid, name=name, source=self.source, model=model,
                     capabilities={c: cap(c) for c in caps}, attributes={"yolink_type": dtype})
        if old:
            dev.online, dev.last_seen = old.online, old.last_seen
        self.hub.upsert_device(dev)

    def _apply(self, did: str, rec: Any, online: bool | None = True) -> dict:
        """Push one state record of device `did` into the hub (also its 2nd valve)."""
        d = self.raw.get(did)
        if not d:
            return {}
        vals = normalize(d["type"], rec if isinstance(rec, dict) else {"state": rec})
        hid = self.dev_id(did)
        dev = self.hub.devices.get(hid)
        if dev is not None:
            if "battery" in vals and "battery" not in dev.capabilities:
                dev.capabilities["battery"] = cap("battery")
            if is_mains(rec) and "battery" in dev.capabilities and d["type"] in VALVE_TYPES:
                dev.capabilities.pop("battery")    # on mains: no fake battery
        main = {k: v for k, v in vals.items() if k != "valve_2"}
        self.hub.set_values(hid, main, online=online)
        if "valve_2" in vals:
            self.hub.set_values(self.dev_id(did + VALVE2), {"valve": vals["valve_2"]}, online=online)
        return vals

    async def _refresh(self, did: str) -> None:
        d = self.raw[did]
        data = await self._call(f"{d['type']}.getState", targetDevice=did, token=d["token"])
        # data = {"online": …, "state": {record}, "reportAt": …}; some types answer with the record's fields
        # next to "online". normalize() looks through data, data.state and data.state.state, so pass it whole.
        online = data.get("online")
        self._apply(did, data, online=True if online is None else bool(online))

    @property
    def interval(self) -> int:
        if self.mqtt_connected:
            return self.poll_seconds
        # fast fallback, but stay under YoLink's rate limit (~100 calls / 5 min per account)
        return min(self.poll_seconds, max(self.FAST_POLL, 4 * len(self.raw)))

    def _set_status(self) -> None:
        if self.status.startswith("error") or self.status == "starting":
            return
        if self.mqtt_connected:
            self.status = "ok"
        else:
            why = f" ({self.mqtt_error or 'connecting'})"
            self.status = (f"connected – instant updates (MQTT) are down{why}, so devices are re-read "
                           f"every {self.interval} s")

    async def _poll_loop(self):
        await self._discover()
        self.status = "ok"
        self._set_status()
        while True:
            for did, d in list(self.raw.items()):
                try:
                    await self._refresh(did)
                    self.poll_ok(self.dev_id(did))
                except Exception as e:  # noqa: BLE001
                    self.log.debug("state %s: %s", d["name"], e)
                    self.poll_failed(self.dev_id(did), e, [self.dev_id(x) for x in self.raw])
                await asyncio.sleep(1)      # stay well under YoLink rate limits
            self._set_status()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except TimeoutError:
                pass
            try:
                await self._discover()      # pick up new / renamed devices
            except Exception as e:  # noqa: BLE001
                self.log.debug("rediscover: %s", e)

    # ---------------------------------------------------------------- MQTT (instant updates)
    def _mqtt_down(self, why: str) -> None:
        was = self.mqtt_connected
        self.mqtt_connected, self.mqtt_error = False, why
        if was:
            self._wake.set()                # switch the poller to the fast interval now
        self._set_status()

    async def _mqtt_loop(self):
        while not self.home_id:
            await asyncio.sleep(2)
        topic = f"yl-home/{self.home_id}/+/report"
        while True:
            try:
                # the MQTT username is the access token (no password); it expires, so always use a fresh one
                token = await self._get_token(force=time.time() > self.token_exp - 600)
                renew = asyncio.timeout(max(60, self.token_exp - time.time() - 300))
                async with renew:
                    async with aiomqtt.Client(MQTT_HOST, MQTT_PORT, username=token, password="",
                                              keepalive=60) as client:
                        await client.subscribe(topic)
                        self.log.info("subscribed to %s", topic)
                        if not self.mqtt_connected:
                            self.mqtt_connected, self.mqtt_error = True, ""
                            self._wake.set()    # catch up on anything missed while disconnected
                            self._set_status()
                        async for msg in client.messages:
                            self.handle_message(str(msg.topic), msg.payload)
                    raise aiomqtt.MqttError("connection closed")
            except TimeoutError as e:
                if renew.expired():
                    self.token = None       # token about to expire: reconnect right away with a new one
                    continue
                self.log.warning("yolink MQTT: timed out (retry in %ss)", self.MQTT_RETRY)
                self._mqtt_down(f"no answer: {e}" if str(e) else "no answer")
                await asyncio.sleep(self.MQTT_RETRY)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log.warning("yolink MQTT: %s (retry in %ss)", e, self.MQTT_RETRY)
                if any(w in str(e).lower() for w in ("auth", "password", "user name")):
                    self.token = None       # rejected token: fetch a new one before the next try
                self._mqtt_down(str(e)[:80] or type(e).__name__)
                await asyncio.sleep(self.MQTT_RETRY)

    def handle_message(self, topic: str, payload: bytes | str) -> None:
        try:
            m = json.loads(payload)
        except (TypeError, ValueError):
            return
        if not isinstance(m, dict):
            return
        parts = topic.split("/")
        did = parts[2] if len(parts) == 4 and parts[3] == "report" else None
        self._on_report(m, did)

    def _on_report(self, m: dict, did: str | None = None):
        did = m.get("deviceId") or did
        ev = str(m.get("event") or "")
        if ev and not ev.endswith(EVENTS):
            return
        if did not in self.raw:
            return
        try:
            self._apply(did, m.get("data") or {}, online=True)
        except Exception as e:  # noqa: BLE001 - one odd message must not stop the MQTT loop
            self.log.warning("yolink: could not read %s from %s: %s", ev, did, e)

    # ---------------------------------------------------------------- commands
    async def command(self, device: Device, capability: str, value: Any):
        did, channel = self._split(device.id.split(":", 1)[1])
        d = self.raw[did]
        data = await self._call(f"{d['type']}.{command_method(d['type'], capability)}", targetDevice=did,
                                token=d["token"], params=command_params(d["type"], capability, value, channel))
        vals = self._apply(did, data if isinstance(data, dict) else {})
        if capability == "valve" and ("valve", "valve_2")[channel] not in vals:
            # the answer didn't carry the new state: show the request until the device confirms
            self.hub.set_values(device.id, {"valve": "open" if value == "open" else "closed"})
        # YoLink also pushes <Type>.setState / StatusChange over MQTT; re-read anyway in case MQTT is down
        for delay in self.REFRESH_AFTER_COMMAND:
            t = asyncio.create_task(self._refresh_later(did, delay), name="yolink:refresh")
            self._tasks.append(t)
            t.add_done_callback(lambda t: t in self._tasks and self._tasks.remove(t))

    async def _refresh_later(self, did: str, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self._refresh(did)
        except Exception as e:  # noqa: BLE001
            self.log.debug("refresh after command %s: %s", did, e)
