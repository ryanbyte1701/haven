import asyncio
import json
import types

import pytest

from app.adapters import awair, dyson, vesync
from app.hub import Hub
from app.models import Device


# ---------------- Awair ----------------
LOCAL = {"timestamp": "2026-11-16T17:00:00.000Z", "score": 86, "dew_point": 8.2, "temp": 22.6, "humid": 41.5,
         "abs_humid": 8.3, "co2": 612, "co2_est": 450, "voc": 245, "voc_baseline": 2370, "pm25": 3, "pm10_est": 4}
CLOUD = {"data": [{"timestamp": "2026-11-16T17:00:00.000Z", "score": 72.0, "sensors": [
    {"comp": "temp", "value": 23.1}, {"comp": "humid", "value": 30.2}, {"comp": "co2", "value": 980},
    {"comp": "voc", "value": 410}, {"comp": "pm25", "value": 12}, {"comp": "lux", "value": 120}]}]}


def test_awair_local_parse():
    v = awair.parse_local(LOCAL)
    assert v == {"air_score": 86.0, "temperature": 22.6, "humidity": 41.5, "co2": 612.0, "voc": 245.0,
                 "pm25": 3.0, "pm10": 4.0, "status_text": "Good"}


def test_awair_cloud_parse_and_bands():
    v = awair.parse_cloud(CLOUD)
    assert v["air_score"] == 72.0 and v["co2"] == 980.0 and v["status_text"] == "Fair" and "lux" not in v
    assert awair.score_status(59) == "Poor" and awair.score_status(80) == "Good" and awair.score_status(None) is None
    assert awair.parse_cloud({"data": []}) == {}


def test_awair_local_loop_registers_device(monkeypatch):
    hub = Hub()
    a = awair.AwairAdapter(hub, {"hosts": ["192.168.1.70"], "poll_seconds": 10})

    class Resp:
        def __init__(self, data): self.data = data
        def raise_for_status(self): return self
        def json(self): return self.data

    async def fake_get(url, **kw):
        return Resp(LOCAL if url.endswith("/air-data/latest") else {"device_uuid": "awair-omni_123"})

    a.http.get = fake_get

    async def run():
        t = asyncio.create_task(a._local_loop())
        await asyncio.sleep(0.05)
        t.cancel()
    asyncio.run(run())
    d = hub.devices["awair:local_192.168.1.70"]
    assert d.attributes["kind"] == "air_monitor" and d.model == "Awair Omni"
    assert d.capabilities["air_score"].value == 86.0 and d.capabilities["voc"].unit == "ppb"
    assert d.capabilities["air_score"].label == "Air score"


def test_awair_registers_settings():
    from app.adapters import ADAPTERS
    from app.settings import SCHEMA_BY_ID
    assert ADAPTERS["awair"] is awair.AwairAdapter and ADAPTERS["dyson"] is dyson.DysonAdapter
    assert ADAPTERS["vesync"] is vesync.VeSyncAdapter
    assert SCHEMA_BY_ID["dyson"]["login"] is True and SCHEMA_BY_ID["awair"]["fields"][0]["key"] == "hosts"


# ---------------- Dyson ----------------
class FakeDyson:
    def __init__(self, **kw):
        self.is_on, self.speed, self.auto_mode, self.night_mode, self.oscillation = True, 4, False, False, True
        self.temperature, self.humidity = 295.65, 38          # Kelvin, %
        self.particulate_matter_2_5, self.particulate_matter_10 = 7, 9
        self.volatile_organic_compounds, self.nitrogen_dioxide = -1, 0.2   # -1 = sensor off
        self.hepa_filter_life, self.error_code, self.calls = 81, "NONE", []
        self.__dict__.update(kw)

    def __getattr__(self, name):                # turn_on, set_speed, enable_auto_mode, ...
        if name.startswith(("turn_", "set_", "enable_", "disable_")):
            return lambda *a: self.calls.append((name, a))
        raise AttributeError(name)


def test_dyson_read_state():
    v = dyson.read_state(FakeDyson())
    assert v["switch"] is True and v["fan_speed"] == 4 and v["mode"] == "manual" and v["oscillation"] is True
    assert v["temperature"] == 22.5 and v["humidity"] == 38 and v["pm25"] == 7.0 and v["filter_life"] == 81
    assert "voc" not in v and v["no2"] == 0.2 and v["status_text"] == "Running"
    assert dyson.read_state(FakeDyson(auto_mode=True, speed="AUTO"))["mode"] == "auto"
    assert dyson.read_state(FakeDyson(night_mode=True))["mode"] == "night"
    assert dyson.read_state(FakeDyson(error_code="FLTR"))["status_text"] == "Error FLTR"


def test_dyson_hosts_and_caps():
    assert dyson.parse_hosts(["tp7-us-abc1234a=192.168.1.60", "junk", " X = "]) == {"TP7-US-ABC1234A": "192.168.1.60"}
    c = dyson.purifier_caps()
    assert c["fan_speed"].min == 1 and c["fan_speed"].max == 10 and c["fan_speed"].writable
    assert c["mode"].options == ["auto", "manual", "night"] and c["oscillation"].writable


