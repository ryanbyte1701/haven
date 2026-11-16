"""Philips Hue Bridge, local CLIP v2 API.

Pair once from Settings > Philips Hue (press the round link button on the bridge first).
The bridge address and app key are saved to /data/hue.json. Changes arrive instantly over
the bridge's event stream; a full refresh runs every 60 s as a fallback.

Lights become devices with: light (on/off), brightness (%), color_temp (K, white-ambiance and
color bulbs), color ("#rrggbb", color bulbs). Hue rooms and zones become "group" devices that
switch every light in them at once.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

EVENT_READ_TIMEOUT = 120

from ..color import hex_to_xy, kelvin_to_mirek, mirek_to_kelvin, xy_to_hex
from ..config import DATA_DIR
from ..models import Device, cap
from .base import Adapter

PAIR_FILE = "hue.json"
RESOURCES = ("light", "room", "zone", "grouped_light", "device", "zigbee_connectivity")
DEFAULT_MIREK = (153, 500)


# ------------------------------------------------------------------ pairing helpers
def load_pairing() -> dict:
    try:
        return json.loads((DATA_DIR / PAIR_FILE).read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_pairing(host: str, app_key: str, client_key: str | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / PAIR_FILE
    from ..config import write_private
    write_private(p, json.dumps({"host": host, "app_key": app_key, "client_key": client_key}))


async def discover() -> list[dict]:
    """Find bridges via Philips' discovery service. Returns [] if it can't be reached."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://discovery.meethue.com/")
            r.raise_for_status()
            return [{"id": b.get("id"), "host": b.get("internalipaddress")} for b in r.json() if b.get("internalipaddress")]
    except Exception:  # noqa: BLE001
        return []


async def pair(host: str) -> dict:
    """{"need_button": True} until the link button is pressed, then {"ok": True} and saves the key."""
    async with httpx.AsyncClient(verify=False, timeout=10) as c:  # noqa: S501 (bridge uses a self-signed cert)
        r = await c.post(f"https://{host}/api", json={"devicetype": "haven#server", "generateclientkey": True})
        r.raise_for_status()
        body = r.json()
    item = body[0] if isinstance(body, list) and body else {}
    if "error" in item:
        if item["error"].get("type") == 101:
            return {"need_button": True}
        raise RuntimeError(item["error"].get("description", "pairing failed"))
    ok = item.get("success") or {}
    if not ok.get("username"):
        raise RuntimeError("unexpected response from bridge")
    save_pairing(host, ok["username"], ok.get("clientkey"))
    return {"ok": True}


# ------------------------------------------------------------------ state mapping
def light_values(light: dict) -> dict[str, Any]:
    out: dict[str, Any] = {"light": bool((light.get("on") or {}).get("on"))}
    if "dimming" in light:
        out["brightness"] = int(round(light["dimming"].get("brightness", 0)))
    ct = light.get("color_temperature") or {}
    if ct.get("mirek") and ct.get("mirek_valid", True):
        out["color_temp"] = mirek_to_kelvin(ct["mirek"])
    xy = (light.get("color") or {}).get("xy")
    if xy:
        out["color"] = xy_to_hex(xy["x"], xy["y"])
    return out


def light_caps(light: dict) -> tuple[dict, dict]:
    caps = {"light": cap("light")}
    attrs: dict[str, Any] = {"kind": "light"}
    if "dimming" in light:
        caps["brightness"] = cap("brightness")
    if "color_temperature" in light:
        caps["color_temp"] = cap("color_temp")
        sch = light["color_temperature"].get("mirek_schema") or {}
        lo, hi = sch.get("mirek_minimum", DEFAULT_MIREK[0]), sch.get("mirek_maximum", DEFAULT_MIREK[1])
        attrs.update(ct_min=mirek_to_kelvin(hi), ct_max=mirek_to_kelvin(lo), mirek_min=lo, mirek_max=hi)
    if "color" in light:
        caps["color"] = cap("color")
    return caps, attrs


def deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


