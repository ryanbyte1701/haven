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
