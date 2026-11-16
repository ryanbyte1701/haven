import asyncio
import json

from app.adapters import demo, hue, mqtt
from app.color import hex_to_xy, kelvin_to_mirek, mirek_to_kelvin, xy_to_hex
from app.hub import Hub


def test_color_roundtrip():
    import colorsys
    for h in ("#ff0000", "#00ff00", "#0000ff", "#ffffff", "#ffa040", "#8000ff"):   # full-brightness colors
        x, y = hex_to_xy(h)
        back = xy_to_hex(x, y)
        assert all(abs(int(h[i:i + 2], 16) - int(back[i:i + 2], 16)) <= 3 for i in (1, 3, 5)), (h, back)
    # xy carries no brightness: a dim color comes back at full brightness with the same hue
    hue_of = lambda c: colorsys.rgb_to_hsv(*(int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)))[0]  # noqa: E731
    back = xy_to_hex(*hex_to_xy("#20c0a0"))
    assert abs(hue_of(back) - hue_of("#20c0a0")) < 0.01 and max(int(back[i:i + 2], 16) for i in (1, 3, 5)) == 255
    # out-of-gamut xy still produces a valid color
    assert xy_to_hex(0.05, 0.9).startswith("#") and len(xy_to_hex(0.05, 0.9)) == 7


def test_mirek_kelvin():
    assert mirek_to_kelvin(153) == 6536 and mirek_to_kelvin(500) == 2000
    assert kelvin_to_mirek(2700) == 370 and mirek_to_kelvin(kelvin_to_mirek(4000)) == 4000