class HueAdapter(Adapter):
    source = "hue"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        saved = load_pairing()
        self.host = cfg.get("host") or saved.get("host")
        self.app_key = cfg.get("app_key") or (saved.get("app_key") if not cfg.get("host") or saved.get("host") == cfg.get("host") else None)
        self.poll_seconds = int(cfg.get("poll_seconds", 60))
        self.res: dict[str, dict[str, dict]] = {t: {} for t in RESOURCES}
        self.http: httpx.AsyncClient | None = None

    # ---------- ids ----------
    def light_did(self, light_id: str) -> str:
        return self.dev_id(light_id)

    def group_did(self, grouped_id: str) -> str:
        return self.dev_id(f"group_{grouped_id}")

    # ---------- lifecycle ----------
    async def start(self):
        self.spawn("main", self._main)

    async def stop(self):
        await super().stop()
        if self.http:
            await self.http.aclose()

    async def _wait_for_pairing(self):
        if self.host and self.app_key:
            return
        self.status = "needs pairing (Settings > Philips Hue)"
        while not (self.host and self.app_key):
            await asyncio.sleep(10)
            saved = load_pairing()
            self.host = self.host or saved.get("host")
            if saved.get("host") == self.host:
                self.app_key = saved.get("app_key")

    async def _main(self):
        await self._wait_for_pairing()
        if self.http:
            await self.http.aclose()
        self.http = httpx.AsyncClient(base_url=f"https://{self.host}", verify=False,  # noqa: S501
                                      headers={"hue-application-key": self.app_key},
                                      timeout=httpx.Timeout(10, read=None))
        await self.refresh()
        self.status = "ok"
        poller = asyncio.create_task(self._poll())
        try:
            await self._events()
        finally:
            poller.cancel()

    async def _get(self, rtype: str) -> list[dict]:
        r = await self.http.get(f"/clip/v2/resource/{rtype}", timeout=10)
        if r.status_code in (401, 403):
            raise RuntimeError("bridge rejected the app key; pair again in Settings")
        r.raise_for_status()
        return r.json().get("data", [])

    async def refresh(self):
        for t in RESOURCES:
            self.res[t] = {x["id"]: x for x in await self._get(t)}
        self.sync()

    async def _poll(self):
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001
                self.log.debug("poll failed: %s", e)

    async def _events(self):
        # The bridge sends a keep-alive at least every minute: a silent stream for 2 minutes is dead
        # (bridge rebooted / IP changed) and must be reopened rather than waited on forever.
        async with self.http.stream("GET", "/eventstream/clip/v2", headers={"Accept": "text/event-stream"},
                                    timeout=httpx.Timeout(10, read=EVENT_READ_TIMEOUT)) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    try:
                        self.apply_events(json.loads(line[5:].strip()))
                    except ValueError:
                        continue
        raise ConnectionError("Hue event stream closed")

    # ---------- mapping (pure: also used by tests) ----------
    def apply_events(self, batch: list[dict]) -> None:
        changed = False
        for ev in batch:
            for item in ev.get("data", []):
                t, rid = item.get("type"), item.get("id")
                if t not in self.res or not rid:
                    continue
                if ev.get("type") == "delete":
                    self.res[t].pop(rid, None)
                elif ev.get("type") == "add":
                    self.res[t][rid] = item
                else:
                    deep_merge(self.res[t].setdefault(rid, {"id": rid, "type": t}), item)
                changed = True
        if changed:
            self.sync()

    def _room_of_device(self) -> dict[str, str]:
        out = {}
        for room in self.res["room"].values():
            for ch in room.get("children", []):
                if ch.get("rtype") == "device":
                    out[ch["rid"]] = room["metadata"]["name"]
        return out

    def _online_devices(self) -> dict[str, bool]:
        return {z["owner"]["rid"]: z.get("status") == "connected" for z in self.res["zigbee_connectivity"].values()
                if z.get("owner")}

    def _group_members(self, container: dict) -> list[str]:
        """Light ids in a room (children are devices) or zone (children are lights)."""
        ids = []
        for ch in container.get("children", []):
            if ch.get("rtype") == "light":
                ids.append(ch["rid"])
            elif ch.get("rtype") == "device":
                dev = self.res["device"].get(ch["rid"]) or {}
                ids += [s["rid"] for s in dev.get("services", []) if s.get("rtype") == "light"]
        return [i for i in ids if i in self.res["light"]]

    def sync(self) -> None:
        rooms = self._room_of_device()
        online = self._online_devices()
        for lid, light in self.res["light"].items():
            did = self.light_did(lid)
            owner = (light.get("owner") or {}).get("rid")
            dev_meta = self.res["device"].get(owner) or {}
            caps, attrs = light_caps(light)
            if did not in self.hub.devices or set(caps) - set(self.hub.devices[did].capabilities):
                prod = dev_meta.get("product_data") or {}
                self.hub.upsert_device(Device(
                    id=did, name=(light.get("metadata") or {}).get("name") or dev_meta.get("metadata", {}).get("name") or lid,
                    source=self.source, model=prod.get("product_name") or prod.get("model_id"),
                    room=rooms.get(owner), capabilities=caps))
            self.hub.set_attributes(did, {**attrs, "hue_id": lid, "archetype": (light.get("metadata") or {}).get("archetype")})
            self.hub.set_values(did, light_values(light), online=online.get(owner, True))

        for kind in ("room", "zone"):
            for cont in self.res[kind].values():
                gid = next((s["rid"] for s in cont.get("services", []) if s.get("rtype") == "grouped_light"), None)
                g = self.res["grouped_light"].get(gid) if gid else None
                if not g:
                    continue
                members = self._group_members(cont)
                if not members:
                    continue
                lights = [self.res["light"][m] for m in members]
                caps = {"light": cap("light")}
                if any("dimming" in x for x in lights):
                    caps["brightness"] = cap("brightness")
                if any("color_temperature" in x for x in lights):
                    caps["color_temp"] = cap("color_temp")
                if any("color" in x for x in lights):
                    caps["color"] = cap("color")
                did = self.group_did(gid)
                name = cont["metadata"]["name"]
                if did not in self.hub.devices or set(caps) - set(self.hub.devices[did].capabilities):
                    self.hub.upsert_device(Device(id=did, name=name, source=self.source,
                                                  model=f"Hue {kind}", room=name, capabilities=caps))
                mins = [x["color_temperature"]["mirek_schema"]["mirek_minimum"] for x in lights
                        if (x.get("color_temperature") or {}).get("mirek_schema")]
                maxs = [x["color_temperature"]["mirek_schema"]["mirek_maximum"] for x in lights
                        if (x.get("color_temperature") or {}).get("mirek_schema")]
                attrs = {"kind": "group", "group_type": kind, "hue_id": gid,
                         "members": [self.light_did(m) for m in members]}
                if mins:
                    attrs.update(ct_min=mirek_to_kelvin(min(maxs)), ct_max=mirek_to_kelvin(max(mins)),
                                 mirek_min=max(mins), mirek_max=min(maxs))
                self.hub.set_attributes(did, attrs)
                vals: dict[str, Any] = {"light": bool((g.get("on") or {}).get("on"))}
                if "dimming" in g:
                    vals["brightness"] = int(round(g["dimming"].get("brightness", 0)))
                lead = next((x for x in lights if (x.get("on") or {}).get("on")), lights[0])
                lv = light_values(lead)
                for k in ("color_temp", "color"):
                    if k in caps and k in lv:
                        vals[k] = lv[k]
                self.hub.set_values(did, vals)

    # ---------- commands ----------
    def build_body(self, device: Device, capability: str, value: Any) -> dict:
        if capability == "light":
            return {"on": {"on": bool(value)}}
        if capability == "brightness":
            return {"on": {"on": True}, "dimming": {"brightness": max(1.0, min(100.0, float(value)))}}
        if capability == "color_temp":
            lo = device.attributes.get("mirek_min", DEFAULT_MIREK[0])
            hi = device.attributes.get("mirek_max", DEFAULT_MIREK[1])
            return {"on": {"on": True}, "color_temperature": {"mirek": max(lo, min(hi, kelvin_to_mirek(int(value))))}}
        if capability == "color":
            x, y = hex_to_xy(str(value))
            return {"on": {"on": True}, "color": {"xy": {"x": x, "y": y}}}
        raise ValueError(f"unsupported capability {capability}")

    async def command(self, device: Device, capability: str, value: Any):
        body = self.build_body(device, capability, value)
        is_group = device.attributes.get("kind") == "group"
        rtype = "grouped_light" if is_group else "light"
        rid = device.attributes.get("hue_id")
        r = await self.http.put(f"/clip/v2/resource/{rtype}/{rid}", json=body, timeout=10)
        r.raise_for_status()
        errs = r.json().get("errors") or []
        if errs:
            raise RuntimeError(errs[0].get("description", "bridge error"))
        # optimistic update; the event stream confirms within a moment
        vals: dict[str, Any] = {"light": bool(value) if capability == "light" else True}
        if capability == "brightness":
            vals["brightness"] = int(round(float(value)))
        elif capability == "color_temp":
            vals["color_temp"] = mirek_to_kelvin(body["color_temperature"]["mirek"])
        elif capability == "color":
            vals["color"] = str(value).lower()
        self.hub.set_values(device.id, vals)
