"""ecobee thermostats and their remote room sensors, through ecobee's cloud API.

Sign-in (Settings > Ecobee > Sign in), two ways:

* **ecobee account** (email + password, plus a code if two-step verification is on). This is the
  login ecobee's own web portal uses (OAuth2 authorization code + PKCE on auth.ecobee.com), the same
  method Home Assistant's built-in ecobee integration switched to in 2026.3 because ecobee stopped
  accepting new developer registrations. The password is only used for that one sign-in.
* **Developer API key + PIN** (the official third-party flow, for people who already have a key):
  Haven asks ecobee for a 4-character PIN, you authorize it at ecobee.com > My Apps > Add application,
  then Haven exchanges it for tokens.

Only the tokens (and the API key, if used) are kept, in /data/ecobee.json (mode 0600). Access tokens
last an hour and are refreshed a few minutes before they expire; if ecobee revokes access the
integration says "Needs sign-in".

Polling follows ecobee's guidance: the cheap thermostat *summary* is read every 3 minutes and the
full thermostat data is only fetched when its revision numbers (or running equipment) change, plus
right after every command.

Devices: one per thermostat (temperature, humidity, heat/cool setpoints, HVAC mode, fan mode,
running equipment, occupancy, outdoor temperature from ecobee's weather) and one per remote sensor
(temperature, occupancy, humidity where the sensor has it). ecobee reports tenths of a degree °F;
Haven stores °C.

HomeKit (local) control is not supported: it needs a HomeKit controller pairing stack, which Haven
doesn't include.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets as _secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..config import DATA_DIR
from ..models import Device, cap, f_to_c
from .base import Adapter

# Overridable so a local fake ecobee server can be used for testing (never set in production).
API_BASE = os.environ.get("HAVEN_ECOBEE_BASE", "https://api.ecobee.com").rstrip("/")
AUTH_BASE = os.environ.get("HAVEN_ECOBEE_AUTH_BASE", "https://auth.ecobee.com").rstrip("/")

# ecobee's web portal client (public client, PKCE; also used by python-ecobee-api / Home Assistant)
WEB_CLIENT_ID = "183eORFPlXyz9BbDZwqexHPBQoVjgadh"
WEB_REDIRECT = "https://www.ecobee.com/home/authCallback"
WEB_AUDIENCE = "https://prod.ecobee.com/api/v1"
WEB_SCOPE = "openid offline_access smartWrite piiWrite piiRead smartRead deleteGrants"

TOKEN_FILE = "ecobee.json"  # noqa: S105 - a file name, not a password
USER_AGENT = "Haven home dashboard"
REFRESH_MARGIN = 300          # refresh the access token this many seconds before it expires
FULL_REFRESH_EVERY = 3600     # fetch everything at least hourly even if no revision changed
CODE_TTL = 600                # a pending two-step verification is kept 10 minutes

_transport: httpx.AsyncBaseTransport | None = None     # tests swap in an httpx.MockTransport
_pin_pending: dict[str, dict] = {}                     # api key -> {"code", "pin", "expires", "interval"}
_mfa_pending: dict[str, dict] = {}                     # email -> {"client", "url", "state", "verifier", "type", "at"}

MODEL_NAMES = {
    "idtSmart": "ecobee Smart Thermostat", "idtEms": "ecobee Smart EMS Thermostat",
    "siSmart": "ecobee Si Smart Thermostat", "siEms": "ecobee Si EMS Thermostat",
    "athenaSmart": "ecobee3", "athenaEms": "ecobee3 EMS", "corSmart": "Carrier/Bryant Côr",
    "nikeSmart": "ecobee3 lite", "nikeEms": "ecobee3 lite EMS", "apolloSmart": "ecobee4",
    "vulcanSmart": "ecobee SmartThermostat with voice control", "aresSmart": "ecobee Smart Thermostat Premium",
    "artemisSmart": "ecobee Smart Thermostat Enhanced", "attisPro": "ecobee Smart Thermostat Pro",
}
# Haven option -> ecobee hvacMode
HVAC_MODES = {"heat": "heat", "cool": "cool", "auto": "auto", "off": "off", "aux_heat_only": "auxHeatOnly"}
HEAT_EQUIP = {"heatPump", "heatPump2", "heatPump3", "auxHeat1", "auxHeat2", "auxHeat3"}
COOL_EQUIP = {"compCool1", "compCool2"}
ACTION_TEXT = {"heating": "Heating", "cooling": "Cooling", "fan": "Fan running", "idle": "Idle"}
HOLD_TYPES = ["nextTransition", "indefinite"]


class EcobeeError(Exception):
    pass


class AuthRevoked(EcobeeError):
    """ecobee no longer accepts the tokens (revoked, expired refresh token, deleted app): sign in again."""


class AuthPending(EcobeeError):
    """The PIN hasn't been authorized on ecobee.com yet."""


