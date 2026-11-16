import json

import app.adapters.mqtt as mq
from app.adapters.mqtt import MqttAdapter
from app.hub import Hub

LOCK = {"id": "front_door", "name": "Front door",
        "capabilities": {"lock": {"state_topic": "home/lock/state", "command_topic": "home/lock/set"}}}


def disc(a, topic, cfg):
    a.handle_message(topic, json.dumps(cfg).encode())


def test_discovery_cannot_hijack_config_device():
    h = Hub()
    a = MqttAdapter(h, {"devices": [LOCK]})
    disc(a, "homeassistant/lock/x/lock/config",
         {"command_topic": "evil/set", "state_topic": "evil/state", "device": {"identifiers": ["front_door"]}})
    disc(a, "homeassistant/lock/y/lock/config", {"command_topic": "evil/set", "state_topic": "evil/state",
                                                 "unique_id": "u", "device": {"identifiers": ["front_door"]}})
    ent = a.by_cap[("mqtt:front_door", "lock")]
    assert (ent.command_topic, ent.state_topic) == ("home/lock/set", "home/lock/state")
    assert "evil/state" not in a.by_topic
    assert list(h.devices["mqtt:front_door"].capabilities) == ["lock"]


def test_discovery_needs_same_unique_id_or_topic_to_replace():
    h = Hub()
    a = MqttAdapter(h, {})
    disc(a, "homeassistant/lock/d/lock/config",
         {"command_topic": "z/set", "state_topic": "z/state", "unique_id": "real", "device": {"identifiers": ["d"]}})
    # the same config topic re-published (e.g. after a broker restart) may update it
    disc(a, "homeassistant/lock/d/lock/config",
         {"command_topic": "z/set2", "state_topic": "z/state", "unique_id": "real", "device": {"identifiers": ["d"]}})
    assert a.by_cap[("mqtt:d", "lock")].command_topic == "z/set2"
    # another topic without / with a different unique_id may not repoint it
    for uid in (None, "other"):
        cfg = {"command_topic": "evil/set", "state_topic": "evil/state", "device": {"identifiers": ["d"]}}
        if uid:
            cfg["unique_id"] = uid
        disc(a, f"homeassistant/lock/{uid}/lock/config", cfg)
        assert a.by_cap[("mqtt:d", "lock")].command_topic == "z/set2"
    # light discovery cannot hijack a config device either
    b = MqttAdapter(Hub(), {"devices": [LOCK]})
    disc(b, "homeassistant/light/l/light/config", {"command_topic": "evil", "device": {"identifiers": ["front_door"]}})
    assert ("mqtt:front_door", "light") not in b.by_cap


def test_discovery_caps(monkeypatch):
    monkeypatch.setattr(mq, "MAX_DISCOVERED_DEVICES", 3)
    monkeypatch.setattr(mq, "MAX_ENTITIES_PER_DEVICE", 2)
    h = Hub()
    a = MqttAdapter(h, {})
    for i in range(6):
        disc(a, f"homeassistant/binary_sensor/n{i}/c/config",
             {"device_class": "door", "state_topic": f"s/{i}", "unique_id": f"u{i}", "device": {"identifiers": [f"n{i}"]}})
    assert len([d for d in h.devices if d.startswith("mqtt:")]) == 3
    for i in range(5):
        disc(a, f"homeassistant/sensor/n0/t{i}/config",
             {"device_class": "temperature", "state_topic": f"t/{i}", "unique_id": f"t{i}", "device": {"identifiers": ["n0"]}})
    assert len(h.devices["mqtt:n0"].capabilities) == 2


def test_discovery_strings_sanitised():
    h = Hub()
    a = MqttAdapter(h, {})
    disc(a, "homeassistant/binary_sensor/s/c/config",
         {"device_class": "door", "state_topic": "s/1", "unique_id": "u1", "name": "<img src=x onerror=alert(1)>",
          "device": {"identifiers": ["dev\x00<b>" + "x" * 200], "name": "Door\x1b[31m<script>" + "y" * 200,
                     "model": "M<o>del", "manufacturer": "Acme\n", "suggested_area": "<Hall>"}})
    did = next(d for d in h.devices if d.startswith("mqtt:"))
    d = h.devices[did]
    for s in (did, d.name, d.model, d.room):
        assert "<" not in s and ">" not in s and "\x00" not in s and "\x1b" not in s
    assert len(d.name) <= 80 and len(did) <= len("mqtt:") + 80
    assert d.room == "Hall" and d.model == "Acme Model"
    # a second entity of the same class gets a label from the discovery name: sanitised too
    disc(a, "homeassistant/binary_sensor/s/c2/config",
         {"device_class": "door", "state_topic": "s/2", "unique_id": "u2", "object_id": "back door!",
          "name": "Back <b>door</b>\x07" + "z" * 100, "device": {"identifiers": ["dev\x00<b>" + "x" * 200]}})
    extra = [c for n, c in h.devices[did].capabilities.items() if n != "contact"]
    assert extra and all(ch.isalnum() or ch == "_" for ch in extra[0].name)
    assert "<" not in extra[0].label and "\x07" not in extra[0].label and len(extra[0].label) <= 80
