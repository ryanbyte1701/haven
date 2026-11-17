import asyncio
import json

from aiohttp import web

from app.adapters.homeassistant import NATIVE, HomeAssistantAdapter, map_entity
from app.hub import Hub
from app.models import Device, cap

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


def make_app(calls, data=None):
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
            res = (data or {"config/area_registry/list": AREAS, "config/device_registry/list": DEVICES,
                            "config/entity_registry/list": ENTITIES, "get_states": STATES}).get(t)
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


# A Home Assistant that has devices from integrations Haven connects to directly (skipped by default)
# and from ones it doesn't (imported).
NATIVE_DATA = {
    "config/area_registry/list": AREAS,
    "config_entries/get": [{"entry_id": "e_yolink", "domain": "yolink"}, {"entry_id": "e_hue", "domain": "hue"},
                           {"entry_id": "e_bn", "domain": "battery_notes"}, {"entry_id": "e_mqtt", "domain": "mqtt"},
                           {"entry_id": "e_zw", "domain": "zwave_js"}, {"entry_id": "e_esp", "domain": "esphome"},
                           {"entry_id": "e_shelly", "domain": "shelly"}, {"entry_id": "e_matter", "domain": "matter"}],
    "config/device_registry/list": [
        {"id": "dev_yolink", "name": "Leak Sensor", "config_entries": ["e_yolink"], "identifiers": [["yolink", "abc"]]},
        # no config entry listed: recognised by its identifiers
        {"id": "dev_tapo", "name": "Tapo Plug", "config_entries": [], "identifiers": [["tplink", "AA:BB"]]},
        # a Hue light another integration also attached entities to: still a Hue device
        {"id": "dev_hue", "name": "Hue Bulb", "config_entries": ["e_bn", "e_hue"], "identifiers": [["hue", "h1"]]},
        {"id": "dev_z2m", "name": "Z2M Motion", "config_entries": ["e_mqtt"], "identifiers": [["mqtt", "zigbee2mqtt_0x1"]]},
        {"id": "dev_matter", "name": "Matter Plug", "config_entries": ["e_matter"], "identifiers": [["matter", "m1"]]},
        {"id": "dev_zw", "name": "Z-Wave Lock", "config_entries": ["e_zw"], "identifiers": [["zwave_js", "1-5"]]},
        {"id": "dev_esp", "name": "ESP Sensor", "config_entries": ["e_esp"], "identifiers": [["esphome", "e1"]]},
        {"id": "dev_shelly", "name": "Shelly Relay", "config_entries": ["e_shelly"], "identifiers": [["shelly", "s1"]]},
    ],
    "config/entity_registry/list": [
        {"entity_id": "binary_sensor.leak", "device_id": "dev_yolink", "platform": "yolink"},
        {"entity_id": "switch.tapo_plug", "device_id": "dev_tapo", "platform": "tplink"},
        {"entity_id": "light.hue_bulb", "device_id": "dev_hue", "platform": "hue"},
        {"entity_id": "binary_sensor.z2m_motion", "device_id": "dev_z2m", "platform": "mqtt"},
        {"entity_id": "switch.matter_plug", "device_id": "dev_matter", "platform": "matter"},
        {"entity_id": "lock.zwave", "device_id": "dev_zw", "platform": "zwave_js"},
        {"entity_id": "sensor.esp_temp", "device_id": "dev_esp", "platform": "esphome"},
        {"entity_id": "switch.shelly", "device_id": "dev_shelly", "platform": "shelly"},
        {"entity_id": "sensor.mqtt_manual", "platform": "mqtt", "config_entry_id": "e_mqtt"},   # no device
        {"entity_id": "sensor.template_temp", "platform": "template"},                         # helper: kept
    ],
    "get_states": [
        {"entity_id": "binary_sensor.leak", "state": "off", "attributes": {"device_class": "moisture"}},
        {"entity_id": "switch.tapo_plug", "state": "on", "attributes": {}},
        {"entity_id": "light.hue_bulb", "state": "on", "attributes": {"supported_color_modes": ["onoff"]}},
        {"entity_id": "binary_sensor.z2m_motion", "state": "off", "attributes": {"device_class": "motion"}},
        {"entity_id": "switch.matter_plug", "state": "off", "attributes": {}},
        {"entity_id": "lock.zwave", "state": "locked", "attributes": {}},
        {"entity_id": "sensor.esp_temp", "state": "21", "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}},
        {"entity_id": "switch.shelly", "state": "off", "attributes": {}},
        {"entity_id": "sensor.mqtt_manual", "state": "5", "attributes": {}},
        {"entity_id": "sensor.template_temp", "state": "20", "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}},
    ],
}
SKIPPED_IDS = {"homeassistant:dev_yolink", "homeassistant:dev_tapo", "homeassistant:dev_hue", "homeassistant:dev_z2m",
               "homeassistant:dev_matter", "homeassistant:sensor_mqtt_manual"}
KEPT_IDS = {"homeassistant:dev_zw", "homeassistant:dev_esp", "homeassistant:dev_shelly", "homeassistant:sensor_template_temp"}


async def _serve(data):
    runner = web.AppRunner(make_app([], data)); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def _wait_for(pred):
    for _ in range(100):
        await asyncio.sleep(0.03)
        if pred():
            return


def test_native_mapping_covers_haven_integrations():
    for d in ("yolink", "ecowitt", "ring", "hue", "nut", "awair", "vesync", "tplink", "tapo", "flo", "rachio",
              "roborock", "matter", "mqtt", "dyson_local", "xsense", "waterguru", "chargepoint", "airnow", "open_meteo"):
        assert d in NATIVE
    for d in ("zwave_js", "zha", "esphome", "shelly", "template", "tasmota", "met", "tapo_control"):
        assert d not in NATIVE


def test_skips_devices_from_integrations_haven_supports():
    async def run():
        runner, port = await _serve(NATIVE_DATA)
        hub = Hub()
        # imported by an earlier Haven version: must disappear on the first import
        hub.upsert_device(Device(id="homeassistant:dev_yolink", name="Leak Sensor", source="homeassistant",
                                 capabilities={"leak": cap("leak")}))
        a = HomeAssistantAdapter(hub, {"url": f"http://127.0.0.1:{port}", "access_token": TOKEN})
        await a.start()
        await _wait_for(lambda: a.status.startswith("ok"))
        ids = {d for d in hub.devices if d.startswith("homeassistant:")}
        status = a.status
        msg = await a.action("refresh")
        await a.stop(); await runner.cleanup()
        return ids, status, msg
    ids, status, msg = asyncio.run(run())
    assert ids == KEPT_IDS                         # zwave_js, esphome, shelly and a template helper are imported
    assert not ids & SKIPPED_IDS                   # yolink, tplink (by identifier), hue, mqtt (device and deviceless), matter
    assert msg == ("Imported 4 devices from Home Assistant · skipped 6 from MQTT, Matter, Philips Hue, TP-Link Tapo, "
                   "YoLink (Haven connects to those directly)")
    assert status == "ok · " + msg[len("Imported "):]


def test_include_native_setting_and_removal_on_refresh():
    async def run():
        runner, port = await _serve(NATIVE_DATA)
        hub = Hub()
        a = HomeAssistantAdapter(hub, {"url": f"http://127.0.0.1:{port}", "access_token": TOKEN, "include_native": True})
        await a.start()
        await _wait_for(lambda: a.status.startswith("ok"))
        with_native = {d for d in hub.devices if d.startswith("homeassistant:")}
        first = await a.action("refresh")
        a.include_native = False                   # e.g. the user turned the setting back off
        second = await a.action("refresh")
        after = {d for d in hub.devices if d.startswith("homeassistant:")}
        await a.stop(); await runner.cleanup()
        return with_native, first, second, after
    with_native, first, second, after = asyncio.run(run())
    assert with_native == KEPT_IDS | SKIPPED_IDS and first == "Imported 10 devices from Home Assistant"
    assert after == KEPT_IDS and "skipped 6" in second    # previously imported duplicates are removed


def test_falls_back_to_identifiers_without_config_entries():
    data = {k: v for k, v in NATIVE_DATA.items() if k != "config_entries/get"}   # older HA / command not allowed
    async def run():
        runner, port = await _serve(data)
        hub = Hub()
        a = HomeAssistantAdapter(hub, {"url": f"http://127.0.0.1:{port}", "access_token": TOKEN})
        await a.start()
        await _wait_for(lambda: a.status.startswith("ok"))
        ids = {d for d in hub.devices if d.startswith("homeassistant:")}
        await a.stop(); await runner.cleanup()
        return ids
    assert asyncio.run(run()) == KEPT_IDS


def test_plugs_from_home_assistant_are_smart_plugs():
    """A Tapo/Kasa plug in HA (switch + power sensor, or an outlet switch) shows on the Power page."""
    from app.hub import Hub
    from app.adapters.homeassistant import HomeAssistantAdapter
    hub = Hub()
    a = HomeAssistantAdapter(hub, {"url": "http://ha:8123", "access_token": "x"})
    a.areas, a.devices_reg = {}, {"dev1": {"name": "Desk Plug"}, "dev2": {"name": "Heater"}, "dev3": {"name": "Fan relay"}}
    a._register("dev1", [{"entity_id": "switch.desk_plug", "state": "on", "attributes": {}},
                         {"entity_id": "sensor.desk_plug_current_consumption", "state": "42.5",
                          "attributes": {"device_class": "power", "unit_of_measurement": "W"}}])
    a._register("dev2", [{"entity_id": "switch.heater", "state": "off", "attributes": {"device_class": "outlet"}}])
    a._register("dev3", [{"entity_id": "switch.fan_relay", "state": "off", "attributes": {}}])
    kinds = {d.name: d.attributes.get("kind") for d in hub.devices.values()}
    assert kinds == {"Desk Plug": "smart_plug", "Heater": "smart_plug", "Fan relay": None}
    plug = next(d for d in hub.devices.values() if d.name == "Desk Plug")
    assert plug.capabilities["power"].value == 42.5 and plug.capabilities["switch"].value is True


def test_plug_energy_today_and_month():
    from app.hub import Hub
    from app.adapters.homeassistant import HomeAssistantAdapter
    hub = Hub()
    a = HomeAssistantAdapter(hub, {"url": "http://ha:8123", "access_token": "x"})
    a.areas, a.devices_reg = {}, {"p": {"name": "Rack", "manufacturer": "TP-Link", "model": "P110"}}
    e = {"device_class": "energy", "unit_of_measurement": "kWh"}
    a._register("p", [{"entity_id": "switch.rack", "state": "on", "attributes": {}},
                      {"entity_id": "sensor.rack_today_s_consumption", "state": "0.42", "attributes": e},
                      {"entity_id": "sensor.rack_this_month_s_consumption", "state": "12.3", "attributes": e},
                      {"entity_id": "sensor.rack_total_consumption", "state": "99", "attributes": e}])
    d = next(iter(hub.devices.values()))
    assert d.attributes["kind"] == "smart_plug"
    assert d.capabilities["energy_today"].value == 0.42 and d.capabilities["energy_month"].value == 12.3
    assert d.capabilities["energy_total"].value == 99


def test_camera_siren_and_extra_switches_are_their_own_controls():
    """A Ring camera in HA: Siren and Motion detection switches must not show as the device's Power switch."""
    from app.hub import Hub
    from app.adapters.homeassistant import HomeAssistantAdapter
    hub = Hub()
    a = HomeAssistantAdapter(hub, {"url": "http://ha:8123", "access_token": "x"})
    a.areas, a.devices_reg = {}, {"cam": {"name": "Front Camera", "manufacturer": "Ring", "model": "Stick Up Cam"},
                                  "plug": {"name": "Desk Plug"}}
    a._register("cam", [
        {"entity_id": "switch.front_camera_siren", "state": "off", "attributes": {"friendly_name": "Front Camera Siren"}},
        {"entity_id": "switch.front_camera_motion_detection", "state": "on", "attributes": {"friendly_name": "Front Camera Motion detection"}},
        {"entity_id": "siren.front_camera_siren", "state": "off", "attributes": {"friendly_name": "Front Camera Siren"}},
        {"entity_id": "light.front_camera_light", "state": "off", "attributes": {"friendly_name": "Front Camera Light"}}])
    a._register("plug", [{"entity_id": "switch.desk_plug", "state": "on", "attributes": {"friendly_name": "Desk Plug"}}])
    cam = next(d for d in hub.devices.values() if d.name == "Front Camera")
    assert "switch" not in cam.capabilities
    assert cam.capabilities["switch_siren"].label == "Siren" and cam.capabilities["switch_siren"].writable
    assert cam.capabilities["switch_motion_detection"].label == "Motion detection"
    assert a.cap_entity[(cam.id, "switch_siren")].startswith(("switch.front_camera_siren", "siren."))
    assert cam.attributes.get("kind") != "smart_plug"
    plug = next(d for d in hub.devices.values() if d.name == "Desk Plug")
    assert plug.capabilities["switch"].writable


def test_appliance_and_car_openings_are_not_doors_and_kinds():
    from app.hub import Hub
    from app.adapters.homeassistant import HomeAssistantAdapter
    hub = Hub()
    a = HomeAssistantAdapter(hub, {"url": "http://ha:8123", "access_token": "x"})
    a.areas, a.registry = {}, {}
    a.entry_domains = {"e1": "econet", "e2": "kia_uvo", "e3": "unifi"}
    a.devices_reg = {"wh": {"name": "Water Heater", "config_entries": ["e1"]},
                     "car": {"name": "Ioniq 5", "config_entries": ["e2"]}, "ap": {"name": "Office AP", "config_entries": ["e3"]}}
    a.registry = {"binary_sensor.water_heater_shutoff_valve": {"device_id": "wh"}, "binary_sensor.ioniq_5_front_left_door": {"device_id": "car"},
                  "sensor.ioniq_5_odometer": {"device_id": "car"}, "sensor.office_ap_clients": {"device_id": "ap"}}
    a._register("wh", [{"entity_id": "binary_sensor.water_heater_shutoff_valve", "state": "on",
                        "attributes": {"device_class": "opening", "friendly_name": "Water Heater Shutoff valve"}}])
    a._register("car", [{"entity_id": "binary_sensor.ioniq_5_front_left_door", "state": "off",
                         "attributes": {"device_class": "door", "friendly_name": "Ioniq 5 Front left door"}},
                        {"entity_id": "sensor.ioniq_5_odometer", "state": "100", "attributes": {"device_class": "distance", "unit_of_measurement": "mi"}}])
    a._register("ap", [{"entity_id": "sensor.office_ap_clients", "state": "4", "attributes": {}}])
    by = {d.name: d for d in hub.devices.values()}
    assert by["Water Heater"].attributes["kind"] == "appliance" and "contact" not in by["Water Heater"].capabilities
    assert by["Water Heater"].capabilities["opening_shutoff_valve"].value == "open"
    assert by["Ioniq 5"].attributes["kind"] == "vehicle" and "contact" not in by["Ioniq 5"].capabilities
    assert by["Ioniq 5"].capabilities["odometer"].value == 100
    assert by["Office AP"].attributes["kind"] == "network"
