"""ecobee: PIN and account sign-in, token refresh/revocation, summary-revision polling, commands, units."""
import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters import ecobee as eco
from app.hub import Hub

API_KEY = "AbCdEf0123456789AbCdEf0123456789"
ACCESS = "eyJhbGciOiJSUzI1NiJ9.access-secret-0001"
REFRESH = "refresh-secret-RTk2N0YzRjE"

# Shaped like real GET /1/thermostat responses (values are ecobee's tenths of °F)
THERMOSTAT = {
    "identifier": "511863547393", "name": "Main Floor", "thermostatRev": "260811235540", "isRegistered": True,
    "modelNumber": "aresSmart", "brand": "ecobee", "features": "Home,HomeKit", "lastModified": "2026-11-16 18:55:40",
    "equipmentStatus": "heatPump,fan",
    "runtime": {"runtimeRev": "261116190500", "connected": True, "firstConnected": "2024-01-09 22:13:41",
                "actualTemperature": 689, "actualHumidity": 41, "rawTemperature": 689, "showIconMode": 1,
                "desiredHeat": 700, "desiredCool": 760, "desiredHumidity": 36, "desiredDehumidity": 60,
                "desiredFanMode": "auto", "desiredHeatRange": [450, 790], "desiredCoolRange": [650, 920]},
    "settings": {"hvacMode": "auto", "heatRangeHigh": 790, "heatRangeLow": 450, "coolRangeHigh": 920, "coolRangeLow": 650,
                 "heatCoolMinDelta": 50, "hasHeatPump": True, "hasForcedAir": True, "heatStages": 1, "coolStages": 1,
                 "autoHeatCoolFeatureEnabled": True, "useCelsius": False, "fanMinOnTime": 10},
    "program": {"currentClimateRef": "home", "climates": [
        {"name": "Home", "climateRef": "home", "heatTemp": 700, "coolTemp": 760},
        {"name": "Away", "climateRef": "away", "heatTemp": 620, "coolTemp": 830},
        {"name": "Sleep", "climateRef": "sleep", "heatTemp": 660, "coolTemp": 780}]},
    "events": [],
    "weather": {"timestamp": "2026-11-16 18:46:07", "weatherStation": "ENV:KMSP",
                "forecasts": [{"weatherSymbol": 2, "dateTime": "2026-11-16 18:46:07", "condition": "Mostly cloudy",
                               "temperature": 352, "pressure": 1012, "relativeHumidity": 70}]},
    "remoteSensors": [
        {"id": "ei:0", "name": "Main Floor", "type": "thermostat", "inUse": True, "capability": [
            {"id": "1", "type": "temperature", "value": "689"}, {"id": "2", "type": "humidity", "value": "41"},
            {"id": "3", "type": "occupancy", "value": "true"}]},
        {"id": "rs:100", "name": "Bedroom", "type": "ecobee3_remote_sensor", "code": "WHXY", "inUse": True, "capability": [
            {"id": "1", "type": "temperature", "value": "662"}, {"id": "2", "type": "occupancy", "value": "false"}]},
        {"id": "rs2:101", "name": "Office", "type": "ecobee3_remote_sensor", "code": "QWER", "inUse": False, "capability": [
            {"id": "1", "type": "temperature", "value": "unknown"}, {"id": "2", "type": "occupancy", "value": "unknown"}]},
    ],
}


def summary(rev="261116190500", equip="heatPump,fan", connected="true"):
    return {"thermostatCount": 1,
            "revisionList": [f"511863547393:Main Floor:{connected}:260811235540:260811235540:{rev}:261116185500"],
            "statusList": [f"511863547393:{equip}"], "status": {"code": 0, "message": ""}}


