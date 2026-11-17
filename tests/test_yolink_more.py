"""YoLink: valves of every kind and battery levels, end to end (getState / MQTT / commands -> hub).

Payloads follow doc.yosmart.com and what the yolink-api package (Home Assistant's YoLink library) parses.
"""
import asyncio
import json

import httpx

from app.adapters import yolink
from app.hub import Hub

HOME = "a1b2c3d4e5f6"

DEVICES = [
    {"deviceId": "d88b4c0100000001", "name": "Pool ATO Valve", "type": "Manipulator", "modelName": "YS5003-UC", "token": "t1"},
    {"deviceId": "d88b4c0100000002", "name": "Main Water", "type": "WaterMeterController", "modelName": "YS5018-UC", "token": "t2"},
    {"deviceId": "d88b4c0100000003", "name": "Garden Taps", "type": "WaterMeterMultiController", "modelName": "YS5029-UC", "token": "t3"},
    {"deviceId": "d88b4c0100000004", "name": "Lawn Timer", "type": "SprinklerV2", "modelName": "YS4102-UC", "token": "t4"},
    {"deviceId": "d88b4c0100000005", "name": "Drip Zones", "type": "Sprinkler", "modelName": "YS4101-UC", "token": "t5"},
    {"deviceId": "d88b4c0100000006", "name": "Back Door", "type": "DoorSensor", "modelName": "YS7704-UC", "token": "t6"},
    {"deviceId": "d88b4c0100000007", "name": "Water Heater Leak", "type": "LeakSensor", "modelName": "YS7903-UC", "token": "t7"},
    {"deviceId": "d88b4c0100000008", "name": "Meter Only", "type": "WaterMeterController", "modelName": "YS5007-UC", "token": "t8"},
    {"deviceId": "d88b4c0100000009", "name": "Shutoff", "type": "WaterLeakController", "modelName": "YS5009-UC", "token": "t9"},
    {"deviceId": "d88b4c010000000a", "name": "Hub", "type": "Hub", "modelName": "YS1603-UC", "token": "ta"},
    {"deviceId": "d88b4c010000000b", "name": "Garage TH", "type": "THSensor", "modelName": "YS8003-UC", "token": "tb"},
]
ID = {d["name"]: d["deviceId"] for d in DEVICES}

# <Type>.getState `data` (BUDP), as the cloud returns it
STATES = {
    # Manipulator: state is a string, battery next to it
    ID["Pool ATO Valve"]: {"online": True, "deviceId": ID["Pool ATO Valve"], "reportAt": "2026-11-16T17:20:11.000Z",
                           "state": {"state": "closed", "battery": 3, "delay": {"ch": 1, "on": 0, "off": 0}, "openRemind": 0,
                                     "version": "040b", "time": "2026-11-16 9:20:11", "tz": -8,
                                     "loraInfo": {"netId": "010201", "signal": -71, "gatewayId": "x", "gateways": 1}}},
    # FlowSmart meter + valve: valve is "close"/"open" inside state.state; on mains (PowerLine)
    ID["Main Water"]: {"online": True, "state": {
        "state": {"valve": "close", "meter": 125530, "waterFlowing": False},
        "alarm": {"openReminder": False, "leak": False, "amountOverrun": False, "durationOverrun": False,
                  "valveError": False, "reminder": False, "freezeError": False},
        "battery": 4, "powerSupply": "PowerLine", "valveDelay": {"on": 0, "off": 0},
        "attributes": {"meterUnit": 0, "meterStepFactor": 10, "autoCloseValve": True}, "temperature": 18.5,
        "version": "0412", "tz": -8}},
    ID["Garden Taps"]: {"online": True, "state": {
        "state": {"valves": {"0": "open", "1": "close"}, "meters": {"0": 1200, "1": 300}, "waterFlowing": True},
        "battery": "2", "powerSupply": "battery", "version": "0201"}},
    ID["Lawn Timer"]: {"online": True, "state": {
        "state": {"running": False, "noWaterWhenRunning": False}, "waterMode": "manual", "battery": 1, "version": "0105"}},
    ID["Drip Zones"]: {"online": True, "state": {
        "state": {"mode": "manual", "zoneSize": 4, "delay": 0}, "setting": {"maxWaterTime": 60}, "version": "0301"}},
    # battery is documented as a String ("0".."4") for the door sensor
    ID["Back Door"]: {"online": True, "reportAt": "2026-11-16T17:01:00.000Z",
                      "state": {"state": "closed", "battery": "4", "alertInterval": 0, "delay": 0, "version": "0602",
                                "stateChangedAt": 1763312460000}},
    ID["Water Heater Leak"]: {"online": True, "state": {"state": "normal", "battery": 0, "devTemperature": 21,
                                                        "interval": 0, "version": "0503"}},
    ID["Meter Only"]: {"online": True, "state": {"state": {"meter": 900, "waterFlowing": False}, "battery": 4,
                                                 "powerSupply": "battery", "alarm": {"leak": True}}},
    ID["Shutoff"]: {"online": True, "state": {"state": {"valve": "open", "waterTemp": 14.2}, "alarm": {"leak": False},
                                              "battery": 4, "powerSupply": "battery"}},
    ID["Garage TH"]: {"online": True, "state": {"state": "normal", "battery": 2, "temperature": 11.4, "humidity": 62.3,
                                                "mode": "f", "version": "0403"}},
}


