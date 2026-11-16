"""Water integrations: WaterGuru, Flo by Moen, Rachio (all cloud calls mocked)."""
import asyncio
import json

import httpx

from app.adapters import flo, rachio, waterguru
from app.hub import Hub
from app.models import Device


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- WaterGuru
DASH = {"waterBodies": [{
    "waterBodyId": 4711, "name": "Backyard Pool", "status": "YELLOW", "waterTemp": 82.4,
    "latestMeasureTime": "2026-11-16T15:02:11Z",
    "measurements": [
        {"type": "FREE_CL", "title": "Free Chlorine", "floatValue": 0.6, "status": "YELLOW",
         "cfg": {"unit": "ppm"}, "alerts": [{"advice": {"action": {"summary": "Add 1 lb of shock"}}}]},
        {"type": "PH", "title": "pH", "floatValue": 7.42, "status": "GREEN", "cfg": {}},
        {"type": "SKIMMER_FLOW", "title": "Skimmer Flow", "intValue": 1, "status": "GREEN", "cfg": {}},
    ],
    "pods": [{"pod": {"podId": 99, "product": "WaterGuru SENSE S2"}, "refillables": [
        {"type": "BATT", "pctLeft": 64}, {"type": "LAB", "pctLeft": 35, "timeLeftText": "3 weeks"}]}],
}]}


def test_waterguru_parse():
    values, attrs, specs = waterguru.parse_waterbody(DASH["waterBodies"][0])
    assert values["temperature"] == 28.0                       # 82.4 °F
    assert values["ph"] == 7.42 and values["free_chlorine"] == 0.6 and values["skimmer_flow"] == 1.0
    assert values["battery"] == 64 and values["cartridge_life"] == 35
    assert values["status_text"] == "Add 1 lb of shock"
    assert attrs["kind"] == "pool" and attrs["cartridge_days_left"] == 21 and attrs["serial"] == "99"
    assert specs["free_chlorine"]["unit"] == "ppm"
    ok = json.loads(json.dumps(DASH))["waterBodies"][0]
    ok["measurements"][0]["status"] = "GREEN"
    assert waterguru.parse_waterbody(ok)[0]["status_text"] == "In range"


def test_waterguru_ingest_creates_pool_device():
    hub = Hub()
    a = waterguru.WaterGuruAdapter(hub, {"email": "a@b.c", "password": "x"})
    a.ingest(DASH)
    d = hub.devices["waterguru:4711"]
    assert d.room == "Pool" and d.model == "WaterGuru SENSE S2" and d.attributes["kind"] == "pool"
    assert d.capabilities["ph"].value == 7.42 and d.capabilities["cartridge_life"].unit == "%"
    assert d.capabilities["status_text"].value == "Add 1 lb of shock"


def test_waterguru_jwt_username():
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"cognito:username": "abc-123"}).encode()).decode().rstrip("=")
    assert waterguru._jwt_claims(f"h.{payload}.s")["cognito:username"] == "abc-123"


# ---------------------------------------------------------------- Flo
FLO_DEVICE = {"id": "dev1", "deviceType": "flo_device_v2", "deviceModel": "flo_device_075_v2", "nickname": "Main shutoff",
              "isConnected": True, "valve": {"lastKnown": "open"},
              "telemetry": {"current": {"gpm": 1.234, "psi": 61.27, "tempF": 59}},
              "notifications": {"pending": {"criticalCount": 0, "warningCount": 1}}}
FLO_PUCK = {"id": "puck1", "deviceType": "puck_oem", "nickname": "Water heater", "isConnected": True,
            "fwProperties": {"telemetry_water": True},
            "telemetry": {"current": {"tempF": 68, "humidity": 44.6, "batteryPercent": 87}}}


def test_flo_parsers():
    v = flo.monitor_values(FLO_DEVICE, "away")
    assert v == {"leak": False, "valve": "open", "flow_rate": 1.23, "water_pressure": 61.3, "temperature": 15.0,
                 "mode": "away", "status_text": "Normal · away"}
    closed = {**FLO_DEVICE, "valve": {"lastKnown": "closed"}, "notifications": {"pending": {"criticalCount": 2}}}
    v = flo.monitor_values(closed, "home")
    assert v["leak"] is True and v["valve"] == "closed" and v["status_text"] == "Leak alert"
    p = flo.puck_values(FLO_PUCK)
    assert p == {"leak": True, "temperature": 20.0, "humidity": 45, "battery": 87}
    assert flo.is_puck(FLO_PUCK) and not flo.is_puck(FLO_DEVICE)
    assert flo.usage_today({"aggregations": {"sumTotalGallonsConsumed": 123.456}}) == 123.5


class FakeFlo:
    def __init__(self):
        self.calls = []
        test = self

        class U:
            async def get_info(self, include_location_info=False):
                return {"locations": [{"id": "loc1", "nickname": "Home", "devices": [{"id": "dev1"}, {"id": "puck1"}]}]}

        class L:
            async def get_info(self, loc):
                return {"systemMode": {"target": "home"}}

            async def set_mode_home(self, loc): test.calls.append(("home", loc))
            async def set_mode_away(self, loc): test.calls.append(("away", loc))
            async def set_mode_sleep(self, loc, minutes, revert): test.calls.append(("sleep", loc, minutes, revert))

        class D:
            async def get_info(self, i):
                return FLO_DEVICE if i == "dev1" else FLO_PUCK

            async def open_valve(self, i): test.calls.append(("open", i))
            async def close_valve(self, i): test.calls.append(("close", i))

        class W:
            async def get_consumption_info(self, loc, start, end):
                return {"aggregations": {"sumTotalGallonsConsumed": 42}}

        self.user, self.location, self.device, self.water = U(), L(), D(), W()


