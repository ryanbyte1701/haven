"""Review round 3: layout validation (L3), atomic overrides (L7), MQTT ghost capabilities (M6) and junk values (L8)."""
from conftest import ADMIN  # noqa: E402  (test admin session)
import json

import pytest

from app import hub as hub_mod
from app.adapters.mqtt import Entity, MqttAdapter
from app.hub import Hub
from app.models import Device


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import app.main as m
    with TestClient(m.app, headers=ADMIN) as c:
        yield c, m


def test_layout_views_with_wrong_types_do_not_500(client):
    c, m = client
    r = c.put("/api/layout", json={"views": [{"id": 1, "devices": 5}, {"id": "ok_view-1", "name": ["x"], "rooms": "a"},
                                             {"id": "<script>", "name": "Evil"}]})
    assert r.status_code == 200, r.text
    views = r.json()["views"]
    assert views[0]["id"] == "1" and views[0]["devices"] == []
    assert views[1] == {**views[1], "id": "ok_view-1", "name": "View", "rooms": []}
    assert views[2]["id"] != "<script>" and views[2]["id"].startswith("v") and views[2]["name"] == "Evil"


def test_overrides_written_atomically_and_save_errors_are_503(tmp_path, monkeypatch, client):
    h = Hub()
    h._overrides_path = tmp_path / "overrides.json"
    h.upsert_device(Device(id="x:1", name="X", source="t", capabilities={}))
    h.set_override("x:1", name="Kitchen lamp")
    assert json.loads((tmp_path / "overrides.json").read_text())["x:1"]["name"] == "Kitchen lamp"
    assert not list(tmp_path.glob("*.tmp"))

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(hub_mod, "_write_json", boom)
    with pytest.raises(hub_mod.SaveError) as e:
        h.set_override("x:1", name="Other")
    assert e.value.status_code == 503 and "No space left" in e.value.detail
    assert h.devices["x:1"].name == "Other"           # still applied until restart
    c, m = client
    m.hub.upsert_device(Device(id="cr3:dev", name="D", source="t", capabilities={}))
    r = c.patch("/api/devices/cr3:dev", json={"name": "n"})
    assert r.status_code == 503 and "could not be saved" in r.json()["detail"]     # not a bare 500
    m.hub.devices.pop("cr3:dev", None)
    r = c.put("/api/layout", json={"rooms": ["a"]})
    assert r.status_code == 503 and "could not be saved" in r.json()["detail"]


def _adapter():
    h = Hub()
    return MqttAdapter(h, {"discovery": True}), h


def test_json_light_delete_removes_every_capability():
    a, h = _adapter()
    topic = "homeassistant/light/bulb1/config"
    cfg = {"schema": "json", "name": "Bulb", "state_topic": "z2m/bulb1", "command_topic": "z2m/bulb1/set",
           "brightness": True, "supported_color_modes": ["xy", "color_temp"], "unique_id": "bulb1",
           "device": {"identifiers": ["bulb1"], "name": "Bulb"}}
    a.handle_message(topic, json.dumps(cfg).encode())
    did = a.dev_id("bulb1")
    assert {"light", "brightness", "color_temp", "color"} <= set(h.devices[did].capabilities)
    a.handle_message(topic, b"")                       # deleted in Zigbee2MQTT
    assert did not in h.devices
    assert not [k for k in a.by_cap if k[0] == did]
    assert "z2m/bulb1" not in a.by_topic


def test_brightness_scale_and_unit_validation():
    for scale in (0, 0.001, -5, "x", float("inf")):
        e = Entity("d", "light", "light_json", cfg={"brightness_scale": scale})
        assert e._bri_scale() == 255.0
        assert e.parse(json.dumps({"state": "ON", "brightness": 255}).encode())["brightness"] == 100
    e = Entity("d", "light", "light_json", cfg={"brightness_scale": 100})
    assert e.parse(b'{"brightness": 5000}')["brightness"] == 100     # clamped
    assert e.parse(b'{"brightness": -3}')["brightness"] == 0
    assert "brightness" not in (e.parse(b'{"state": "ON", "brightness": 1e400}') or {})
    topic, payload = e.command_for("brightness", 1000)
    assert json.loads(payload)["brightness"] == 100
    a, h = _adapter()
    a.handle_message("homeassistant/sensor/t1/config", json.dumps({
        "device_class": "temperature", "state_topic": "t1/state", "unit_of_measurement": {"x": 1},
        "unique_id": "t1", "device": {"identifiers": ["t1"]}}).encode())
    a.handle_message("t1/state", b"21.5")
    d = h.devices[a.dev_id("t1")]
    assert d.capabilities["temperature"].value == 21.5


def test_hub_view_ids_generated():
    v = hub_mod._clean_view({"id": "a b/c", "devices": [1, "x", {"y": 1}]})
    assert hub_mod._VIEW_ID.fullmatch(v["id"]) and v["devices"] == ["1", "x"]
    assert hub_mod._clean_view({"id": True})["id"].startswith("v")
