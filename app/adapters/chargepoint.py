"""ChargePoint home chargers (Home Flex) via python-chargepoint, the library behind the
community Home Assistant ChargePoint integration.

Sign in once from Settings → ChargePoint with your ChargePoint app email and password. Only
the resulting session token is stored (data/chargepoint.json, owner-only); ChargePoint
refreshes it on every response and Haven saves the newest one. If ChargePoint's bot
protection blocks the password login, paste a session token under Advanced instead.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import DATA_DIR
from ..models import Device, cap
from .base import Adapter

TOKEN_FILE = "chargepoint.json"


def _load(data_dir: Path) -> dict:
    try:
        return json.loads((Path(data_dir) / TOKEN_FILE).read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save(data_dir: Path, data: dict) -> None:
    p = Path(data_dir) / TOKEN_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    try:
        p.chmod(0o600)
    except OSError:
        pass


def charger_status(raw: str | None, plugged_in: bool, connected: bool, session_state: str | None = None) -> str:
    """Map ChargePoint's charging status strings to Haven's charger_status values.

    Home chargers report values such as "AVAILABLE", "CHARGING", "NOT_CHARGING" and sessions
    report e.g. "in_use", "fully_charged", "done"; anything unrecognised falls back on the
    plug/connection flags, so new strings degrade gracefully.
    """
    if not connected:
        return "offline"
    s = f"{raw or ''} {session_state or ''}".lower()
    if any(w in s for w in ("fault", "error", "maintenance", "unreachable")):
        return "fault"
    if "not_charging" in s or "not charging" in s:
        return "plugged_in" if plugged_in else "available"
    if any(w in s for w in ("fully_charged", "done", "complete", "finished")):
        return "done" if plugged_in else "available"
    if "charging" in s or "in_use" in s:
        return "charging"
    return "plugged_in" if plugged_in else "available"


def session_values(sess: Any) -> dict[str, Any]:
    """Live values from an active ChargingSession."""
    out: dict[str, Any] = {}
    kw = getattr(sess, "power_kw", None)
    if kw is not None:
        out["power"] = round(float(kw) * 1000)
    kwh = getattr(sess, "energy_kwh", None)
    if kwh is not None:
        out["energy_session"] = round(float(kwh), 2)
    start = getattr(sess, "start_time", None)
    if isinstance(start, datetime):
        out["charge_time"] = max(0, round((datetime.now(timezone.utc) - start).total_seconds() / 60))
    return out


STATUS_TEXT = {"available": "Ready", "plugged_in": "Plugged in", "charging": "Charging", "done": "Charged",
               "fault": "Needs attention", "offline": "Offline"}


class ChargePointAdapter(Adapter):
    source = "chargepoint"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.poll_seconds = int(cfg.get("poll_seconds") or 60)
        self.client = None
        self.chargers: dict[str, int] = {}          # our device id -> charger id
        self.session_ids: dict[str, int] = {}       # our device id -> active session id

    async def start(self):
        self.spawn("poll", self._main, max_delay=300)

    async def stop(self):
        await super().stop()
        if self.client:
            try:
                await self.client.close()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    # ---------- sign-in (Settings → ChargePoint → Sign in) ----------
    @classmethod
    async def signin(cls, values: dict, data_dir) -> dict:
        from python_chargepoint import ChargePoint
        from python_chargepoint.exceptions import DatadomeCaptcha, LoginError
        username = (values.get("username") or "").strip()
        password = values.get("password") or ""
        if not username or not password:
            raise ValueError("Enter your ChargePoint email and password")
        client = await ChargePoint.create(username)
        try:
            await client.login_with_password(password)
            _save(data_dir, {"username": username, "token": client.coulomb_token})
            return {"ok": True}
        except DatadomeCaptcha:
            raise ValueError("ChargePoint's bot protection blocked the sign-in. Sign in at chargepoint.com in a "
                             "browser, copy the coulomb_sess cookie, and paste it under Advanced → Session token.")
        except LoginError:
            raise ValueError("ChargePoint rejected that email or password")
        finally:
            await client.close()

    # ---------- polling ----------
    async def _connect(self):
        from python_chargepoint import ChargePoint
        saved = _load(DATA_DIR)
        username = saved.get("username") or self.cfg.get("username") or ""
        token = self.cfg.get("session_token") or saved.get("token") or ""
        if not username or not token:
            self.status = "needs sign-in (Settings > ChargePoint)"
            while not (_load(DATA_DIR).get("token")):
                await asyncio.sleep(15)
            saved = _load(DATA_DIR)
            username, token = saved["username"], saved["token"]
        self.client = await ChargePoint.create(username, token)
        self._username = username

    def _persist_token(self):
        tok = self.client.coulomb_token if self.client else None
        if tok and tok != _load(DATA_DIR).get("token"):
            _save(DATA_DIR, {"username": self._username, "token": tok})

    async def _main(self):
        if self.client:
            await self.client.close()
            self.client = None
        await self._connect()
        for cid in await self.client.get_home_chargers():
            did = self.dev_id(str(cid))
            self.chargers[did] = cid
            st = await self.client.get_home_charger_status(cid)
            stop_ok = True
            try:
                tech = await self.client.get_home_charger_technical_info(cid)
                stop_ok = bool(getattr(tech, "stop_charge_supported", True))
                self.hub.set_attributes(did, {"firmware": tech.software_version, "serial": tech.serial_number})
            except Exception:  # noqa: BLE001
                pass
            limits = sorted(getattr(st, "possible_amperage_limits", []) or [])
            caps = {
                "charger_status": cap("charger_status"), "charging": cap("charging"), "power": cap("power"),
                "energy_session": cap("energy_session", unit="kWh", label="This session"),
                "charge_time": cap("charge_time", unit="min"), "status_text": cap("status_text"),
                "action": cap("action", options=["start", "stop"] if stop_ok else ["start"]),
            }
            if limits:
                step = (limits[1] - limits[0]) if len(limits) > 1 else 1
                caps["amperage_limit"] = cap("amperage_limit", unit="A", writable=True,
                                             min=limits[0], max=limits[-1], step=step, label="Charge limit")
            self.hub.upsert_device(Device(id=did, name=f"ChargePoint {st.model or 'Home Flex'}", source=self.source,
                                          model=" ".join(x for x in (st.brand, st.model) if x) or None,
                                          capabilities=caps, attributes={"kind": "ev_charger", "charger_id": cid}))
        self.status = "ok" if self.chargers else "no home chargers on this account"
        while True:
            await self._refresh()
            self._persist_token()
            await asyncio.sleep(self.poll_seconds)

    async def _refresh(self):
        user_status = None
        try:
            user_status = await self.client.get_user_charging_status()
        except Exception as e:  # noqa: BLE001
            self.log.debug("charging status: %s", e)
        for did, cid in self.chargers.items():
            st = await self.client.get_home_charger_status(cid)
            vals: dict[str, Any] = {"amperage_limit": st.amperage_limit or None}
            sess_state = None
            here = user_status and any(int(getattr(s, "id", 0)) == cid for s in (user_status.stations or []))
            if here and user_status.session_id:
                self.session_ids[did] = user_status.session_id
                try:
                    sess = await self.client.get_charging_session(user_status.session_id)
                    sess_state = sess.charging_state
                    vals.update(session_values(sess))
                except Exception as e:  # noqa: BLE001
                    self.log.debug("session: %s", e)
            else:
                self.session_ids.pop(did, None)
                vals.update(power=0)
            status = charger_status(st.charging_status, st.is_plugged_in, st.is_connected, sess_state)
            vals.update(charger_status=status, charging=status == "charging", status_text=STATUS_TEXT[status])
            self.hub.set_values(did, vals, online=st.is_connected)

    async def command(self, device: Device, capability: str, value: Any):
        cid = self.chargers[device.id]
        if capability == "amperage_limit":
            await self.client.set_amperage_limit(cid, int(value))
            self.hub.set_values(device.id, {"amperage_limit": int(value)})
        elif capability == "action" and value == "start":
            await self.client.start_charging_session(cid)
            self.hub.set_values(device.id, {"charger_status": "charging", "charging": True, "status_text": "Charging"})
        elif capability == "action" and value == "stop":
            sid = self.session_ids.get(device.id)
            if not sid:
                st = await self.client.get_user_charging_status()
                sid = st.session_id if st else None
            if not sid:
                raise ValueError("No active charging session to stop")
            sess = await self.client.get_charging_session(sid)
            await sess.stop()
            self.hub.set_values(device.id, {"charging": False, "power": 0})
        else:
            raise ValueError(f"{capability} isn't controllable")
        await asyncio.sleep(3)
        await self._refresh()


ADAPTER = ChargePointAdapter
SETTINGS = {
    "id": "chargepoint", "label": "ChargePoint", "icon": "electrical_services",
    "help": "Home Flex and other ChargePoint home chargers. Turn on, save, then Sign in with the email and password "
            "you use in the ChargePoint app. Only a session token is kept. If ChargePoint's bot protection blocks "
            "the sign-in, sign in at chargepoint.com in a browser, copy the coulomb_sess cookie and paste it under "
            "Advanced → Session token.",
    "login": True,
    "login_fields": [{"key": "username", "label": "ChargePoint email"},
                     {"key": "password", "label": "Password", "type": "password"}],
    "login_help": "Your password is only used once to get a session token; it isn't stored.",
    "fields": [
        {"key": "poll_seconds", "label": "Refresh every (seconds)", "type": "number", "default": 60},
        {"key": "username", "label": "ChargePoint email (only needed with a session token)", "type": "text"},
        {"key": "session_token", "label": "Session token (coulomb_sess cookie)", "type": "password"},
    ],
}