# ---------------------------------------------------------------- token storage
def load_tokens(data_dir=None) -> dict:
    try:
        d = json.loads(((data_dir or DATA_DIR) / TOKEN_FILE).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_tokens(data: dict, data_dir=None) -> None:
    from ..config import write_private
    write_private((data_dir or DATA_DIR) / TOKEN_FILE, json.dumps(data, indent=2))


def usable(tokens: dict) -> bool:
    return bool(tokens.get("refresh_token") and tokens.get("method") in ("account", "pin")
                and (tokens.get("method") != "pin" or tokens.get("api_key")))


def _client(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, transport=_transport, headers={"User-Agent": USER_AGENT}, **kw)


def _json(r: httpx.Response) -> dict:
    try:
        d = r.json()
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _token_record(payload: dict, **extra) -> dict:
    if not payload.get("access_token") or not payload.get("refresh_token"):
        raise EcobeeError("ecobee's answer had no tokens in it")
    try:
        life = float(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        life = 3600
    return {**extra, "access_token": payload["access_token"], "refresh_token": payload["refresh_token"],
            "expires_at": time.time() + life}


# ---------------------------------------------------------------- developer API key + PIN
async def request_pin(api_key: str) -> dict:
    """Step 1 of ecobee's PIN flow: returns {"pin", "code", "expires" (unix time), "interval"}."""
    async with _client() as c:
        r = await c.get(f"{API_BASE}/authorize", params={"response_type": "ecobeePin", "client_id": api_key,
                                                          "scope": "smartWrite"})
    d = _json(r)
    if r.status_code != 200 or not d.get("ecobeePin") or not d.get("code"):
        if d.get("error") in ("invalid_client", "unauthorized_client") or r.status_code in (400, 401, 403):
            raise EcobeeError("ecobee didn't accept that API key. Copy it again from ecobee.com > Developer.")
        raise EcobeeError(f"ecobee didn't hand out a PIN (HTTP {r.status_code})")
    try:
        minutes = float(d.get("expires_in") or 9)
    except (TypeError, ValueError):
        minutes = 9
    return {"pin": str(d["ecobeePin"]), "code": str(d["code"]), "expires": time.time() + minutes * 60,
            "interval": int(d.get("interval") or 30)}


async def exchange_pin(api_key: str, code: str) -> dict:
    """Step 2: after the PIN was authorized on ecobee.com, trade the authorization code for tokens."""
    async with _client() as c:
        r = await c.post(f"{API_BASE}/token", data={"grant_type": "ecobeePin", "code": code, "client_id": api_key})
    d = _json(r)
    if r.status_code == 200 and d.get("access_token"):
        return _token_record(d, method="pin", api_key=api_key)
    err = d.get("error")
    if err in ("authorization_pending", "slow_down"):
        raise AuthPending("not authorized yet")
    if err == "authorization_expired":
        raise EcobeeError("The PIN expired. Close this window and choose Sign in again for a new PIN.")
    raise EcobeeError(f"ecobee refused the PIN ({err or f'HTTP {r.status_code}'})")


async def refresh_pin(api_key: str, refresh_token: str, client: httpx.AsyncClient) -> dict:
    r = await client.post(f"{API_BASE}/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                                      "client_id": api_key})
    d = _json(r)
    if r.status_code == 200 and d.get("access_token"):
        return _token_record(d, method="pin", api_key=api_key)
    if d.get("error") in ("invalid_grant", "invalid_client", "unauthorized_client"):
        raise AuthRevoked(f"ecobee ended the session ({d['error']})")
    raise EcobeeError(f"couldn't refresh the ecobee sign-in (HTTP {r.status_code})")


# ---------------------------------------------------------------- ecobee account (web login)
def _pkce() -> tuple[str, str]:
    verifier = _secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _q(url: str, key: str) -> str:
    return (parse_qs(urlsplit(str(url)).query).get(key) or [""])[0]


async def _walk(client: httpx.AsyncClient, r: httpx.Response) -> str:
    """Follow Auth0's redirects while they stay on auth.ecobee.com; return the final URL (the
    callback with ?code=..., an MFA challenge, or a page we stopped on). Never loads the callback."""
    for _ in range(10):
        if not r.is_redirect:
            return str(r.url)
        nxt = str(r.url.join(r.headers.get("location", "")))
        if not nxt.startswith(AUTH_BASE):
            return nxt
        r = await client.get(nxt)
    raise EcobeeError("ecobee's sign-in redirected too many times")


async def _exchange_code(client: httpx.AsyncClient, code: str, verifier: str, email: str) -> dict:
    r = await client.post(f"{AUTH_BASE}/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": WEB_CLIENT_ID, "redirect_uri": WEB_REDIRECT})
    d = _json(r)
    if r.status_code != 200:
        raise EcobeeError(f"ecobee didn't issue tokens ({d.get('error') or f'HTTP {r.status_code}'})")
    if not d.get("refresh_token"):
        raise EcobeeError("ecobee didn't issue a long-lived sign-in (no refresh token)")
    return _token_record(d, method="account", account=email)


async def _after_login(client: httpx.AsyncClient, url: str, verifier: str, email: str) -> dict:
    """Branch on where Auth0 sent us after the password (or code) was submitted."""
    if "/u/mfa-otp-challenge" in url or "/u/mfa-sms-challenge" in url:
        kind = "sms" if "sms" in url else "otp"
        old = _mfa_pending.pop(email.lower(), None)
        if old and old["client"] is not client:
            await old["client"].aclose()
        _mfa_pending[email.lower()] = {"client": client, "url": url, "state": _q(url, "state"), "verifier": verifier,
                                       "type": kind, "at": time.time()}
        return {"need_code": True, "message": "ecobee texted you a verification code. Enter it here." if kind == "sms"
                else "Enter the 6-digit code from your authenticator app."}
    if "/u/mfa-" in url:
        raise EcobeeError("your ecobee account asks for a kind of two-step verification Haven can't answer (only "
                          "authenticator-app and text-message codes work). Sign in once at ecobee.com, then try again.")
    if "/u/login/password" in url or "/u/login/identifier" in url:
        raise EcobeeError("ecobee didn't accept that email and password")
    if "/u/custom-prompt" in url:
        raise EcobeeError("ecobee wants you to confirm something first (such as new terms). Sign in at ecobee.com, "
                          "accept it, then try again.")
    code = _q(url, "code")
    if not code:
        raise EcobeeError("ecobee's sign-in page answered in a way Haven doesn't recognize")
    return await _exchange_code(client, code, verifier, email)


async def account_login(email: str, password: str) -> dict:
    """Returns a token record, or {"need_code": True, "message": ...} when two-step verification is on."""
    verifier, challenge = _pkce()
    client = _client()
    keep = False
    try:
        r = await client.get(f"{AUTH_BASE}/authorize", follow_redirects=True, params={
            "response_type": "code", "client_id": WEB_CLIENT_ID, "redirect_uri": WEB_REDIRECT,
            "audience": WEB_AUDIENCE, "scope": WEB_SCOPE, "code_challenge": challenge, "code_challenge_method": "S256"})
        if "/u/login/identifier" not in str(r.url):
            raise EcobeeError("ecobee's sign-in page changed; Haven needs an update to use it")
        r = await client.post(str(r.url), follow_redirects=True, data={"state": _q(r.url, "state"), "username": email})
        if "/u/login/password" not in str(r.url):
            raise EcobeeError("ecobee didn't recognize that email address")
        r = await client.post(str(r.url), data={"state": _q(r.url, "state"), "username": email, "password": password})
        res = await _after_login(client, await _walk(client, r), verifier, email)
        keep = bool(res.get("need_code"))
        return res
    finally:
        if not keep:
            await client.aclose()


async def account_code(email: str, code: str) -> dict:
    p = _mfa_pending.get(email.lower())
    if not p or time.time() - p["at"] > CODE_TTL:
        if p:
            _mfa_pending.pop(email.lower(), None)
            await p["client"].aclose()
        raise EcobeeError("The verification step timed out. Close this window and sign in again.")
    client = p["client"]
    r = await client.post(p["url"], data={"state": p["state"], "code": code.strip()})
    if r.status_code == 400:
        raise EcobeeError("ecobee didn't accept that code. Check it and try again.")
    url = await _walk(client, r)
    if "/u/mfa-" in url and "-challenge" in url:
        raise EcobeeError("ecobee didn't accept that code. Check it and try again.")
    _mfa_pending.pop(email.lower(), None)
    try:
        return await _after_login(client, url, p["verifier"], email)
    finally:
        await client.aclose()


async def refresh_account(refresh_token: str, client: httpx.AsyncClient, email: str = "") -> dict:
    r = await client.post(f"{AUTH_BASE}/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                                             "client_id": WEB_CLIENT_ID})
    d = _json(r)
    if r.status_code == 200 and d.get("access_token"):
        d.setdefault("refresh_token", refresh_token)      # Auth0 may or may not rotate it
        return _token_record(d, method="account", account=email)
    if d.get("error") in ("invalid_grant", "unauthorized_client", "access_denied"):
        raise AuthRevoked(f"ecobee ended the session ({d['error']})")
    raise EcobeeError(f"couldn't refresh the ecobee sign-in (HTTP {r.status_code})")


# ---------------------------------------------------------------- data mapping (pure, tested)
def t10(v: Any) -> float | None:
    """ecobee tenths of °F (int or numeric string, "unknown" possible) -> °C."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= -5000:              # -5002 = unknown, -5003 = calibrating
        return None
    c = f_to_c(f / 10)
    return None if c is None else round(c, 2)


def to_t10(c: float) -> int:
    """°C -> ecobee tenths of °F, rounded to a tenth of a degree."""
    return int(round((float(c) * 9 / 5 + 32) * 10))


def hvac_action(equipment: str) -> str:
    eq = {x.strip() for x in str(equipment or "").split(",") if x.strip()}
    if eq & HEAT_EQUIP:
        return "heating"
    if eq & COOL_EQUIP:
        return "cooling"
    if "fan" in eq:
        return "fan"
    return "idle"


def modes_for(settings: dict) -> list[str]:
    heat = settings.get("heatStages")
    cool = settings.get("coolStages")
    hp = bool(settings.get("hasHeatPump"))
    can_heat = heat is None or hp or int(heat or 0) > 0
    can_cool = cool is None or int(cool or 0) > 0
    out = []
    if can_heat:
        out.append("heat")
    if can_cool:
        out.append("cool")
    if can_heat and can_cool and settings.get("autoHeatCoolFeatureEnabled", True) is not False:
        out.append("auto")
    out.append("off")
    if hp:
        out.append("aux_heat_only")
    return out


def _range(settings: dict, lo: str, hi: str, dlo: int, dhi: int) -> tuple[float, float]:
    """Setpoint limits (°C) from the thermostat's settings, with ecobee's defaults when missing."""
    a = t10(settings.get(lo)) if settings.get(lo) is not None else None
    b = t10(settings.get(hi)) if settings.get(hi) is not None else None
    a = t10(dlo) if a is None else a
    b = t10(dhi) if b is None else b
    return (min(a, b), max(a, b))


def _sensor_values(s: dict) -> dict:
    out: dict[str, Any] = {}
    for c in s.get("capability") or []:
        typ, v = c.get("type"), c.get("value")
        if v in (None, "", "unknown"):
            continue
        if typ == "temperature":
            out["temperature"] = t10(v)
        elif typ == "humidity":
            try:
                out["humidity"] = float(v)
            except (TypeError, ValueError):
                pass
        elif typ == "occupancy":
            out["occupancy"] = str(v).lower() == "true"
    return {k: v for k, v in out.items() if v is not None}


def _running_event(t: dict) -> dict | None:
    for e in t.get("events") or []:
        if e.get("running"):
            return e
    return None


def parse_thermostat(t: dict) -> dict:
    """One ecobee thermostat object -> {"id", "name", "model", "online", "values", "caps", "attributes", "sensors"}."""
    rt, st = t.get("runtime") or {}, t.get("settings") or {}
    ident = str(t.get("identifier"))
    mode_e = st.get("hvacMode") or "off"
    mode = next((k for k, v in HVAC_MODES.items() if v == mode_e), mode_e)
    action = hvac_action(t.get("equipmentStatus", ""))
    ev = _running_event(t)
    prog = t.get("program") or {}
    climates = {c.get("climateRef"): c.get("name") for c in prog.get("climates") or []}
    if ev and ev.get("type") == "vacation":
        sched = "Vacation"
    elif ev and ev.get("type") in ("hold", "autoAway", "autoHome", "quickSave"):
        ref = ev.get("holdClimateRef")
        sched = f"Hold · {climates.get(ref, ref)}" if ref else "Hold"
    else:
        sched = climates.get(prog.get("currentClimateRef")) or ""
    status = "Off" if mode == "off" else ACTION_TEXT[action] + (f" · {sched}" if sched else "")
    fan = rt.get("desiredFanMode") or (ev or {}).get("fan") or "auto"
    vals: dict[str, Any] = {
        "temperature": t10(rt.get("actualTemperature")),
        "heat_setpoint": t10(rt.get("desiredHeat")),
        "cool_setpoint": t10(rt.get("desiredCool")),
        "hvac_mode": mode, "fan_mode": "on" if fan == "on" else "auto",
        "hvac_action": action, "status_text": status,
    }
    hum = rt.get("actualHumidity")
    if isinstance(hum, (int, float)) and 0 <= hum <= 100:
        vals["humidity"] = hum
    sensors, own = [], {}
    for s in t.get("remoteSensors") or []:
        sv = _sensor_values(s)
        if s.get("type") == "thermostat":
            own = sv
            continue
        if "temperature" not in sv and "occupancy" not in sv and not any(
                c.get("type") in ("temperature", "occupancy") for c in s.get("capability") or []):
            continue
        sid = re.sub(r"[^A-Za-z0-9_.-]", "-", str(s.get("id") or s.get("code") or s.get("name")))
        sensors.append({"id": f"{ident}_{sid}", "name": s.get("name") or "Sensor", "values": sv,
                        "types": sorted({c.get("type") for c in s.get("capability") or []} & {"temperature", "occupancy", "humidity"}),
                        "kind": s.get("type"), "in_use": bool(s.get("inUse"))})
    if "occupancy" in own:
        vals["occupancy"] = own["occupancy"]
    fc = ((t.get("weather") or {}).get("forecasts") or [{}])[0]
    out_t = t10(fc.get("temperature")) if fc else None
    if out_t is not None:
        vals["outdoor_temperature"] = out_t
    heat_r = _range(st, "heatRangeLow", "heatRangeHigh", 450, 790)
    cool_r = _range(st, "coolRangeLow", "coolRangeHigh", 650, 920)
    modes = modes_for(st)
    if mode not in modes:
        modes.append(mode)
    caps = {
        "temperature": cap("temperature"),
        "heat_setpoint": cap("heat_setpoint", writable=True, unit="°C", min=heat_r[0], max=heat_r[1], step=0.5, label="Heat to"),
        "cool_setpoint": cap("cool_setpoint", writable=True, unit="°C", min=cool_r[0], max=cool_r[1], step=0.5, label="Cool to"),
        "hvac_mode": cap("hvac_mode", writable=True, options=modes, label="Mode"),
        "fan_mode": cap("fan_mode", writable=True, options=["auto", "on"], label="Fan"),
        "hvac_action": cap("hvac_action", label="Running"),
        "status_text": cap("status_text", label="Status"),
        "action": cap("action", options=["resume_schedule"]),
    }
    if "humidity" in vals:
        caps["humidity"] = cap("humidity")
    if "occupancy" in vals:
        caps["occupancy"] = cap("occupancy", label="Occupancy")
    if "outdoor_temperature" in vals:
        caps["outdoor_temperature"] = cap("outdoor_temperature", unit="°C", label="Outside")
    model_no = t.get("modelNumber") or ""
    attrs = {"kind": "thermostat", "identifier": ident, "equipment": t.get("equipmentStatus", ""),
             "schedule": sched, "hold": bool(ev and ev.get("type") != "vacation")}
    return {"id": ident, "name": t.get("name") or f"Thermostat {ident[-4:]}", "model": MODEL_NAMES.get(model_no, model_no or "ecobee"),
            "online": bool(rt.get("connected", True)), "values": {k: v for k, v in vals.items() if v is not None},
            "caps": caps, "attributes": attrs, "sensors": sensors}


def parse_summary(d: dict) -> dict[str, tuple]:
    """thermostatSummary -> {identifier: (connected, thermostatRevision, runtimeRevision, equipment)}."""
    equip = {}
    for row in d.get("statusList") or []:
        ident, _, eq = str(row).partition(":")
        equip[ident] = eq
    out = {}
    for row in d.get("revisionList") or []:
        parts = str(row).split(":")
        if len(parts) < 7:
            continue
        ident = parts[0]
        # identifier:name:connected:thermostatRev:alertsRev:runtimeRev:intervalRev (the name may contain ":")
        tail = parts[-5:]
        out[ident] = (tail[0], tail[1], tail[3], equip.get(ident, ""))
    return out


# ---------------------------------------------------------------- adapter
class EcobeeAdapter(Adapter):
    source = "ecobee"

    def __init__(self, hub, cfg, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(hub, cfg)
        try:
            self.poll = max(3.0, float(cfg.get("poll_minutes") or 3)) * 60
        except (TypeError, ValueError):
            self.poll = 180
        self.hold_type = "indefinite" if cfg.get("hold_type") == "indefinite" else "nextTransition"
        self.http = httpx.AsyncClient(timeout=30, transport=transport or _transport, headers={"User-Agent": USER_AGENT})
        self.tokens: dict = {}
        self._lock = asyncio.Lock()
        self.raw: dict[str, dict] = {}            # identifier -> last full thermostat object
        self._revs: dict[str, tuple] = {}
        self._sig: dict[str, tuple] = {}
        self._last_full = 0.0
        self.full_fetches = 0
        self._refresh_task: asyncio.Task | None = None

    # -- sign-in (Settings > Ecobee > Sign in) --
    @classmethod
    async def signin(cls, values: dict, data_dir) -> dict:
        api_key = str(values.get("api_key") or "").strip()
        if api_key:
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", api_key):
                raise ValueError("That doesn't look like an ecobee API key (letters and numbers from ecobee.com > Developer)")
            if not values.get("confirm"):
                p = await request_pin(api_key)
                _pin_pending[api_key] = p
                mins = max(1, round((p["expires"] - time.time()) / 60))
                return {"need_confirm": True, "pin": p["pin"],
                        "message": f"On ecobee.com, open the menu > My Apps > Add application, enter this PIN and "
                                   f"choose Authorize within {mins} minutes. Then come back and press Finish sign-in."}
            p = _pin_pending.get(api_key)
            if not p or time.time() > p["expires"]:
                _pin_pending.pop(api_key, None)
                raise ValueError("The PIN expired. Close this window and choose Sign in again for a new PIN.")
            try:
                rec = await exchange_pin(api_key, p["code"])
            except AuthPending:
                return {"need_confirm": True, "pin": p["pin"],
                        "message": "ecobee hasn't seen this PIN yet. Enter it at ecobee.com > My Apps > Add application, "
                                   "choose Authorize, then press Finish sign-in again."}
            _pin_pending.pop(api_key, None)
            save_tokens({**rec, "signed_in": time.time()}, data_dir)
            return {"ok": True}
        email = str(values.get("email") or "").strip()
        password = str(values.get("password") or "")
        if not email:
            raise ValueError("Enter your ecobee email and password (or a developer API key)")
        code = str(values.get("code") or "").strip()
        if code:
            res = await account_code(email, code)
        else:
            if not password:
                raise ValueError("Enter your ecobee password")
            res = await account_login(email, password)
        if res.get("need_code"):
            return res
        save_tokens({**res, "signed_in": time.time()}, data_dir)      # the password itself is never stored
        return {"ok": True}

    # -- helpers --
    def _secrets(self) -> list[str]:
        return [str(self.tokens.get(k)) for k in ("api_key", "access_token", "refresh_token") if self.tokens.get(k)]

    def redact(self, e: Any) -> str:
        from ..security import redact
        return redact(str(e) or type(e).__name__, self._secrets())

    async def start(self):
        self.spawn("poll", self._loop, max_delay=900)

    async def stop(self):
        await super().stop()
        if self._refresh_task:
            self._refresh_task.cancel()
        await self.http.aclose()

    async def action(self, name: str) -> str:
        if name != "refresh":
            raise ValueError(name)
        if not usable(self.tokens):
            raise RuntimeError("sign in first")
        try:
            await self.poll_once(force=True)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(self.redact(e)) from None
        return "Updated from ecobee"

    async def _loop(self):
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never let a token reach the status line
            raise RuntimeError(self.redact(e)) from None

    async def _run(self):
        while True:
            self.tokens = load_tokens()
            if not usable(self.tokens):
                self.status = ("needs sign-in (ecobee ended the session; sign in again in Settings > Ecobee)"
                               if self.tokens.get("revoked") else "needs sign-in (Settings > Ecobee)")
                await asyncio.sleep(15)
                continue
            try:
                await self.poll_once()
            except AuthRevoked as e:
                self._signed_out(str(e))
                continue
            await asyncio.sleep(self.poll)

    def _signed_out(self, why: str) -> None:
        self.log.warning("ecobee: %s; sign in again in Settings > Ecobee", self.redact(why))
        keep = {k: v for k, v in self.tokens.items() if k in ("method", "api_key", "account")}
        self.tokens = {**keep, "revoked": time.time()}
        save_tokens(self.tokens)
        self.status = "needs sign-in (ecobee ended the session; sign in again in Settings > Ecobee)"
        for did, dev in list(self.hub.devices.items()):
            if dev.source == self.source:
                self.hub.set_values(did, {}, online=False)

    async def _refresh(self) -> None:
        tk = self.tokens
        if tk.get("method") == "pin":
            rec = await refresh_pin(tk["api_key"], tk["refresh_token"], self.http)
        else:
            rec = await refresh_account(tk["refresh_token"], self.http, tk.get("account", ""))
        self.tokens = {**tk, **rec}
        save_tokens(self.tokens)

    async def _access(self, force: bool = False, stale: str | None = None) -> str:
        async with self._lock:
            if force and self.tokens.get("access_token") != stale:
                return self.tokens["access_token"]        # someone else already refreshed it
            if force or time.time() > float(self.tokens.get("expires_at") or 0) - REFRESH_MARGIN:
                await self._refresh()
            return self.tokens["access_token"]

    async def api(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                  _retry: bool = True) -> dict:
        token = await self._access()
        r = await self.http.request(method, f"{API_BASE}/1/{path}", params=params, json=body,
                                    headers={"Authorization": f"Bearer {token}",
                                             "Content-Type": "application/json;charset=UTF-8"})
        d = _json(r)
        st = d.get("status") if isinstance(d.get("status"), dict) else {}
        code = st.get("code")
        if r.status_code == 200 and code in (0, None):
            return d
        if code == 14 and _retry:                       # access token expired early: refresh once and retry
            await self._access(force=True, stale=token)
            return await self.api(method, path, params=params, body=body, _retry=False)
        if code in (1, 16) or (code == 14 and not _retry) or (r.status_code == 401 and code is None):
            raise AuthRevoked(f"ecobee refused the sign-in (code {code if code is not None else r.status_code})")
        msg = str(st.get("message") or "").strip()[:200]
        raise EcobeeError(f"ecobee error {code}: {msg}" if code is not None else f"ecobee answered HTTP {r.status_code}")

    async def poll_once(self, force: bool = False, only: list[str] | None = None) -> None:
        sel = {"selection": {"selectionType": "registered", "selectionMatch": "", "includeEquipmentStatus": True}}
        summary = parse_summary(await self.api("GET", "thermostatSummary", params={"json": json.dumps(sel)}))
        stale = time.time() - self._last_full > FULL_REFRESH_EVERY
        changed = [i for i, rev in summary.items() if force or stale or self._revs.get(i) != rev or i in (only or [])]
        if changed:
            await self._fetch(changed)
            for i in changed:
                if i in summary:
                    self._revs[i] = summary[i]
            if set(changed) >= set(summary):
                self._last_full = time.time()
        n_t = len(self.raw)
        n_s = sum(len(parse_thermostat(t)["sensors"]) for t in self.raw.values())
        self.status = f"ok · {n_t} thermostat{'s' if n_t != 1 else ''}" + (f", {n_s} sensor{'s' if n_s != 1 else ''}" if n_s else "")

    async def _fetch(self, idents: list[str]) -> None:
        sel = {"selection": {"selectionType": "thermostats", "selectionMatch": ",".join(idents),
                             "includeRuntime": True, "includeSettings": True, "includeEquipmentStatus": True,
                             "includeSensors": True, "includeProgram": True, "includeEvents": True, "includeWeather": True}}
        d = await self.api("GET", "thermostat", params={"json": json.dumps(sel)})
        self.full_fetches += 1
        for t in d.get("thermostatList") or []:
            if t.get("identifier"):
                self.raw[str(t["identifier"])] = t
                self._apply(t)

    def _apply(self, t: dict) -> None:
        p = parse_thermostat(t)
        did = self.dev_id(p["id"])
        sig = tuple(sorted((k, tuple(c.options or []), c.min, c.max) for k, c in p["caps"].items()))
        if self._sig.get(did) != sig or did not in self.hub.devices:
            self.hub.upsert_device(Device(id=did, name=p["name"], source=self.source, model=p["model"], capabilities=p["caps"]))
            self._sig[did] = sig
        self.hub.set_attributes(did, p["attributes"])
        self.hub.set_values(did, p["values"], online=p["online"])
        for s in p["sensors"]:
            sid = self.dev_id(s["id"])
            caps = {}
            if "temperature" in s["types"]:
                caps["temperature"] = cap("temperature")
            if "humidity" in s["types"]:
                caps["humidity"] = cap("humidity")
            if "occupancy" in s["types"]:
                caps["occupancy"] = cap("occupancy", label="Occupancy")
            ssig = tuple(sorted(caps))
            if self._sig.get(sid) != ssig or sid not in self.hub.devices:
                self.hub.upsert_device(Device(id=sid, name=s["name"], source=self.source, model="ecobee room sensor",
                                              capabilities=caps))
                self._sig[sid] = ssig
            self.hub.set_attributes(sid, {"kind": "room_sensor", "thermostat": p["name"], "identifier": p["id"],
                                          "in_use": s["in_use"]})
            self.hub.set_values(sid, s["values"], online=p["online"] and bool(s["values"]))

    # -- commands --
    def _body(self, ident: str, **extra) -> dict:
        return {"selection": {"selectionType": "thermostats", "selectionMatch": ident}, **extra}

    async def command(self, device: Device, capability: str, value: Any) -> None:
        ident = str(device.attributes.get("identifier") or "")
        t = self.raw.get(ident)
        if t is None or device.attributes.get("kind") != "thermostat":
            raise ValueError(f"{capability} can't be changed on {device.name}")
        rt, st = t.setdefault("runtime", {}), t.get("settings") or {}
        heat, cool = int(rt.get("desiredHeat") or 680), int(rt.get("desiredCool") or 750)
        optimistic: dict[str, Any] = {}
        if capability == "hvac_mode":
            if value not in HVAC_MODES or value not in (device.capabilities["hvac_mode"].options or []):
                raise ValueError(f"unknown mode {value}")
            body = self._body(ident, thermostat={"settings": {"hvacMode": HVAC_MODES[value]}})
            t.setdefault("settings", {})["hvacMode"] = HVAC_MODES[value]
            optimistic["hvac_mode"] = value
        elif capability in ("heat_setpoint", "cool_setpoint"):
            try:
                c = float(value)
            except (TypeError, ValueError):
                raise ValueError("setpoint must be a number") from None
            hc, cc = device.capabilities["heat_setpoint"], device.capabilities["cool_setpoint"]
            delta = int(st.get("heatCoolMinDelta") or 50)
            if capability == "heat_setpoint":
                heat = to_t10(min(hc.max, max(hc.min, c)))
                cool = max(cool, heat + delta)
            else:
                cool = to_t10(min(cc.max, max(cc.min, c)))
                heat = min(heat, cool - delta)
            body = self._body(ident, functions=[{"type": "setHold", "params": {
                "holdType": self.hold_type, "heatHoldTemp": heat, "coolHoldTemp": cool}}])
            rt["desiredHeat"], rt["desiredCool"] = heat, cool
            optimistic.update(heat_setpoint=t10(heat), cool_setpoint=t10(cool))
        elif capability == "fan_mode":
            if value not in ("auto", "on"):
                raise ValueError(f"unknown fan mode {value}")
            body = self._body(ident, functions=[{"type": "setHold", "params": {
                "holdType": self.hold_type, "fan": value, "heatHoldTemp": heat, "coolHoldTemp": cool}}])
            rt["desiredFanMode"] = value
            optimistic["fan_mode"] = value
        elif capability == "action" and value == "resume_schedule":
            body = self._body(ident, functions=[{"type": "resumeProgram", "params": {"resumeAll": False}}])
        else:
            raise ValueError(capability)
        try:
            await self.api("POST", "thermostat", params={"format": "json"}, body=body)
        except AuthRevoked as e:
            self._signed_out(str(e))
            raise RuntimeError("ecobee ended the session; sign in again in Settings > Ecobee") from None
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(self.redact(e)) from None
        if optimistic:
            self.hub.set_values(device.id, optimistic)
        self._schedule_refresh(ident)

    def _schedule_refresh(self, ident: str, delay: float = 3.0) -> None:
        async def later():
            await asyncio.sleep(delay)
            try:
                await self._fetch([ident])
            except Exception as e:  # noqa: BLE001
                self.log.info("ecobee refresh after command failed: %s", self.redact(e))
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(later())


ADAPTER = EcobeeAdapter
SETTINGS = {
    "id": "ecobee", "label": "Ecobee", "icon": "thermostat",
    "help": "Thermostats and their room sensors, through ecobee's cloud. Turn this on, Save, then choose Sign in "
            "and enter the email and password you use at ecobee.com (plus the code from your authenticator app or "
            "text message if you use two-step verification). Haven keeps only ecobee's access tokens, not your "
            "password. Already have an ecobee developer API key? Enter just the key instead: Haven shows a PIN to "
            "enter at ecobee.com > My Apps > Add application. ecobee stopped handing out new developer keys in "
            "2025, and you don't need one. Readings update every 3 minutes (ecobee's limit) and right after a change.",
    "login": True,
    "login_fields": [
        {"key": "email", "label": "ecobee email", "type": "text", "optional": True},
        {"key": "password", "label": "ecobee password", "type": "password", "optional": True},
        {"key": "api_key", "label": "Developer API key (optional)", "type": "text", "optional": True},
    ],
    "login_help": "Use your ecobee.com email and password, or only a developer API key if you have one. "
                  "Your password is only used for this sign-in.",
    "actions": [{"id": "refresh", "label": "Refresh now", "icon": "sync"}],
    "fields": [
        {"key": "hold_type", "label": "Keep temperature changes", "type": "select", "default": "nextTransition",
         "options": HOLD_TYPES},
        {"key": "poll_minutes", "label": "Check for changes every (minutes, 3 or more)", "type": "number", "default": 3,
         "advanced": True},
    ],
}
