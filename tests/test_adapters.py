import json

from app.adapters import ecowitt, mqtt, xsense, yolink
from app.hub import Hub
from app.models import Device, cap


def test_yolink_normalize():
    assert yolink.normalize("DoorSensor", {"state": "open", "battery": 4}) == {"contact": "open", "battery": 100}
    assert yolink.normalize("LeakSensor", {"state": "alert", "battery": 1})["leak"] is True
    assert yolink.normalize("THSensor", {"temperature": 21.5, "humidity": 30, "state": "normal"})["temperature"] == 21.5
    assert yolink.normalize("LockV2", {"state": {"lock": "locked", "door": "closed"}}) == {"lock": "locked", "contact": "closed"}
    assert yolink.command_params("Manipulator", "valve", "close") == {"state": "close"}
    assert yolink.command_params("LockV2", "lock", "unlock") == {"state": {"lock": "unlock"}}


def test_ecowitt_parse():
    g = ecowitt.parse({"PASSKEY": "X", "tempinf": "72.5", "humidityin": "30", "tempf": "98.6",
                       "temp1f": "68.0", "humidity1": "40", "batt1": "0", "soilmoisture1": "35",
                       "leak_ch1": "1", "baromrelin": "29.92"})
    assert g["indoor"]["values"]["temperature"] == 22.5
    assert g["outdoor"]["values"]["temperature"] == 37.0
    assert g["ch1"]["values"] == {"temperature": 20.0, "humidity": 40.0, "battery_low": False}
    assert g["soil1"]["values"]["moisture"] == 35.0
    assert g["leak1"]["values"]["leak"] is True
    assert round(g["indoor"]["values"]["pressure"]) == 1013


def test_template_path():
    assert mqtt.template_path("{{ value_json.temperature }}") == ["temperature"]
    assert mqtt.template_path("{{ value_json['state'] | lower }}") == ["state"]
    assert mqtt.template_path("{{ value_json.a.b }}") == ["a", "b"]


def test_mqtt_discovery_z2m(tmp_path, monkeypatch):
    hub = Hub()
    a = mqtt.MqttAdapter(hub, {})
    # Zigbee2MQTT-style door sensor: contact=false means OPEN
    a.handle_message("homeassistant/binary_sensor/0x00158d0001/contact/config", json.dumps({
        "device_class": "door", "state_topic": "zigbee2mqtt/Back Door", "value_template": "{{ value_json.contact }}",
        "payload_on": False, "payload_off": True, "unique_id": "0x00158d0001_contact_z2m",
        "device": {"identifiers": ["zigbee2mqtt_0x00158d0001"], "name": "Back Door", "model": "MCCGQ11LM", "manufacturer": "Aqara"}}).encode())
    a.handle_message("homeassistant/sensor/0x00158d0001/battery/config", json.dumps({
        "device_class": "battery", "stat_t": "zigbee2mqtt/Back Door", "val_tpl": "{{ value_json.battery }}",
        "unit_of_meas": "%", "uniq_id": "0x00158d0001_battery_z2m", "dev": {"ids": ["zigbee2mqtt_0x00158d0001"]}}).encode())
    a.handle_message("zigbee2mqtt/Back Door", b'{"contact": false, "battery": 91}')
    d = hub.devices["mqtt:zigbee2mqtt_0x00158d0001"]
    assert d.capabilities["contact"].value == "open"
    assert d.capabilities["battery"].value == 91
    # lock with command
    a.handle_message("homeassistant/lock/0xabc/lock/config", json.dumps({
        "state_topic": "zigbee2mqtt/Front Lock", "command_topic": "zigbee2mqtt/Front Lock/set",
        "value_template": "{{ value_json.state }}", "state_locked": "LOCK", "state_unlocked": "UNLOCK",
        "device": {"identifiers": ["z_abc"], "name": "Front Lock"}}).encode())
    a.handle_message("zigbee2mqtt/Front Lock", b'{"state": "UNLOCK"}')
    lock = hub.devices["mqtt:z_abc"].capabilities["lock"]
    assert lock.value == "unlocked" and lock.writable
    assert a.by_cap[("mqtt:z_abc", "lock")].command_payload("lock") == "LOCK"


def test_xsense_normalize():
    assert xsense.normalize("temperature", {"temperature": 72.0, "temperatureUnit": "1", "humidity": 20.0, "batInfo": 3}) == \
        {"temperature": 22.22, "humidity": 20.0, "battery": 100}
    assert xsense.normalize("water", {"alarmStatus": True})["leak"] is True
    assert xsense.normalize("combi", {"alarmStatus": True, "coPpm": 120})["co"] is True


def test_hub_command_validation():
    import asyncio
    hub = Hub()
    hub.upsert_device(Device(id="x:1", name="V", source="x", capabilities={"valve": cap("valve"), "temperature": cap("temperature")}))
    for capname, val in (("temperature", 1), ("valve", "explode")):
        try:
            asyncio.run(hub.command("x:1", capname, val))
            assert False
        except ValueError:
            pass


def test_layout_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    from app.hub import Hub
    monkeypatch.setattr(m, "hub", Hub())
    m.hub._layout_path = tmp_path / "layout.json"
    with TestClient(m.app) as c:
        assert c.get("/api/layout").json() == {"rooms": [], "order": {}, "pinned": [], "views": []}
        body = {"rooms": ["Garage", "Entry"], "order": {"Entry": ["demo:front_lock", "demo:front_door"]}, "pinned": ["demo:main_valve"],
                "views": [{"id": "v1", "name": "Bedtime", "icon": "bedtime", "rooms": ["Entry"], "types": ["doors"], "sources": [], "devices": ["demo:main_valve"]}]}
        assert c.put("/api/layout", json=body).json() == body
        assert c.get("/api/layout").json() == body
    import json as _j
    assert _j.loads((tmp_path / "layout.json").read_text())["pinned"] == ["demo:main_valve"]


def test_device_tab_override(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    from app.hub import Hub
    from app.models import Device, cap
    h = Hub(); h._overrides_path = tmp_path / "o.json"; h.overrides = {}
    monkeypatch.setattr(m, "hub", h)
    h.upsert_device(Device(id="x:mail", name="Mailbox", source="x", capabilities={"contact": cap("contact")}))
    with TestClient(m.app) as c:
        r = c.patch("/api/devices/x:mail", json={"categories": ["security"]}).json()
        assert r["categories"] == ["security"]
        assert c.patch("/api/devices/x:mail", json={"categories": ["kitchen"]}).status_code == 400
        r = c.patch("/api/devices/x:mail", json={"auto_categories": True}).json()
        assert r["categories"] is None
        r = c.patch("/api/devices/x:mail", json={"categories": []}).json()      # in no tab (still on Home)
        assert r["categories"] == []