def test_flo_refresh_and_commands():
    hub = Hub()
    a = flo.FloAdapter(hub, {"email": "a@b.c", "password": "x"})
    a.api = FakeFlo()
    run(a.refresh())
    mon, puck = hub.devices["flo:dev1"], hub.devices["flo:puck1"]
    assert mon.name == "Main shutoff" and mon.attributes["kind"] == "water_monitor"
    assert mon.capabilities["water_usage_today"].value == 42.0 and mon.capabilities["mode"].value == "home"
    assert mon.capabilities["mode"].options == ["home", "away", "sleep"] and mon.capabilities["mode"].writable
    assert puck.capabilities["leak"].value is True and puck.capabilities["battery"].value == 87
    run(a.command(mon, "valve", "close"))
    run(a.command(mon, "mode", "sleep"))
    assert a.api.calls == [("close", "dev1"), ("sleep", "loc1", 120, "home")]
    assert mon.capabilities["valve"].value == "closed" and mon.capabilities["mode"].value == "sleep"


# ---------------------------------------------------------------- Rachio
PERSON = {"id": "p1", "devices": [{
    "id": "c1", "name": "Front Yard", "status": "ONLINE", "on": True, "model": "GENERATION3_8ZONE",
    "rainDelayExpirationDate": 0,
    "zones": [{"id": "z1", "name": "Lawn", "zoneNumber": 1, "enabled": True, "lastWateredDate": 1763300000000},
              {"id": "z2", "name": "Unused", "zoneNumber": 2, "enabled": False}]}]}


def rachio_transport(calls, running=None):
    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None,
                      req.headers.get("authorization")))
        path = req.url.path
        if path.endswith("/person/info"):
            return httpx.Response(200, json={"id": "p1"})
        if path.endswith("/person/p1"):
            return httpx.Response(200, json=PERSON)
        if path.endswith("/current_schedule"):
            return httpx.Response(200, json=running or {})
        return httpx.Response(204)
    return httpx.MockTransport(handler)


def test_rachio_values():
    now = 1_000_000
    v = rachio.controller_values({**PERSON["devices"][0], "rainDelayExpirationDate": (now + 7200) * 1000}, {}, now)
    assert v == {"status_text": "Rain delay · 2 h left", "standby": False, "rain_delay": 2}
    v = rachio.controller_values(PERSON["devices"][0], {"zoneId": "z1"}, now)
    assert v["status_text"] == "Watering Lawn"
    assert rachio.controller_values({**PERSON["devices"][0], "on": False}, {}, now)["status_text"] == "Standby"
    assert rachio.zone_minutes("run_15m") == 15 and rachio.zone_minutes("stop") is None


def test_rachio_refresh_and_commands():
    hub, calls = Hub(), []
    a = rachio.RachioAdapter(hub, {"api_key": "KEY", "poll_seconds": 60})
    assert a.poll_seconds == 300                       # never faster than the rate limit allows
    a.http = httpx.AsyncClient(base_url=rachio.API, transport=rachio_transport(calls, {"zoneId": "z1"}),
                               headers={"Authorization": "Bearer KEY"})
    run(a.refresh())
    ctrl, zone = hub.devices["rachio:c1"], hub.devices["rachio:z1"]
    assert "rachio:z2" not in hub.devices               # disabled zones are skipped
    assert ctrl.capabilities["status_text"].value == "Watering Lawn" and zone.capabilities["watering"].value is True
    assert zone.room == "Outdoor" and zone.attributes["kind"] == "irrigation_zone" and zone.attributes["last_watered"] == 1763300000
    assert ctrl.capabilities["rain_delay"].max == 72 and ctrl.capabilities["standby"].writable
    calls.clear()
    run(a.command(zone, "action", "run_10m"))
    run(a.command(zone, "action", "stop"))
    run(a.command(ctrl, "standby", True))
    run(a.command(ctrl, "rain_delay", 24))
    assert [(m, p.rsplit("/public", 1)[1], b) for m, p, b, _ in calls] == [
        ("PUT", "/zone/start", {"id": "z1", "duration": 600}),
        ("PUT", "/device/stop_water", {"id": "c1"}),
        ("PUT", "/device/off", {"id": "c1"}),
        ("PUT", "/device/rain_delay", {"id": "c1", "duration": 86400}),
    ]
    assert all(h == "Bearer KEY" for *_, h in calls)
    assert ctrl.capabilities["standby"].value is True and ctrl.capabilities["rain_delay"].value == 24


def test_water_integrations_register():
    from app.adapters import ADAPTERS
    from app.settings import SCHEMA_BY_ID
    for sid in ("waterguru", "flo", "rachio"):
        assert sid in ADAPTERS and sid in SCHEMA_BY_ID
        assert all(f["key"] for f in SCHEMA_BY_ID[sid]["fields"])


def test_missing_credentials_status():
    hub = Hub()
    for cls in (waterguru.WaterGuruAdapter, flo.FloAdapter, rachio.RachioAdapter):
        a = cls(hub, {})
        run(a.start())
        assert a.status.startswith("needs")
        run(a.stop())
