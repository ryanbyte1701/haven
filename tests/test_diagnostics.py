"""Settings → Diagnostics: an anonymous tech dump that keeps what devices are and report."""
import json
import logging

import pytest

from conftest import ADMIN
from app.models import Device, cap


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.config as config
    import app.history as hist_mod
    import app.main as main
    import app.settings as settings_mod
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text("history:\n  backend: memory\n")
    monkeypatch.setattr(config, "CONFIG_PATH", cfgfile)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hist_mod, "DATA_DIR", tmp_path)
    main.hub.devices.clear()
    with TestClient(main.app, headers=ADMIN) as c:
        hub = main.hub
        hub.upsert_device(Device(id="yolink:d88b4c010004f2a1", name="Ryan's Front Door", source="yolink",
                                 model="YS7704-UC", room="Ryan's Office",
                                 capabilities={"contact": cap("contact"), "battery": cap("battery")}))
        hub.set_values("yolink:d88b4c010004f2a1", {"contact": "open", "battery": 75})
        hub.upsert_device(Device(id="homeassistant:abc", name="Kitchen Thermostat", source="homeassistant",
                                 room="Kitchen", capabilities={"temperature": cap("temperature"),
                                                               "status_text": cap("status_text")}))
        hub.set_values("homeassistant:abc", {"temperature": 21.5,
                                              "status_text": "Connected to 192.168.1.40 as ryan@example.com"})
        hub.set_attributes("homeassistant:abc", {"kind": "thermostat", "ha_entities": ["climate.kitchen_ryan"],
                                                  "place": "123 Main St"})
        logging.getLogger("haven.test").warning("login failed for ryan@example.com at http://192.168.1.40:8123/api?token=abc")
        yield c, main
    main.hub.devices.clear()


def test_dump_is_anonymous_but_keeps_device_facts(client):
    c, main = client
    r = c.get("/api/diagnostics")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    d = r.json()
    text = json.dumps(d)
    for secret in ("Ryan", "ryan@example.com", "192.168.1.40", "Kitchen", "Office", "d88b4c010004f2a1",
                   "climate.kitchen_ryan", "123 Main St", "token=abc"):
        assert secret not in text, secret
    devs = {x["source"]: x for x in d["devices"]}
    door, tstat = devs["yolink"], devs["homeassistant"]
    assert door["model"] == "YS7704-UC" and door["online"] is True
    assert door["capabilities"]["contact"]["value"] == "open" and door["capabilities"]["battery"]["value"] == 75
    assert door["room"].startswith("Room ") and door["name"].endswith(" 1")
    assert tstat["name"] == "Thermostat 1" and tstat["attributes"]["kind"] == "thermostat"
    assert tstat["capabilities"]["temperature"]["value"] == 21.5
    assert tstat["capabilities"]["status_text"]["value"].startswith("<text")
    assert tstat["ha_entities"][0].startswith("climate.") and "place" in tstat["attribute_keys"]
    assert d["summary"]["devices"] >= 2 and d["haven"]["edition"] in ("og", "home-assistant")
    msgs = [w["message"] for w in d["recent_warnings"] if w["logger"] == "haven.test"]
    assert msgs and "<email>" in msgs[-1] and "<host>" in msgs[-1]


def test_dump_is_admin_only(client):
    from fastapi.testclient import TestClient
    c, main = client
    assert TestClient(main.app).get("/api/diagnostics").status_code in (401, 403)


def test_download_link_works_without_signing_in_and_expires(client):
    from fastapi.testclient import TestClient
    c, main = client[0], client[1]
    r = c.post("/api/diagnostics/link")
    assert r.status_code == 200
    path = r.json()["path"]
    assert path.startswith("/api/diagnostics/file/") and path.endswith(".json")
    anon = TestClient(main.app)                                   # the phone's downloader: no sign-in
    f = anon.get(path)
    assert f.status_code == 200 and "attachment" in f.headers["content-disposition"] and f.json()["summary"]
    assert anon.get(path.rsplit("/", 2)[0] + "/wrongtoken/haven-diagnostics-1.json").status_code == 404
    assert anon.post("/api/diagnostics/link").status_code in (401, 403)   # making a link needs an admin
    tok = path.split("/")[4]
    main._diag_files[tok] = (0, b"{}")                            # expired
    assert anon.get(path).status_code == 404


def test_scrub_removes_hosts_ipv6_macs_urls_and_known_names():
    from app.diagnostics import scrub
    out = scrub("Cannot connect to host homeassistant.local:8123 and my.duckdns.org via 2001:db8::1 / fe80::1ff:fe23:4567:890a; "
                "mac AABBCCDDEEFF; GET https://x.example.com/api/webhook/abc123secret failed for Bedroom Lamp (homeassistant:d1)",
                names={"Bedroom Lamp": "Light 1", "homeassistant:d1": "dev-1234abcd"})
    for leak in ("homeassistant.local", "duckdns", "2001:db8", "fe80", "AABBCCDDEEFF", "abc123secret", "Bedroom", "d1)"):
        assert leak not in out, (leak, out)
    assert "Light 1" in out and "dev-1234abcd" in out
    assert scrub("took 12:30 and 5 seconds") == "took 12:30 and 5 seconds"      # ordinary text is left alone


def test_dump_hides_friendly_names_in_labels_and_capability_keys_and_text_readings():
    from app.diagnostics import _Anon, _cap_key, _device, _value
    anon = _Anon()
    assert _cap_key("switch", anon) == "switch" and _cap_key("brightness", anon) == "brightness"
    k = _cap_key("switch_johns_bedroom_fan", anon)
    assert k.startswith("switch_") and "john" not in k
    assert "john" not in _cap_key("zzz_johns_bedroom", anon)
    assert _value("John Smith", []).startswith("<text") and _value("on_battery", []) == "on_battery"
    d = Device(id="x:1", name="John's Fan", source="x", capabilities={"switch_johns_bedroom_fan": cap("switch_johns_bedroom_fan", label="John's Bedroom Fan")})
    text = json.dumps(_device(d, anon, 0, []))
    assert "John" not in text and "john" not in text