def test_dyson_commands():
    hub = Hub()
    a = dyson.DysonAdapter(hub, {})
    dev = Device(id="dyson:S1", name="Bedroom", source="dyson", capabilities=dyson.purifier_caps())
    hub.upsert_device(dev)
    fake = FakeDyson(night_mode=True)
    a.devs["dyson:S1"] = fake
    for capname, val in (("switch", False), ("fan_speed", 14), ("oscillation", False), ("mode", "auto"), ("mode", "manual")):
        asyncio.run(a.command(dev, capname, val))
    assert fake.calls == [("turn_off", ()), ("set_speed", (10,)), ("disable_oscillation", ()), ("enable_auto_mode", ()),
                          ("disable_auto_mode", ()), ("disable_night_mode", ())]
    with pytest.raises(ValueError):
        asyncio.run(a.command(dev, "mode", "turbo"))


def test_dyson_signin_two_steps(tmp_path, monkeypatch):
    sent = {}

    class Info:
        def __init__(self, serial): self.serial, self.name, self.product_type, self.credential = serial, "Living room", "438", "LOCALKEY"
        def get_device_type(self): return "438"

    class FakeAccount:
        def login_email_otp(self, email, region):
            sent["otp_to"] = (email, region)
            def verify(code, password):
                assert code == "123456" and password == "pw"
                return {"token": "t"}
            return verify
        def devices(self): return [Info("TP7-US-AAA0001A")]

    fake_cloud = types.SimpleNamespace(DysonAccount=FakeAccount, DysonAccountCN=FakeAccount)
    monkeypatch.setitem(__import__("sys").modules, "libdyson.cloud", fake_cloud)
    r1 = asyncio.run(dyson.DysonAdapter.signin({"email": "me@x.com", "password": "pw", "region": "GB"}, tmp_path))
    assert r1["need_code"] and sent["otp_to"] == ("me@x.com", "GB")
    r2 = asyncio.run(dyson.DysonAdapter.signin({"email": "me@x.com", "password": "pw", "region": "GB", "code": "123456"}, tmp_path))
    assert r2 == {"ok": True, "devices": 1}
    saved = json.loads((tmp_path / "dyson.json").read_text())
    assert saved["devices"][0]["credential"] == "LOCALKEY" and "pw" not in json.dumps(saved)
    assert (tmp_path / "dyson.json").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):    # a code without a pending challenge
        asyncio.run(dyson.DysonAdapter.signin({"email": "other@x.com", "password": "pw", "code": "1"}, tmp_path))


def test_dyson_start_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(dyson, "DATA_DIR", tmp_path)
    a = dyson.DysonAdapter(Hub(), {})
    asyncio.run(a.start())
    assert a.status.startswith("needs sign-in")


# ---------------- VeSync / Levoit ----------------
class En:
    def __init__(self, v): self.value = v


class FakePurifier:
    def __init__(self, **st):
        base = dict(device_status=En("on"), connection_status=En("online"), fan_level=2, mode=En("manual"),
                    pm25=14, pm10=None, voc=None, co2=None, humidity=None, temperature=None, filter_life=73)
        base.update(st)
        self.state = types.SimpleNamespace(**base)
        self.cid, self.device_name, self.device_type = "cid1", "Office purifier", "Core400S"
        self.fan_levels, self.modes = [1, 2, 3, 4], [En("auto"), En("sleep"), En("manual")]
        self.supports_air_quality, self.air_quality_string, self.calls = True, "very_good", []

    async def turn_on(self): self.calls.append("on"); return True
    async def turn_off(self): self.calls.append("off"); return True
    async def set_fan_speed(self, s): self.calls.append(("speed", s)); return True
    async def set_mode(self, m): self.calls.append(("mode", m)); return m != "turbo"


def test_vesync_read_state_and_caps():
    p = FakePurifier()
    v = vesync.read_state(p)
    assert v == {"switch": True, "fan_speed": "2", "mode": "manual", "pm25": 14, "filter_life": 73,
                 "status_text": "Very Good", "_online": True}
    off = vesync.read_state(FakePurifier(device_status=En("off"), connection_status=En("offline"), temperature=71))
    assert off["switch"] is False and off["_online"] is False and off["temperature"] == 21.7
    c = vesync.purifier_caps(p)
    assert c["fan_speed"].options == ["1", "2", "3", "4"] and c["mode"].options == ["auto", "sleep", "manual"]
    assert c["switch"].writable and not c["filter_life"].writable


def test_vesync_commands():
    hub = Hub()
    a = vesync.VeSyncAdapter(hub, {"username": "u", "password": "p"})
    p = FakePurifier()
    dev = Device(id="vesync:cid1", name="Office purifier", source="vesync", capabilities=vesync.purifier_caps(p))
    hub.upsert_device(dev)
    a.devs[dev.id] = p
    asyncio.run(a.command(dev, "fan_speed", "3"))
    asyncio.run(a.command(dev, "mode", "sleep"))
    asyncio.run(a.command(dev, "switch", False))
    assert p.calls == [("speed", 3), ("mode", "sleep"), "off"]
    assert hub.devices[dev.id].capabilities["fan_speed"].value == "3"
    with pytest.raises(RuntimeError):
        asyncio.run(a.command(dev, "mode", "turbo"))


def test_vesync_needs_credentials():
    a = vesync.VeSyncAdapter(Hub(), {})
    asyncio.run(a.start())
    assert "email" in a.status
