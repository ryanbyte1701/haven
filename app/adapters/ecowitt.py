"""Ecowitt local push receiver.

In the WS View Plus / Ecowitt app: Weather Services > Customized
  Protocol: Ecowitt   Server: <docker host IP>   Port: 1701   Path: /data/report/<secret>   Interval: 60
The gateway POSTs form data; each sensor group becomes its own device.

Gateways can't authenticate, so a gateway is only listened to once it is trusted:
- pushes to the secret path (/data/report/<secret>, shown in Settings) trust that gateway automatically;
- pushes to plain /data/report from an unknown PASSKEY wait in Settings for you to approve them;
- PASSKEYs listed in config (ecowitt.passkeys) are always trusted.
A gateway that was trusted through the secret path stays bound to it: a plain /data/report push with its
PASSKEY (which anyone who has seen the PASSKEY could send) is ignored.
At most MAX_GATEWAYS gateways are trusted and MAX_PENDING wait for approval; a waiting entry expires after
PENDING_TTL, and when the list is full the oldest entry is replaced (an address's own entries first, so one
sender can't push everybody else's gateways out).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from typing import Any

from ..models import Device, cap, f_to_c
from .base import Adapter

INHG_TO_HPA = 33.8639


MAX_CHANNEL = 16
MAX_GROUPS_PER_GATEWAY = 64
MAX_GATEWAYS = 4
MAX_PENDING = 4
PENDING_TTL = 24 * 3600
STATE_FILE = "ecowitt_gateways.json"


def gw_id(passkey: str) -> str:
    return hashlib.sha256(passkey.encode()).hexdigest()[:12]


def parse(form: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Return {group: {"label": str, "values": {cap: value}, "units": {cap: unit}}}."""
    groups: dict[str, dict] = {}

    def put(group, label, name, value, unit=None):
        ch = re.search(r"(\d+)$", group)
        if ch and not 1 <= int(ch[1]) <= MAX_CHANNEL:
            return                                   # gateways have at most 16 channels per kind
        g = groups.setdefault(group, {"label": label, "values": {}, "units": {}})
        g["values"][name] = value
        if unit:
            g["units"][name] = unit

    def num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

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
        pk = cfg.get("passkeys") or []
        if isinstance(pk, str):          # the Settings field is a (masked) comma-separated password field
            pk = re.split(r"[,\s]+", pk)
        self.passkeys: list[str] = [str(p).strip() for p in pk if str(p).strip()]
        self.known: set[str] = set()
        self.pending: dict[str, dict] = {}        # gw_id -> {passkey, model, ip, first_seen, last_seen, count}
        self._load_state()
        self._refresh_status()

    # ---- trusted gateways (persisted next to settings) ----
    def _state_path(self):
        from ..config import conf_dir
        return conf_dir() / STATE_FILE

    def _load_state(self) -> None:
        self.approved: dict[str, dict] = {}       # gw_id -> {passkey, model, added}
        self.push_secret = ""
        try:
            data = json.loads(self._state_path().read_text())
            self.approved = {k: v for k, v in (data.get("approved") or {}).items() if isinstance(v, dict) and v.get("passkey")}
            self.push_secret = str(data.get("push_secret") or "")
        except (OSError, ValueError, AttributeError):
            pass
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", self.push_secret):
            self.push_secret = secrets.token_urlsafe(18)
            self._save_state()

    def _save_state(self) -> None:
        from ..config import write_private
        try:
            write_private(self._state_path(), json.dumps({"approved": self.approved, "push_secret": self.push_secret}, indent=2))
        except OSError as e:
            self.log.warning("ecowitt: can't save trusted gateways: %s", e)

    def trusted(self, passkey: str) -> bool:
        return passkey in self.passkeys or gw_id(passkey) in self.approved

    def approve(self, gid: str) -> bool:
        p = self.pending.pop(gid, None)
        if not p or len(self.approved) >= MAX_GATEWAYS:
            if p:
                self.pending[gid] = p
            return False
        self.approved[gid] = {"passkey": p["passkey"], "model": p.get("model"), "added": time.time(), "via_secret": False}
        self._save_state()
        self._refresh_status()
        return True

    def forget(self, gid: str) -> bool:
        hit = self.approved.pop(gid, None) or self.pending.pop(gid, None)
        if hit:
            self._save_state()
            self._refresh_status()
        return bool(hit)

    def check_secret(self, given: str) -> bool:
        return bool(given) and secrets.compare_digest(given.encode(), self.push_secret.encode())

    def gateways(self) -> dict:
        mask = lambda pk: "…" + pk[-4:] if len(pk) > 4 else "…"   # noqa: E731
        self._expire_pending(time.time())
        return {
            "push_path": f"/data/report/{self.push_secret}",
            "approved": [{"id": k, "passkey": mask(v["passkey"]), "model": v.get("model"),
                          "via_secret": bool(v.get("via_secret"))} for k, v in self.approved.items()]
            + [{"id": gw_id(p), "passkey": mask(p), "model": None, "from_config": True} for p in self.passkeys],
            "pending": [{"id": k, "passkey": mask(v["passkey"]), "model": v.get("model"), "ip": v.get("ip"),
                         "last_seen": v.get("last_seen"), "count": v.get("count")} for k, v in self.pending.items()],
            "max": MAX_GATEWAYS,
        }

    def _refresh_status(self) -> None:
        if self.pending:
            self.status = f"needs approval: {len(self.pending)} new gateway{'s' if len(self.pending) > 1 else ''} waiting"
        elif self.status.startswith("needs approval") or self.status == "starting":
            self.status = "ok" if self.known else "waiting for gateway push"

    def _expire_pending(self, now: float) -> None:
        for k in [k for k, v in self.pending.items() if now - v.get("last_seen", v.get("first_seen", 0)) > PENDING_TTL]:
            self.pending.pop(k, None)

    def _evict_pending(self, ip: str | None) -> None:
        """Make room for one more waiting gateway: the sender's own oldest entry first, then the oldest entry of
        an address that has several, then the oldest overall."""
        age = lambda k: self.pending[k].get("last_seen", 0)   # noqa: E731
        same = [k for k, v in self.pending.items() if v.get("ip") == ip]
        if not same:
            counts: dict = {}
            for v in self.pending.values():
                counts[v.get("ip")] = counts.get(v.get("ip"), 0) + 1
            same = [k for k, v in self.pending.items() if counts[v.get("ip")] > 1] or list(self.pending)
        if same:
            self.pending.pop(min(same, key=age), None)

    def ingest(self, form: dict[str, str], via_secret: bool = False, ip: str | None = None) -> str:
        """Returns "ok", "pending" (unknown gateway waiting for approval, or a secret-path gateway pushing to the
        plain path: ignored) or "full" (no PASSKEY)."""
        passkey = str(form.get("PASSKEY") or "")[:128]
        if not passkey:
            return "full"
        gid = gw_id(passkey)
        now = time.time()
        if passkey not in self.passkeys and not via_secret and (self.approved.get(gid) or {}).get("via_secret"):
            # trusted because it pushed to the secret path: it must keep doing so
            self.log.debug("ecowitt: ignoring plain /data/report push for a gateway bound to the secret path")
            return "pending"
        if not self.trusted(passkey):
            if via_secret and len(self.approved) < MAX_GATEWAYS:
                self.approved[gid] = {"passkey": passkey, "model": form.get("model") or form.get("stationtype"),
                                      "added": now, "via_secret": True}
                self._save_state()
                self.pending.pop(gid, None)
                self.log.info("ecowitt: trusted a new gateway (pushed to the secret path)")
            else:
                self._expire_pending(now)
                if gid not in self.pending and len(self.pending) >= MAX_PENDING:
                    self._evict_pending(ip)
                p = self.pending.setdefault(gid, {"passkey": passkey, "first_seen": now, "count": 0})
                p.update(model=(form.get("model") or form.get("stationtype") or "")[:40], ip=ip,
                         last_seen=now, count=p["count"] + 1)
                self._refresh_status()
                return "pending"
        gw = passkey[-6:]
        model = form.get("model") or form.get("stationtype")
        for group, g in parse(form).items():
            did = self.dev_id(f"{gw}_{group}")
            if did not in self.known:
                if sum(1 for k in self.known if k.startswith(self.dev_id(f"{gw}_"))) >= MAX_GROUPS_PER_GATEWAY:
                    continue                         # don't let one sender create unbounded devices
                caps = {n: cap(n, unit=g["units"].get(n) or cap(n).unit) for n in g["values"]}
                self.hub.upsert_device(Device(
                    id=did, name=self.names.get(group, g["label"]), source=self.source,
                    model=model, room=self.rooms.get(group), capabilities=caps))
                self.known.add(did)
            self.hub.set_values(did, g["values"])
        self.status = "ok"
        self._refresh_status()
        return "ok"
