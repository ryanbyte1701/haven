import asyncio
import json

from aiohttp import web

from app.adapters.homeassistant import HomeAssistantAdapter, map_entity
from app.hub import Hub

TOKEN = "ha-token-123"
AREAS = [{"area_id": "living", "name": "Living Room"}, {"area_id": "garage", "name": "Garage"}]
DEVICES = [{"id": "d1", "name": "Front Door Sensor", "manufacturer": "Aqara", "model": "MCCGQ11LM", "area_id": "garage"},
           {"id": "d2", "name": "Lamp", "name_by_user": "Floor Lamp", "area_id": "living"}]
ENTITIES = [{"entity_id": "binary_sensor.front_door", "device_id": "d1"},
            {"entity_id": "sensor.front_door_battery", "device_id": "d1", "entity_category": "diagnostic"},
            {"entity_id": "sensor.front_door_rssi", "device_id": "d1", "entity_category": "diagnostic"},
            {"entity_id": "light.lamp", "device_id": "d2"},
            {"entity_id": "switch.hidden", "hidden_by": "user"},
            {"entity_id": "lock.front", "area_id": "garage"}]
STATES = [
    {"entity_id": "binary_sensor.front_door", "state": "off", "attributes": {"device_class": "door", "friendly_name": "Front Door"}},
    {"entity_id": "sensor.front_door_battery", "state": "87", "attributes": {"device_class": "battery", "unit_of_measurement": "%"}},
    {"entity_id": "sensor.front_door_rssi", "state": "-60", "attributes": {"device_class": "signal_strength"}},
    {"entity_id": "light.lamp", "state": "on", "attributes": {"brightness": 128, "supported_color_modes": ["color_temp"], "color_temp_kelvin": 2700}},
    {"entity_id": "switch.hidden", "state": "on", "attributes": {}},
    {"entity_id": "lock.front", "state": "locked", "attributes": {"friendly_name": "Front Lock"}},
    {"entity_id": "sensor.outside", "state": "77", "attributes": {"device_class": "temperature", "unit_of_measurement": "°F"}},
    {"entity_id": "sensor.text_only", "state": "hello", "attributes": {}},
]


def make_app(calls):
    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid"}); await ws.close(); return ws
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            m = json.loads(msg.data)
            t = m["type"]
            res = {"config/area_registry/list": AREAS, "config/device_registry/list": DEVICES,
                   "config/entity_registry/list": ENTITIES, "get_states": STATES}.get(t)
            if t == "call_service":
                calls.append(m)
            await ws.send_json({"id": m["id"], "type": "result", "success": True, "result": res})
            if t == "subscribe_events":
                await ws.send_json({"id": m["id"], "type": "event", "event": {"data": {"entity_id": "binary_sensor.front_door",
                                    "new_state": {"entity_id": "binary_sensor.front_door", "state": "on", "attributes": {"device_class": "door"}}}}})
        return ws
    app = web.Application(); app.router.add_get("/api/websocket", ws_handler)
    return app


def test_mapping():
    assert map_entity("binary_sensor.x", "on", {"device_class": "moisture"}) == {"leak": (True, {})}
    assert map_entity("sensor.t", "68", {"device_class": "temperature", "unit_of_measurement": "°F"})["temperature"][0] == 20.0
    assert map_entity("sensor.p", "1.5", {"device_class": "power", "unit_of_measurement": "kW"})["power"][0] == 1500
    assert map_entity("sensor.txt", "abc", {}) is None
    assert map_entity("lock.a", "unlocked", {})["lock"][0] == "unlocked"
    assert map_entity("person.me", "home", {}) is None


def test_import_live_updates_and_commands():
    async def run():
        calls = []
        runner = web.AppRunner(make_app(calls)); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        hub = Hub()
        a = HomeAssistantAdapter(hub, {"url": f"http://127.0.0.1:{port}", "access_token": TOKEN})
        await a.start()
        for _ in range(100):
            await asyncio.sleep(0.03)
            door = hub.devices.get("homeassistant:d1")
            if door and door.capabilities["contact"].value == "open":
                break
        lamp = hub.devices["homeassistant:d2"]
        live_contact = hub.devices["homeassistant:d1"].capabilities["contact"].value
        await a.command(hub.devices["homeassistant:lock_front"], "lock", "unlock")
        await a.command(lamp, "brightness", 40)
        msg = await a.action("refresh")
        await asyncio.sleep(0.2)
        status = a.status
        await a.stop(); await runner.cleanup()
        return hub, calls, status, msg, live_contact
    hub, calls, status, msg, live_contact = asyncio.run(run())
    door = hub.devices["homeassistant:d1"]
    assert door.name == "Front Door Sensor" and door.room == "Garage" and door.model == "Aqara MCCGQ11LM"
    assert live_contact == "open"                                             # live state_changed applied
    assert door.capabilities["contact"].value == "closed"                     # Refresh now re-read HA's (fake) states
    assert door.capabilities["battery"].value == 87 and "rssi" not in door.capabilities   # diagnostic battery kept, rssi skipped
    lamp = hub.devices["homeassistant:d2"]
    assert lamp.name == "Floor Lamp" and lamp.room == "Living Room" and lamp.capabilities["brightness"].value == 50
    assert lamp.capabilities["light"].writable and lamp.attributes["kind"] == "light"
    assert "homeassistant:switch_hidden" not in hub.devices and "homeassistant:sensor_text_only" not in hub.devices
    assert hub.devices["homeassistant:sensor_outside"].capabilities["temperature"].value == 25.0
    assert {(c["domain"], c["service"]) for c in calls} == {("lock", "unlock"), ("light", "turn_on")}
    assert next(c for c in calls if c["domain"] == "light")["service_data"] == {"brightness_pct": 40}
    assert status.startswith("ok") and "Home Assistant" in msg


def test_bad_token_and_missing_config():
    async def run():
        runner = web.AppRunner(make_app([])); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        a = HomeAssistantAdapter(Hub(), {"url": f"http://127.0.0.1:{port}", "access_token": "wrong"})
        await a.start(); await asyncio.sleep(0.4)
        st = a.status
        await a.stop(); await runner.cleanup()
        b = HomeAssistantAdapter(Hub(), {})
        await b.start()
        return st, b.status
    st, missing = asyncio.run(run())
    assert "rejected" in st and "access token" in missing