class FakeYoLink(yolink.YoLinkAdapter):
    """The adapter with the cloud API replaced by recorded answers."""
    REFRESH_AFTER_COMMAND = (0,)

    def __init__(self, hub, states=None):
        super().__init__(hub, {"uaid": "ua_x", "secret_key": "sec_x"})
        self.states = json.loads(json.dumps(states or STATES))
        self.calls: list[tuple[str, dict]] = []
        self.answers: dict[str, dict] = {}

    async def _call(self, method, **body):
        self.calls.append((method, body))
        if method == "Home.getGeneralInfo":
            return {"id": HOME}
        if method == "Home.getDeviceList":
            return {"devices": DEVICES}
        if method.endswith(".getState"):
            return self.states[body["targetDevice"]]
        return self.answers.get(method, {})


def run(coro):
    return asyncio.run(coro)


def setup():
    hub = Hub()
    a = FakeYoLink(hub)
    hub.adapters["yolink"] = a

    async def go():
        await a._discover()
        for did in list(a.raw):
            await a._refresh(did)
    run(go())
    return hub, a


def val(hub, name, capname, suffix=""):
    d = hub.devices[f"yolink:{ID[name]}{suffix}"]
    c = d.capabilities.get(capname)
    return c.value if c else "<missing>"


def mqtt(a, name, event, data):
    did = ID[name]
    a.handle_message(f"yl-home/{HOME}/{did}/report", json.dumps(
        {"event": event, "time": 1763313600000, "msgid": "1763313600000", "data": data, "deviceId": did}).encode())


# ---------------------------------------------------------------- discovery
def test_discovery_valve_capabilities_and_source():
    hub, a = setup()
    for name in ("Pool ATO Valve", "Main Water", "Garden Taps", "Lawn Timer", "Drip Zones", "Shutoff"):
        c = hub.devices[f"yolink:{ID[name]}"].capabilities["valve"]
        assert c.writable and c.options == ["open", "close"], name
    assert "valve" in hub.devices[f"yolink:{ID['Garden Taps']}-valve2"].capabilities
    assert "valve" not in hub.devices[f"yolink:{ID['Meter Only']}"].capabilities   # YS5007 has no valve
    ato = hub.devices[f"yolink:{ID['Pool ATO Valve']}"]
    assert ato.source == "yolink" and ato.model == "YS5003-UC · YoLink valve controller"
    assert ato.attributes["yolink_type"] == "Manipulator"
    assert f"yolink:{ID['Hub']}" not in hub.devices


# ---------------------------------------------------------------- getState
def test_getstate_valves():
    hub, _ = setup()
    assert val(hub, "Pool ATO Valve", "valve") == "closed"
    assert val(hub, "Main Water", "valve") == "closed"          # "close" -> "closed"
    assert val(hub, "Main Water", "leak") is False
    assert val(hub, "Garden Taps", "valve") == "open"
    assert val(hub, "Garden Taps", "valve", "-valve2") == "closed"
    assert val(hub, "Lawn Timer", "valve") == "closed"
    assert val(hub, "Drip Zones", "valve") == "closed"
    assert val(hub, "Shutoff", "valve") == "open"
    assert val(hub, "Meter Only", "leak") is True


