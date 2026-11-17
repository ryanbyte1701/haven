"""Aqara Open API (developer.aqara.com) — cloud polling.

Create a project on the Aqara developer portal to get App ID / App Key / Key ID, then
generate an access + refresh token for your Aqara account. Tokens are refreshed
automatically and saved to /data/aqara_token.json.

Resource IDs differ by model. Defaults below cover common sensors; override per model in
config (`aqara.resources`) and use GET /api/aqara/resources/<model> to list what a model offers.

Alternative: pair Aqara Zigbee devices to Zigbee2MQTT (or expose a hub over Matter→MQTT)
and they'll appear through the MQTT adapter with local, instant updates.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

import httpx

from ..config import DATA_DIR
from ..models import Device, cap
from .base import Adapter

REGIONS = {
    "usa": "https://open-usa.aqara.com", "eu": "https://open-ger.aqara.com",
    "cn": "https://open-cn.aqara.com", "kr": "https://open-kr.aqara.com",
    "ru": "https://open-ru.aqara.com", "sg": "https://open-sg.aqara.com",
}

# resourceId -> (capability, transform)
COMMON: dict[str, tuple[str, Any]] = {
    "0.1.85": ("temperature", lambda v: round(int(v) / 100, 2)),
    "0.2.85": ("humidity", lambda v: round(int(v) / 100, 1)),
    "0.3.85": ("pressure", lambda v: round(int(v) / 100, 1)),
    "0.4.85": ("illuminance", int),
    "8.0.2001": ("battery", int),
    "4.1.85": ("switch", lambda v: str(v) == "1"),
}
# 3.1.85 is the "primary status" resource; meaning depends on model family
STATUS_BY_MODEL = [
    ("magnet", "contact", lambda v: "open" if str(v) == "1" else "closed"),
    ("wleak", "leak", lambda v: str(v) == "1"),
    ("flood", "leak", lambda v: str(v) == "1"),
    ("motion", "motion", lambda v: str(v) == "1"),
    ("presence", "presence", lambda v: str(v) == "1"),
    ("vibration", "vibration", lambda v: str(v) == "1"),
    ("smoke", "smoke", lambda v: str(v) == "1"),
]


class AqaraAdapter(Adapter):
    source = "aqara"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.base = REGIONS.get(cfg.get("region", "usa"), cfg.get("region"))
        self.app_id, self.app_key, self.key_id = (cfg.get("app_id") or "", cfg.get("app_key") or "",
                                                  cfg.get("key_id") or "")
        self.poll_seconds = int(cfg.get("poll_seconds", 60))
        self.custom: dict[str, dict[str, str]] = cfg.get("resources", {})  # model -> {rid: cap}
        self.token_file = DATA_DIR / "aqara_token.json"
        tok = self._load_tokens()
        self.access = tok.get("access_token") or cfg.get("access_token")
        self.refresh = tok.get("refresh_token") or cfg.get("refresh_token")
        self.refreshed_at = tok.get("refreshed_at", 0)
        self.models: dict[str, str] = {}    # did -> model
        self.http = httpx.AsyncClient(timeout=20)

    def _load_tokens(self) -> dict:
        try:
            return json.loads(self.token_file.read_text())
        except Exception:  # noqa: BLE001 - no saved token yet
            return {}

    def _headers(self, with_token=True) -> dict:
        nonce, ts = uuid.uuid4().hex, str(int(time.time() * 1000))
        parts = []
        if with_token and self.access:
            parts.append(f"Accesstoken={self.access}")
        parts += [f"Appid={self.app_id}", f"Keyid={self.key_id}", f"Nonce={nonce}", f"Time={ts}"]
        # MD5 is what Aqara's API signature requires (not used for anything security-sensitive on our side)
        sign = hashlib.md5(("&".join(parts) + self.app_key).lower().encode(), usedforsecurity=False).hexdigest()
        h = {"Appid": self.app_id, "Keyid": self.key_id, "Nonce": nonce, "Time": ts,
             "Sign": sign, "Lang": "en", "Content-Type": "application/json"}
        if with_token and self.access:
            h["Accesstoken"] = self.access
        return h

    async def _call(self, intent: str, data: Any, with_token=True, retry=True) -> Any:
        r = await self.http.post(f"{self.base}/v3.0/open/api", headers=self._headers(with_token),
                                 json={"intent": intent, "data": data})
        r.raise_for_status()
        j = r.json()
        if j.get("code") in (108, 2010) and retry:      # token expired / invalid
            await self._refresh_token()
            return await self._call(intent, data, with_token, retry=False)
        if j.get("code") != 0:
            raise RuntimeError(f"{intent}: {j.get('message')} ({j.get('code')})")
        return j.get("result")

    async def _refresh_token(self):
        res = await self._call("config.auth.refreshToken", {"refreshToken": self.refresh},
                               with_token=False, retry=False)
        self.access, self.refresh = res["accessToken"], res["refreshToken"]
        self.refreshed_at = time.time()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        from ..config import write_private
        write_private(self.token_file, json.dumps({"access_token": self.access,
                                                   "refresh_token": self.refresh,
                                                   "refreshed_at": self.refreshed_at}))
        self.log.info("Aqara token refreshed")

    def _mapping(self, model: str) -> dict[str, tuple[str, Any]]:
        m = dict(COMMON)
        for needle, capname, fn in STATUS_BY_MODEL:
            if needle in model:
                m["3.1.85"] = (capname, fn)
                break
        for rid, capname in self.custom.get(model, {}).items():
            m[rid] = (capname, lambda v: float(v) if str(v).replace(".", "").isdigit() else v)
        return m

    async def stop(self):
        await super().stop()
        await self.http.aclose()

    async def start(self):
        missing = [n for n, v in (("App ID", self.app_id), ("App Key", self.app_key), ("Key ID", self.key_id)) if not v]
        if missing:
            self.status = f"enter {', '.join(missing)} (Settings > Aqara)"
            return
        self.spawn("poll", self._poll_loop)

    async def _discover(self):
        page = 1
        while True:
            res = await self._call("query.device.info", {"pageNum": page, "pageSize": 100})
            for d in res.get("data", []):
                did, model = d["did"], d.get("model", "")
                if model.startswith("lumi.gateway") or model.startswith("app."):
                    continue
                self.models[did] = model
                self.hub.upsert_device(Device(
                    id=self.dev_id(did), name=d.get("deviceName") or did, source=self.source,
                    model=model, room=d.get("positionName"), online=d.get("state", 1) == 1))
            if page * 100 >= res.get("totalCount", 0):
                break
            page += 1

    async def _poll_loop(self):
        if not self.access and not self.refresh:
            self.status = "needs sign-in (Settings > Aqara > Sign in)"
            self.log.warning(self.status)
            while not self._load_tokens():
                await asyncio.sleep(30)
            tok = self._load_tokens()
            self.access, self.refresh = tok["access_token"], tok["refresh_token"]
            self.refreshed_at = tok.get("refreshed_at", time.time())
        if time.time() - self.refreshed_at > 5 * 86400:
            await self._refresh_token()
        await self._discover()
        self.status = "ok"
        while True:
            items = list(self.models.items())
            for i in range(0, len(items), 20):
                batch = items[i:i + 20]
                req = [{"subjectId": did, "resourceIds": list(self._mapping(m))} for did, m in batch]
                for row in await self._call("query.resource.value", {"resources": req}) or []:
                    did, rid = row["subjectId"], row["resourceId"]
                    capname, fn = self._mapping(self.models.get(did, "")).get(rid, (None, None))
                    if not capname:
                        continue
                    try:
                        val = fn(row["value"])
                    except Exception:  # noqa: BLE001
                        continue
                    dev = self.hub.devices.get(self.dev_id(did))
                    if dev and capname not in dev.capabilities:
                        dev.capabilities[capname] = cap(capname)
                    self.hub.set_values(self.dev_id(did), {capname: val})
            await asyncio.sleep(self.poll_seconds)

    async def resource_info(self, model: str) -> Any:
        return await self._call("query.resource.info", {"model": model})

    async def command(self, device: Device, capability: str, value: Any):
        did = device.id.split(":", 1)[1]
        rid = next((r for r, (c, _) in self._mapping(self.models.get(did, "")).items()
                    if c == capability), None)
        if not rid:
            raise ValueError(f"no writable resource for {capability}")
        v = {"switch": "1" if value else "0", "valve": "1" if value == "open" else "0",
             "lock": "1" if value == "lock" else "0"}.get(capability, str(value))
        await self._call("write.resource.device",
                         [{"subjectId": did, "resources": [{"resourceId": rid, "value": v}]}])
        state = {"close": "closed", "lock": "locked", "unlock": "unlocked"}.get(value, value)
        self.hub.set_values(device.id, {capability: state})   # optimistic; next poll confirms