class FakeEcobee:
    """A small stand-in for api.ecobee.com + auth.ecobee.com."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.posts: list[dict] = []
        self.summary = summary()
        self.thermostat = json.loads(json.dumps(THERMOSTAT))
        self.access = ACCESS
        self.refresh = REFRESH
        self.expire_next = False          # answer the next data call with code 14
        self.revoked = False
        self.pin_authorized = False
        self.mfa = False
        self.form_seen: dict = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url, path = str(req.url), req.url.path
        self.calls.append((req.method, path))
        form = parse_qs(req.content.decode()) if req.method == "POST" and b"=" in req.content and not req.content.startswith(b"{") else {}
        f = {k: v[0] for k, v in form.items()}
        host = req.url.host
        if host == "api.ecobee.com" and path == "/authorize":
            if req.url.params["client_id"] != API_KEY:
                return httpx.Response(401, json={"error": "invalid_client", "error_description": "Invalid client"})
            return httpx.Response(200, json={"ecobeePin": "bv29", "code": "uiv7DDWhOCTHOmAmUxcOnkDeprUKJbmY",
                                             "scope": "smartWrite", "expires_in": 9, "interval": 30})
        if host == "api.ecobee.com" and path == "/token":
            self.form_seen = f
            if f["grant_type"] == "ecobeePin":
                if not self.pin_authorized:
                    return httpx.Response(401, json={"error": "authorization_pending",
                                                     "error_description": "Waiting for user to authorize application."})
                return self._tokens()
            if f["grant_type"] == "refresh_token":
                if self.revoked or f["refresh_token"] != self.refresh:
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "The authorization grant, token or credentials are invalid."})
                return self._tokens()
        if host == "auth.ecobee.com":
            return self._auth(req, path, f)
        if host == "api.ecobee.com" and path.startswith("/1/"):
            if req.headers.get("authorization") != f"Bearer {self.access}":
                return httpx.Response(500, json={"status": {"code": 16, "message": "Authorization has been deauthorized."}})
            if self.expire_next:
                self.expire_next = False
                return httpx.Response(500, json={"status": {"code": 14, "message": "Authentication token has expired. Refresh your tokens. Please refer to documentation."}})
            if path == "/1/thermostatSummary":
                return httpx.Response(200, json=self.summary)
            if path == "/1/thermostat" and req.method == "GET":
                sel = json.loads(req.url.params["json"])["selection"]
                assert sel["includeRuntime"] and sel["includeSensors"] and sel["includeEquipmentStatus"]
                return httpx.Response(200, json={"page": {"page": 1, "totalPages": 1, "pageSize": 1, "total": 1},
                                                 "thermostatList": [self.thermostat], "status": {"code": 0, "message": ""}})
            if path == "/1/thermostat" and req.method == "POST":
                assert req.url.params["format"] == "json"
                self.posts.append(json.loads(req.content))
                return httpx.Response(200, json={"status": {"code": 0, "message": ""}})
        return httpx.Response(404, json={"error": "not found", "url": url})

    def _tokens(self):
        self.access = self.access + "x"
        self.refresh = self.refresh + "y"
        return httpx.Response(200, json={"access_token": self.access, "token_type": "Bearer", "expires_in": 3599,
                                         "refresh_token": self.refresh, "scope": "smartWrite"})

    def _auth(self, req, path, f):
        base = "https://auth.ecobee.com"
        if path == "/authorize":
            assert req.url.params["code_challenge_method"] == "S256" and req.url.params["client_id"] == eco.WEB_CLIENT_ID
            return httpx.Response(302, headers={"location": "/u/login/identifier?state=S1"})
        if path == "/u/login/identifier" and req.method == "GET":
            return httpx.Response(200, text="<form>")
        if path == "/u/login/identifier":
            return httpx.Response(302, headers={"location": "/u/login/password?state=S2"})
        if path == "/u/login/password" and req.method == "GET":
            return httpx.Response(200, text="<form>")
        if path == "/u/login/password":
            if f.get("password") != "hunter2-correct":
                return httpx.Response(400, text="wrong")
            return httpx.Response(302, headers={"location": "/authorize/resume?state=S3"})
        if path == "/authorize/resume":
            if self.mfa:
                return httpx.Response(302, headers={"location": "/u/mfa-otp-challenge?state=S4"})
            return httpx.Response(302, headers={"location": f"{eco.WEB_REDIRECT}?code=WEBCODE1&state=S3"})
        if path == "/u/mfa-otp-challenge" and req.method == "POST":
            if f.get("code") != "123456":
                return httpx.Response(302, headers={"location": "/u/mfa-otp-challenge?state=S4"})
            return httpx.Response(302, headers={"location": f"{base}/authorize/resume2?state=S5"})
        if path == "/u/mfa-otp-challenge":
            return httpx.Response(200, text="<form>")
        if path == "/authorize/resume2":
            return httpx.Response(302, headers={"location": f"{eco.WEB_REDIRECT}?code=WEBCODE2"})
        if path == "/oauth/token":
            self.form_seen = f
            if f["grant_type"] == "authorization_code":
                assert f["code"] in ("WEBCODE1", "WEBCODE2") and len(f["code_verifier"]) >= 43
                return self._tokens()
            if self.revoked or f.get("refresh_token") != self.refresh:
                return httpx.Response(403, json={"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})
            return self._tokens()
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    fe = FakeEcobee()
    monkeypatch.setattr(eco, "_transport", httpx.MockTransport(fe))
    monkeypatch.setattr(eco, "DATA_DIR", tmp_path)
    monkeypatch.setattr(eco, "API_BASE", "https://api.ecobee.com")
    monkeypatch.setattr(eco, "AUTH_BASE", "https://auth.ecobee.com")
    eco._pin_pending.clear()
    eco._mfa_pending.clear()
    return fe


def signed_in(tmp_path, fe, **kw):
    rec = {"method": "pin", "api_key": API_KEY, "access_token": fe.access, "refresh_token": fe.refresh,
           "expires_at": time.time() + 3000, **kw}
    eco.save_tokens(rec, tmp_path)
    return rec


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- units / mapping
def test_units_and_mapping():
    assert eco.t10(689) == pytest.approx(20.5, abs=0.01)
    assert eco.t10("720") == pytest.approx(22.22, abs=0.01)
    assert eco.t10("unknown") is None and eco.t10(-5002) is None
    assert eco.to_t10(21.0) == 698 and eco.to_t10(eco.t10(700)) == 700
    assert eco.hvac_action("heatPump,fan") == "heating" and eco.hvac_action("compCool1,fan") == "cooling"
    assert eco.hvac_action("fan") == "fan" and eco.hvac_action("") == "idle"
    p = eco.parse_thermostat(THERMOSTAT)
    v = p["values"]
    assert v["temperature"] == pytest.approx(20.5, abs=0.01) and v["humidity"] == 41
    assert v["heat_setpoint"] == pytest.approx(21.11, abs=0.01) and v["cool_setpoint"] == pytest.approx(24.44, abs=0.01)
    assert v["hvac_mode"] == "auto" and v["fan_mode"] == "auto" and v["hvac_action"] == "heating"
    assert v["status_text"] == "Heating · Home" and v["occupancy"] is True
    assert v["outdoor_temperature"] == pytest.approx(1.78, abs=0.01)
    caps = p["caps"]
    assert caps["hvac_mode"].options == ["heat", "cool", "auto", "off", "aux_heat_only"] and caps["hvac_mode"].writable
    assert caps["heat_setpoint"].min == pytest.approx(7.22, abs=0.01) and caps["heat_setpoint"].max == pytest.approx(26.11, abs=0.01)
    assert caps["cool_setpoint"].unit == "°C" and caps["fan_mode"].options == ["auto", "on"]
    assert caps["action"].options == ["resume_schedule"]
    assert p["model"] == "ecobee Smart Thermostat Premium" and p["attributes"]["kind"] == "thermostat"
    # the thermostat's own sensor isn't a separate device; remote sensors are
    assert [s["name"] for s in p["sensors"]] == ["Bedroom", "Office"]
    assert p["sensors"][0]["values"] == {"temperature": pytest.approx(19.0, abs=0.01), "occupancy": False}
    assert p["sensors"][1]["values"] == {}                     # "unknown" readings are skipped


def test_modes_hold_and_off():
    t = json.loads(json.dumps(THERMOSTAT))
    t["settings"].update(hasHeatPump=False, coolStages=0, hvacMode="heat")
    t["events"] = [{"type": "hold", "running": True, "holdClimateRef": "away", "fan": "on"}]
    t["runtime"].pop("desiredFanMode")
    p = eco.parse_thermostat(t)
    assert p["caps"]["hvac_mode"].options == ["heat", "off"]
    assert p["values"]["status_text"] == "Heating · Hold · Away" and p["values"]["fan_mode"] == "on"
    t["settings"]["hvacMode"] = "off"
    t["equipmentStatus"] = ""
    assert eco.parse_thermostat(t)["values"]["status_text"] == "Off"
    t["settings"]["hvacMode"] = "auxHeatOnly"
    assert eco.parse_thermostat(t)["values"]["hvac_mode"] == "aux_heat_only"


def test_summary_parse_name_with_colon():
    s = eco.parse_summary({"revisionList": ["1234:Up:stairs:true:r1:a1:rt1:i1"], "statusList": ["1234:compCool1"]})
    assert s == {"1234": ("true", "r1", "rt1", "compCool1")}


# ---------------------------------------------------------------- PIN flow
def test_pin_flow(fake, tmp_path):
    res = run(eco.EcobeeAdapter.signin({"api_key": API_KEY}, tmp_path))
    assert res["need_confirm"] and res["pin"] == "bv29" and "My Apps" in res["message"] and "9 minutes" in res["message"]
    again = run(eco.EcobeeAdapter.signin({"api_key": API_KEY, "confirm": "1"}, tmp_path))
    assert again["need_confirm"] and again["pin"] == "bv29" and "hasn't seen this PIN" in again["message"]
    assert not eco.load_tokens(tmp_path)
    fake.pin_authorized = True
    assert run(eco.EcobeeAdapter.signin({"api_key": API_KEY, "confirm": "1"}, tmp_path)) == {"ok": True}
    assert fake.form_seen == {"grant_type": "ecobeePin", "code": "uiv7DDWhOCTHOmAmUxcOnkDeprUKJbmY", "client_id": API_KEY}
    tok = eco.load_tokens(tmp_path)
    assert tok["method"] == "pin" and tok["api_key"] == API_KEY and tok["access_token"] == fake.access
    assert tok["expires_at"] > time.time() + 3000
    assert (tmp_path / eco.TOKEN_FILE).stat().st_mode & 0o077 == 0


def test_pin_bad_key_and_expired(fake, tmp_path):
    with pytest.raises(eco.EcobeeError, match="didn't accept that API key"):
        run(eco.EcobeeAdapter.signin({"api_key": "WrongKey12345678"}, tmp_path))
    with pytest.raises(ValueError, match="doesn't look like"):
        run(eco.EcobeeAdapter.signin({"api_key": "bad key!"}, tmp_path))
    run(eco.EcobeeAdapter.signin({"api_key": API_KEY}, tmp_path))
    eco._pin_pending[API_KEY]["expires"] = time.time() - 1
    with pytest.raises(ValueError, match="PIN expired"):
        run(eco.EcobeeAdapter.signin({"api_key": API_KEY, "confirm": "1"}, tmp_path))


# ---------------------------------------------------------------- account flow
def test_account_login(fake, tmp_path):
    with pytest.raises(eco.EcobeeError, match="email and password"):
        run(eco.EcobeeAdapter.signin({"email": "me@example.com", "password": "nope"}, tmp_path))
    assert run(eco.EcobeeAdapter.signin({"email": "me@example.com", "password": "hunter2-correct"}, tmp_path)) == {"ok": True}
    tok = eco.load_tokens(tmp_path)
    assert tok["method"] == "account" and tok["account"] == "me@example.com" and tok["refresh_token"] == fake.refresh
    assert "hunter2" not in (tmp_path / eco.TOKEN_FILE).read_text()


def test_account_login_with_two_step_code(fake, tmp_path):
    fake.mfa = True
    creds = {"email": "me@example.com", "password": "hunter2-correct"}
    res = run(eco.EcobeeAdapter.signin(creds, tmp_path))
    assert res["need_code"] and "authenticator" in res["message"]

    async def rest():
        with pytest.raises(eco.EcobeeError, match="didn't accept that code"):
            await eco.EcobeeAdapter.signin({**creds, "code": "000000"}, tmp_path)
        return await eco.EcobeeAdapter.signin({**creds, "code": "123456"}, tmp_path)
    # the pending login keeps its own client (cookies); it survives across calls in one event loop in the app
    assert run(rest()) == {"ok": True}
    assert eco.load_tokens(tmp_path)["method"] == "account" and not eco._mfa_pending


def test_signin_needs_something(fake, tmp_path):
    with pytest.raises(ValueError, match="email and password"):
        run(eco.EcobeeAdapter.signin({}, tmp_path))


# ---------------------------------------------------------------- polling
def make(fake, tmp_path, **cfg):
    hub = Hub()
    a = eco.EcobeeAdapter(hub, cfg)
    a.tokens = eco.load_tokens(tmp_path)
    hub.adapters["ecobee"] = a
    return hub, a


def test_poll_uses_summary_revisions(fake, tmp_path):
    signed_in(tmp_path, fake)

    async def go():
        hub, a = make(fake, tmp_path)
        await a.poll_once()
        assert a.full_fetches == 1
        await a.poll_once()                       # nothing changed: summary only
        await a.poll_once()
        assert a.full_fetches == 1
        fake.summary = summary(rev="261116191000")          # runtime revision moved
        fake.thermostat["runtime"]["actualTemperature"] = 701
        await a.poll_once()
        assert a.full_fetches == 2
        fake.summary = summary(rev="261116191000", equip="")  # equipment stopped
        fake.thermostat["equipmentStatus"] = ""
        await a.poll_once()
        assert a.full_fetches == 3
        await a.stop()
        return hub, a
    hub, a = run(go())
    d = hub.devices["ecobee:511863547393"]
    assert d.name == "Main Floor" and d.attributes["kind"] == "thermostat" and d.online
    assert d.capabilities["temperature"].value == pytest.approx(21.17, abs=0.01)
    assert d.capabilities["hvac_action"].value == "idle" and d.capabilities["status_text"].value == "Idle · Home"
    s = hub.devices["ecobee:511863547393_rs-100"]
    assert s.name == "Bedroom" and s.capabilities["occupancy"].value is False and s.attributes["kind"] == "room_sensor"
    assert not hub.devices["ecobee:511863547393_rs2-101"].online         # no readings yet
    assert a.status == "ok · 1 thermostat, 2 sensors"
    paths = [p for _, p in fake.calls]
    assert paths.count("/1/thermostatSummary") == 5 and paths.count("/1/thermostat") == 3


def test_refresh_before_expiry_and_on_code_14(fake, tmp_path):
    signed_in(tmp_path, fake, expires_at=time.time() + 60)          # inside the 5-minute margin

    async def go():
        hub, a = make(fake, tmp_path)
        old_refresh = fake.refresh
        await a.poll_once()
        assert fake.form_seen["grant_type"] == "refresh_token" and fake.form_seen["refresh_token"] == old_refresh
        assert eco.load_tokens(tmp_path)["refresh_token"] == fake.refresh       # rotated refresh token saved
        n = len([c for c in fake.calls if c[1] == "/token"])
        fake.expire_next = True
        await a.poll_once(force=True)
        assert len([c for c in fake.calls if c[1] == "/token"]) == n + 1
        await a.stop()
    run(go())


def test_revoked_tokens_need_sign_in_and_stay_secret(fake, tmp_path):
    signed_in(tmp_path, fake, expires_at=0)
    fake.revoked = True

    async def go():
        hub, a = make(fake, tmp_path)
        await a.start()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if a.status.startswith("needs"):
                break
        await a.stop()
        return a
    a = run(go())
    assert a.status.startswith("needs sign-in (ecobee ended the session")
    tok = eco.load_tokens(tmp_path)
    assert "refresh_token" not in tok and "access_token" not in tok and tok["revoked"] and tok["api_key"] == API_KEY
    assert REFRESH not in a.status and API_KEY not in a.status


def test_code_16_on_data_call_signs_out(fake, tmp_path):
    signed_in(tmp_path, fake)
    fake.access = "rotated-elsewhere"            # our token is no longer valid -> code 16

    async def go():
        hub, a = make(fake, tmp_path)
        with pytest.raises(eco.AuthRevoked):
            await a.poll_once()
        await a.stop()
    run(go())


def test_errors_are_redacted(fake, tmp_path):
    rec = signed_in(tmp_path, fake)

    async def go():
        hub, a = make(fake, tmp_path)
        msg = a.redact(RuntimeError(f"boom {rec['access_token']} {rec['refresh_token']} {API_KEY} "
                                    f"https://api.ecobee.com/token?refresh_token={rec['refresh_token']}"))
        await a.stop()
        return msg
    msg = run(go())
    for s in (rec["access_token"], rec["refresh_token"], API_KEY):
        assert s not in msg


def test_status_never_shows_tokens_on_unexpected_error(fake, tmp_path, monkeypatch):
    rec = signed_in(tmp_path, fake)

    async def go():
        hub, a = make(fake, tmp_path)

        async def explode(*_a, **_k):
            raise RuntimeError(f"weird failure with {rec['access_token']} and {API_KEY}")
        monkeypatch.setattr(a, "poll_once", explode)
        await a.start()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if a.status.startswith("error"):
                break
        await a.stop()
        return a.status
    st = run(go())
    assert st.startswith("error") and rec["access_token"] not in st and API_KEY not in st


# ---------------------------------------------------------------- commands
def test_commands(fake, tmp_path):
    signed_in(tmp_path, fake)

    async def go():
        hub, a = make(fake, tmp_path, hold_type="indefinite")
        await a.poll_once()
        did = "ecobee:511863547393"
        await hub.command(did, "hvac_mode", "heat")
        await hub.command(did, "heat_setpoint", 22.0)          # 71.6 °F -> 716; cool pushed up to 766 (min. 5 °F apart)
        await hub.command(did, "cool_setpoint", 23.0)          # 73.4 °F -> 734; heat pulled down to 684 (5 °F apart)
        await hub.command(did, "cool_setpoint", 22.5)          # 72.5 °F -> 725; heat pulled down to 675
        await hub.command(did, "fan_mode", "on")
        await hub.command(did, "action", "resume_schedule")
        with pytest.raises(ValueError):
            await hub.command(did, "hvac_mode", "turbo")
        with pytest.raises(ValueError):
            await hub.command("ecobee:511863547393_rs-100", "temperature", 20)
        d = hub.devices[did]
        assert d.capabilities["hvac_mode"].value == "heat" and d.capabilities["fan_mode"].value == "on"
        await asyncio.sleep(3.2)                                 # refresh right after commands
        await a.stop()
        return a
    a = run(go())
    sel = {"selectionType": "thermostats", "selectionMatch": "511863547393"}
    p = fake.posts
    assert p[0] == {"selection": sel, "thermostat": {"settings": {"hvacMode": "heat"}}}
    assert p[1]["functions"] == [{"type": "setHold", "params": {"holdType": "indefinite", "heatHoldTemp": 716, "coolHoldTemp": 766}}]
    assert p[2]["functions"][0]["params"] == {"holdType": "indefinite", "heatHoldTemp": 684, "coolHoldTemp": 734}
    assert p[3]["functions"][0]["params"] == {"holdType": "indefinite", "heatHoldTemp": 675, "coolHoldTemp": 725}
    assert p[4]["functions"][0]["params"] == {"holdType": "indefinite", "fan": "on", "heatHoldTemp": 675, "coolHoldTemp": 725}
    assert p[5] == {"selection": sel, "functions": [{"type": "resumeProgram", "params": {"resumeAll": False}}]}
    assert a.full_fetches == 2


def test_setpoint_clamped_and_default_hold(fake, tmp_path):
    signed_in(tmp_path, fake)

    async def go():
        hub, a = make(fake, tmp_path)
        await a.poll_once()
        await hub.command("ecobee:511863547393", "heat_setpoint", 40)       # above heatRangeHigh 79 °F
        await a.stop()
    run(go())
    params = fake.posts[0]["functions"][0]["params"]
    assert params["holdType"] == "nextTransition" and params["heatHoldTemp"] == 790 and params["coolHoldTemp"] == 840


def test_settings_section_registered():
    from app.adapters import ADAPTERS, PLUGIN_SETTINGS
    assert ADAPTERS["ecobee"] is eco.EcobeeAdapter
    sec = next(s for s in PLUGIN_SETTINGS if s["id"] == "ecobee")
    assert sec["login"] is True and {f["key"] for f in sec["login_fields"]} == {"email", "password", "api_key"}
    assert "API key" in sec["help"] and "PIN" in sec["help"] and "My Apps" in sec["help"]
    assert not any(f["key"] in ("api_key", "password", "access_token") for f in sec["fields"])


def test_generic_signin_endpoint_redacts(fake, admin_headers):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, headers=admin_headers) as c:
        r = c.post("/api/settings/ecobee/signin", json={"api_key": API_KEY})
        assert r.status_code == 200 and r.json()["need_confirm"] and r.json()["pin"] == "bv29"
        r = c.post("/api/settings/ecobee/signin", json={"api_key": API_KEY, "confirm": "1"})
        assert r.status_code == 200 and "hasn't seen this PIN" in r.json()["message"]
        r = c.post("/api/settings/ecobee/signin", json={"api_key": "WrongKey12345678"})
        assert r.status_code == 400 and "WrongKey12345678" not in r.json()["detail"]