def test_getstate_batteries():
    hub, _ = setup()
    assert val(hub, "Pool ATO Valve", "battery") == 75
    assert val(hub, "Back Door", "battery") == 100              # "4" (string) -> 100 %
    assert val(hub, "Back Door", "contact") == "closed"
    assert val(hub, "Water Heater Leak", "battery") == 0
    assert val(hub, "Garden Taps", "battery") == 50
    assert val(hub, "Lawn Timer", "battery") == 25
    assert val(hub, "Garage TH", "battery") == 50 and val(hub, "Garage TH", "temperature") == 11.4
    assert val(hub, "Main Water", "battery") == "<missing>"     # on mains: no fake battery
    assert val(hub, "Drip Zones", "battery") == "<missing>"     # mains sprinkler reports none
    assert hub.devices[f"yolink:{ID['Pool ATO Valve']}"].capabilities["battery"].unit == "%"
    assert "temperature" not in hub.devices[f"yolink:{ID['Main Water']}"].capabilities


def test_getstate_flat_shape_and_offline():
    # some answers carry the record next to "online" (state is the plain string)
    hub, a = setup()
    a.states[ID["Pool ATO Valve"]] = {"online": False, "state": "open", "battery": 4, "version": "040b"}
    run(a._refresh(ID["Pool ATO Valve"]))
    d = hub.devices[f"yolink:{ID['Pool ATO Valve']}"]
    assert d.capabilities["valve"].value == "open" and d.capabilities["battery"].value == 100 and d.online is False


def test_battery_levels():
    assert [yolink.battery_pct(x) for x in (0, 1, 2, 3, 4, "3", 4.0, None, True, "x", 87)] == \
        [0, 25, 50, 75, 100, 75, 100, None, None, None, 87]


# ---------------------------------------------------------------- MQTT
def test_mqtt_manipulator_events():
    hub, a = setup()
    mqtt(a, "Pool ATO Valve", "Manipulator.StatusChange", {"state": "open", "battery": 4, "delay": {"on": 0, "off": 0},
                                                           "loraInfo": {"signal": -70}})
    assert val(hub, "Pool ATO Valve", "valve") == "open" and val(hub, "Pool ATO Valve", "battery") == 100
    mqtt(a, "Pool ATO Valve", "Manipulator.setState", {"state": "closed", "loraInfo": {"signal": -70}})
    assert val(hub, "Pool ATO Valve", "valve") == "closed"
    mqtt(a, "Pool ATO Valve", "Manipulator.Report", {"state": "open", "battery": 1, "version": "040b", "tz": -8})
    assert val(hub, "Pool ATO Valve", "valve") == "open" and val(hub, "Pool ATO Valve", "battery") == 25
    mqtt(a, "Pool ATO Valve", "Manipulator.getState", {"state": "closed", "battery": 1})
    assert val(hub, "Pool ATO Valve", "valve") == "closed"


def test_mqtt_water_meter_and_multi():
    hub, a = setup()
    mqtt(a, "Main Water", "WaterMeterController.Report", {
        "state": {"valve": "open", "meter": 125600, "waterFlowing": True}, "alarm": {"leak": False},
        "battery": 4, "powerSupply": "PowerLine"})
    assert val(hub, "Main Water", "valve") == "open"
    mqtt(a, "Main Water", "WaterMeterController.Alert", {
        "state": {"valve": "close", "meter": 125700, "waterFlowing": False}, "alarm": {"leak": True}, "powerSupply": "PowerLine"})
    assert val(hub, "Main Water", "valve") == "closed" and val(hub, "Main Water", "leak") is True
    mqtt(a, "Garden Taps", "WaterMeterMultiController.StatusChange", {
        "state": {"valves": {"0": "close", "1": "open"}, "meters": {"0": 1210, "1": 305}}, "battery": 3})
    assert val(hub, "Garden Taps", "valve") == "closed" and val(hub, "Garden Taps", "valve", "-valve2") == "open"
    assert val(hub, "Garden Taps", "battery") == 75