DEVICE_ID, LIGHT_ID, LIGHT2_ID, ROOM_ID, GL_ID, ZONE_ID, GL2_ID = "dev-1", "light-1", "light-2", "room-1", "gl-1", "zone-1", "gl-2"
PAYLOAD = {
    "device": [{"id": DEVICE_ID, "type": "device", "metadata": {"name": "Floor Lamp"},
                "product_data": {"model_id": "LCA001", "product_name": "Hue color lamp", "manufacturer_name": "Signify"},
                "services": [{"rid": LIGHT_ID, "rtype": "light"}, {"rid": "zc-1", "rtype": "zigbee_connectivity"}]},
               {"id": "dev-2", "type": "device", "metadata": {"name": "Hall"}, "product_data": {"product_name": "Hue white lamp"},
                "services": [{"rid": LIGHT2_ID, "rtype": "light"}]}],
    "light": [{"id": LIGHT_ID, "type": "light", "owner": {"rid": DEVICE_ID, "rtype": "device"},
               "metadata": {"name": "Floor Lamp", "archetype": "floor_shade"}, "on": {"on": True},
               "dimming": {"brightness": 74.7, "min_dim_level": 0.2},
               "color_temperature": {"mirek": 366, "mirek_valid": True, "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500}},
               "color": {"xy": {"x": 0.4573, "y": 0.41}, "gamut_type": "C"}},
              {"id": LIGHT2_ID, "type": "light", "owner": {"rid": "dev-2", "rtype": "device"},
               "metadata": {"name": "Hall"}, "on": {"on": False}, "dimming": {"brightness": 100.0}}],
    "room": [{"id": ROOM_ID, "type": "room", "metadata": {"name": "Living Room", "archetype": "living_room"},
              "children": [{"rid": DEVICE_ID, "rtype": "device"}], "services": [{"rid": GL_ID, "rtype": "grouped_light"}]}],
    "zone": [{"id": ZONE_ID, "type": "zone", "metadata": {"name": "Downstairs"},
              "children": [{"rid": LIGHT_ID, "rtype": "light"}, {"rid": LIGHT2_ID, "rtype": "light"}],
              "services": [{"rid": GL2_ID, "rtype": "grouped_light"}]}],
    "grouped_light": [{"id": GL_ID, "type": "grouped_light", "owner": {"rid": ROOM_ID, "rtype": "room"}, "on": {"on": True},
                       "dimming": {"brightness": 74.7}},
                      {"id": GL2_ID, "type": "grouped_light", "owner": {"rid": ZONE_ID, "rtype": "zone"}, "on": {"on": True},
                       "dimming": {"brightness": 37.0}}],
    "zigbee_connectivity": [{"id": "zc-1", "type": "zigbee_connectivity", "owner": {"rid": DEVICE_ID, "rtype": "device"},
                             "status": "connected"},
                            {"id": "zc-2", "type": "zigbee_connectivity", "owner": {"rid": "dev-2", "rtype": "device"},
                             "status": "connectivity_issue"}],
}


def _adapter():
    hub = Hub()
    a = hue.HueAdapter(hub, {"host": "10.0.0.2", "app_key": "k"})
    for t in hue.RESOURCES:
        a.res[t] = {x["id"]: x for x in PAYLOAD.get(t, [])}
    a.sync()
    return hub, a


def test_hue_parse():
    hub, a = _adapter()
    lamp = hub.devices["hue:light-1"]
    v = {k: c.value for k, c in lamp.capabilities.items()}
    assert v["light"] is True and v["brightness"] == 75 and v["color_temp"] == 2732 and v["color"].startswith("#")
    assert lamp.room == "Living Room" and lamp.model == "Hue color lamp" and lamp.online
    assert lamp.attributes["kind"] == "light" and lamp.attributes["ct_min"] == 2000 and lamp.attributes["ct_max"] == 6536
    assert all(lamp.capabilities[k].writable for k in ("light", "brightness", "color_temp", "color"))
    assert lamp.capabilities["brightness"].unit == "%" and lamp.capabilities["color_temp"].unit == "K"
    hall = hub.devices["hue:light-2"]
    assert set(hall.capabilities) == {"light", "brightness"} and hall.online is False
    room = hub.devices["hue:group_gl-1"]
    assert room.attributes["kind"] == "group" and room.attributes["members"] == ["hue:light-1"]
    assert room.capabilities["light"].value is True and room.capabilities["color"].value == lamp.capabilities["color"].value
    zone = hub.devices["hue:group_gl-2"]
    assert zone.attributes["members"] == ["hue:light-1", "hue:light-2"] and zone.capabilities["brightness"].value == 37


def test_hue_events_and_commands():
    hub, a = _adapter()
    a.apply_events([{"type": "update", "id": "e1", "data": [
        {"id": LIGHT_ID, "type": "light", "on": {"on": False}, "owner": {"rid": DEVICE_ID, "rtype": "device"}},
        {"id": GL_ID, "type": "grouped_light", "dimming": {"brightness": 20.0}}]}])
    assert hub.devices["hue:light-1"].capabilities["light"].value is False
    assert hub.devices["hue:light-1"].capabilities["brightness"].value == 75   # unchanged fields kept
    assert hub.devices["hue:group_gl-1"].capabilities["brightness"].value == 20
    lamp = hub.devices["hue:light-1"]
    assert a.build_body(lamp, "light", True) == {"on": {"on": True}}
    assert a.build_body(lamp, "brightness", 0)["dimming"]["brightness"] == 1.0
    assert a.build_body(lamp, "color_temp", 10000)["color_temperature"]["mirek"] == 153   # clamped to schema
    assert a.build_body(lamp, "color_temp", 2700)["color_temperature"]["mirek"] == 370
    xy = a.build_body(lamp, "color", "#ff0000")["color"]["xy"]
    assert abs(xy["x"] - 0.7006) < 0.01


def test_mqtt_json_light():
    hub = Hub()
    m = mqtt.MqttAdapter(hub, {})
    m.handle_message("homeassistant/light/0xbulb/light/config", json.dumps({
        "schema": "json", "state_topic": "zigbee2mqtt/Desk", "command_topic": "zigbee2mqtt/Desk/set",
        "brightness": True, "brightness_scale": 254, "supported_color_modes": ["xy", "color_temp"],
        "min_mireds": 153, "max_mireds": 454, "unique_id": "0xbulb_light",
        "device": {"identifiers": ["zigbee2mqtt_0xbulb"], "name": "Desk Lamp", "suggested_area": "Office"}}).encode())
    did = "mqtt:zigbee2mqtt_0xbulb"
    d = hub.devices[did]
    assert set(d.capabilities) == {"light", "brightness", "color_temp", "color"} and d.attributes["kind"] == "light"
    assert d.attributes["ct_min"] == 2203 and d.room == "Office"
    m.handle_message("zigbee2mqtt/Desk", b'{"state":"ON","brightness":127,"color_mode":"color_temp","color_temp":370}')
    v = {k: c.value for k, c in d.capabilities.items()}
    assert v["light"] is True and v["brightness"] == 50 and v["color_temp"] == 2703
    m.handle_message("zigbee2mqtt/Desk", b'{"state":"OFF","color_mode":"xy","color":{"x":0.7006,"y":0.2993}}')
    assert d.capabilities["light"].value is False and d.capabilities["color"].value == "#ff0000"
    ent = m.by_cap[(did, "brightness")]
    topic, payload = ent.command_for("brightness", 100)
    assert topic == "zigbee2mqtt/Desk/set" and json.loads(payload) == {"state": "ON", "brightness": 254}
    assert json.loads(ent.command_for("light", False)[1]) == {"state": "OFF"}
    assert json.loads(ent.command_for("color_temp", 2700)[1])["color_temp"] == 370
    assert set(json.loads(ent.command_for("color", "#00ff00")[1])["color"]) == {"x", "y"}


def test_mqtt_default_schema_light():
    hub = Hub()
    m = mqtt.MqttAdapter(hub, {})
    m.handle_message("homeassistant/light/strip/config", json.dumps({
        "name": "LED Strip", "state_topic": "strip/state", "command_topic": "strip/set",
        "brightness_state_topic": "strip/bri", "brightness_command_topic": "strip/bri/set",
        "rgb_state_topic": "strip/rgb", "rgb_command_topic": "strip/rgb/set", "unique_id": "strip1"}).encode())
    did = "mqtt:strip1"
    m.handle_message("strip/state", b"ON")
    m.handle_message("strip/bri", b"64")
    m.handle_message("strip/rgb", b"255,128,0")
    v = {k: c.value for k, c in hub.devices[did].capabilities.items()}
    assert v == {"light": True, "brightness": 25, "color": "#ff8000"}
    assert m.by_cap[(did, "color")].command_for("color", "#0080ff") == ("strip/rgb/set", "0,128,255")
    assert m.by_cap[(did, "brightness")].command_for("brightness", 100) == ("strip/bri/set", "255")


def test_demo_lights_group():
    hub = Hub()
    a = demo.DemoAdapter(hub, {})

    async def run():
        await a.start()
        g = hub.devices["demo:lr_group"]
        assert g.attributes["kind"] == "group" and len(g.attributes["members"]) == 2
        await a.command(g, "light", False)
        assert all(hub.devices[x].capabilities["light"].value is False for x in g.attributes["members"])
        assert g.capabilities["light"].value is False
        await a.command(hub.devices["demo:lr_lamp"], "color", "#00ff00")
        assert hub.devices["demo:lr_lamp"].capabilities["color"].value == "#00ff00"
        assert g.capabilities["light"].value is True
        await a.stop()
    asyncio.run(run())