def test_mqtt_sprinklers():
    hub, a = setup()
    mqtt(a, "Lawn Timer", "SprinklerV2.StatusChange", {"state": {"running": True, "noWaterWhenRunning": False}, "battery": 4})
    assert val(hub, "Lawn Timer", "valve") == "open" and val(hub, "Lawn Timer", "battery") == 100
    mqtt(a, "Drip Zones", "Sprinkler.waterReport", {"event": "start", "zone": 1})
    assert val(hub, "Drip Zones", "valve") == "open"
    mqtt(a, "Drip Zones", "Sprinkler.Report", {"state": {"mode": "manual", "watering": {"zone": 1, "total": 10, "left": 4}}})
    assert val(hub, "Drip Zones", "valve") == "open"
    mqtt(a, "Drip Zones", "Sprinkler.waterReport", {"event": "end", "zone": 1})
    assert val(hub, "Drip Zones", "valve") == "closed"


def test_mqtt_battery_only_on_alert_and_records():
    hub, a = setup()
    mqtt(a, "Back Door", "DoorSensor.Alert", {"state": "open", "alertType": "normal", "battery": "1", "version": "0602",
                                              "loraInfo": {"signal": -90}, "stateChangedAt": 1763313600000})
    assert val(hub, "Back Door", "contact") == "open" and val(hub, "Back Door", "battery") == 25
    mqtt(a, "Water Heater Leak", "LeakSensor.Report", {"state": "normal", "battery": 3, "interval": 0})
    assert val(hub, "Water Heater Leak", "battery") == 75 and val(hub, "Water Heater Leak", "leak") is False
    mqtt(a, "Garage TH", "THSensor.DataRecord", {"records": [{"time": 1, "temperature": 9.0, "humidity": 50},
                                                             {"time": 2, "temperature": 9.5, "humidity": 51}]})
    assert val(hub, "Garage TH", "temperature") == 9.5


def test_mqtt_ignores_junk_and_unknown():
    hub, a = setup()
    a.handle_message(f"yl-home/{HOME}/{ID['Pool ATO Valve']}/report", b"not json")
    a.handle_message(f"yl-home/{HOME}/{ID['Pool ATO Valve']}/report", b"[1,2]")
    mqtt(a, "Pool ATO Valve", "Manipulator.getSchedules", {"0": {"on": "07:00", "off": "07:10"}})
    mqtt(a, "Pool ATO Valve", "Manipulator.StatusChange", {"state": {"weird": 1}, "battery": "full?"})
    assert val(hub, "Pool ATO Valve", "valve") == "closed"
    # deviceId only in the topic
    a.handle_message(f"yl-home/{HOME}/{ID['Pool ATO Valve']}/report",
                     json.dumps({"event": "Manipulator.StatusChange", "data": {"state": "open"}}).encode())
    assert val(hub, "Pool ATO Valve", "valve") == "open"


# ---------------------------------------------------------------- commands
def test_commands_send_right_params_and_refresh():
    hub, a = setup()

    async def go():
        a.calls.clear()
        a.answers["Manipulator.setState"] = {"state": "open", "loraInfo": {"signal": -70}}
        a.states[ID["Pool ATO Valve"]]["state"]["state"] = "open"
        await hub.command(f"yolink:{ID['Pool ATO Valve']}", "valve", "open")
        assert val(hub, "Pool ATO Valve", "valve") == "open"
        await hub.command(f"yolink:{ID['Main Water']}", "valve", "open")      # answer without state: optimistic
        assert val(hub, "Main Water", "valve") == "open"
        await hub.command(f"yolink:{ID['Garden Taps']}-valve2", "valve", "open")
        await hub.command(f"yolink:{ID['Lawn Timer']}", "valve", "open")
        await hub.command(f"yolink:{ID['Drip Zones']}", "valve", "close")
        await asyncio.sleep(0.05)       # the after-command refreshes
        return list(a.calls)
    calls = run(go())
    sent = [(m, b.get("params")) for m, b in calls if not m.endswith("getState")]
    assert sent == [("Manipulator.setState", {"state": "open"}),
                    ("WaterMeterController.setState", {"valve": "open"}),
                    ("WaterMeterMultiController.setState", {"valves": {"1": "open"}}),
                    ("SprinklerV2.setState", {"running": True}),
                    ("Sprinkler.setManualWater", {"state": "stop"})]
    refreshed = {b["targetDevice"] for m, b in calls if m.endswith("getState")}
    assert ID["Pool ATO Valve"] in refreshed and ID["Garden Taps"] in refreshed
    # the refresh read the real state back (the meter valve didn't open in the recorded getState)
    assert val(hub, "Main Water", "valve") == "closed"
    assert val(hub, "Pool ATO Valve", "valve") == "open"


# ---------------------------------------------------------------- MQTT down -> faster polling, status
class FakeClient:
    """Stands in for aiomqtt.Client: first connection is refused, second delivers one message."""
    attempts = 0
    users: list = []

    def __init__(self, host, port, username=None, password=None, **kw):
        FakeClient.users.append((username, password))
        self.messages = self._gen()

    async def __aenter__(self):
        FakeClient.attempts += 1
        if FakeClient.attempts == 1:
            raise yolink.aiomqtt.MqttError("Not authorized")
        return self

    async def __aexit__(self, *a):
        return False

    async def subscribe(self, topic):
        self.topic = topic

    async def _gen(self):
        class M:
            topic = f"yl-home/{HOME}/{ID['Pool ATO Valve']}/report"
            payload = json.dumps({"event": "Manipulator.StatusChange", "deviceId": ID["Pool ATO Valve"],
                                  "data": {"state": "open", "battery": 2}}).encode()
        yield M()
        await asyncio.sleep(3600)


def test_mqtt_reconnects_with_fresh_token_and_status(monkeypatch):
    hub, a = setup()
    a.status = "ok"
    a.MQTT_RETRY = 0.01
    tokens = iter(["tok1", "tok2", "tok3"])

    async def get_token(force=False):
        if force or not a.token:
            a.token, a.token_exp = next(tokens), yolink.time.time() + 7200
        return a.token
    monkeypatch.setattr(a, "_get_token", get_token)
    monkeypatch.setattr(yolink.aiomqtt, "Client", FakeClient)
    FakeClient.attempts, FakeClient.users = 0, []

    async def go():
        a.mqtt_connected = True
        t = asyncio.create_task(a._mqtt_loop())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if val(hub, "Pool ATO Valve", "valve") == "open":
                break
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    run(go())
    assert FakeClient.users[0] == ("tok1", "") and FakeClient.users[1] == ("tok2", "")   # refused -> new token
    assert val(hub, "Pool ATO Valve", "valve") == "open" and val(hub, "Pool ATO Valve", "battery") == 50
    assert a.mqtt_connected and a.status == "ok"


def test_status_and_interval_when_mqtt_down():
    hub, a = setup()
    a.status = "ok"
    a.mqtt_connected = True
    assert a.interval == 900
    a._mqtt_down("Not authorized")
    assert a.interval == 60
    assert "MQTT" in a.status and "60 s" in a.status and not a.status.startswith(("ok", "error"))
    import re
    assert not re.search(r"error|refused|failed|denied|timed out", a.status, re.I)   # shown as info, not red


def test_call_retries_with_new_token_when_expired():
    seen = []

    def handler(req: httpx.Request):
        if req.url.path.endswith("/token"):
            n = sum(1 for s in seen if s == "token")
            seen.append("token")
            return httpx.Response(200, json={"access_token": f"tok{n}", "expires_in": 7200})
        seen.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer tok0":
            return httpx.Response(200, json={"code": "010104", "desc": "Token is expired"})
        return httpx.Response(200, json={"code": "000000", "data": {"state": {"state": "open"}}})

    a = yolink.YoLinkAdapter(Hub(), {"uaid": "u", "secret_key": "s"})
    a.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    data = run(a._call("Manipulator.getState", targetDevice="x", token="t"))
    assert data == {"state": {"state": "open"}}
    assert seen == ["token", "Bearer tok0", "token", "Bearer tok1"]
