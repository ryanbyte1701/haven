import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.adapters import ADAPTERS, chargepoint as cpm, roborock as rr
from app.hub import Hub
from app.settings import SCHEMA_BY_ID


_real_sleep = asyncio.sleep


def _fast_sleep(*_):
    return _real_sleep(0)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------- registration ----------------
def test_registered_with_settings():
    assert ADAPTERS["chargepoint"] is cpm.ChargePointAdapter and ADAPTERS["roborock"] is rr.RoborockAdapter
    for sid in ("chargepoint", "roborock"):
        sec = SCHEMA_BY_ID[sid]
        assert sec["login"] is True and sec["login_fields"]
        assert all(f["type"] in {"text", "password", "number", "bool", "select", "list"} for f in sec["fields"])


# ---------------- ChargePoint ----------------
@pytest.mark.parametrize("raw,plugged,conn,sess,expected", [
    ("AVAILABLE", False, True, None, "available"),
    ("AVAILABLE", True, True, None, "plugged_in"),
    ("NOT_CHARGING", True, True, None, "plugged_in"),
    ("CHARGING", True, True, "in_use", "charging"),
    ("AVAILABLE", True, True, "fully_charged", "done"),
    ("FAULT", True, True, None, "fault"),
    ("CHARGING", True, False, None, "offline"),
    ("SOMETHING_NEW", False, True, None, "available"),
])
def test_charger_status_mapping(raw, plugged, conn, sess, expected):
    assert cpm.charger_status(raw, plugged, conn, sess) == expected


def test_session_values_units():
    sess = SimpleNamespace(power_kw=7.2, energy_kwh=12.3456, start_time=datetime.now(timezone.utc) - timedelta(minutes=95))
    v = cpm.session_values(sess)
    assert v["power"] == 7200 and v["energy_session"] == 12.35 and 94 <= v["charge_time"] <= 96


class FakeCPClient:
    """Mimics python_chargepoint.ChargePoint for one Home Flex charger."""
    def __init__(self, charging=False):
        self.charging = charging
        self.calls = []
        self.coulomb_token = "tok-2"

    async def get_home_chargers(self): return [1234]
    async def get_home_charger_status(self, cid):
        return SimpleNamespace(charger_id=cid, brand="CP", model="HOME FLEX", charging_status="CHARGING" if self.charging else "AVAILABLE",
                               is_plugged_in=True, is_connected=True, amperage_limit=32, possible_amperage_limits=[20, 24, 28, 32, 36, 40])
    async def get_home_charger_technical_info(self, cid):
        return SimpleNamespace(software_version="5.5.2", serial_number="SN1", stop_charge_supported=True)
    async def get_user_charging_status(self):
        return SimpleNamespace(session_id=99, stations=[SimpleNamespace(id=1234)]) if self.charging else None
    async def get_charging_session(self, sid):
        async def stop(): self.calls.append(("stop", sid)); self.charging = False
        return SimpleNamespace(power_kw=9.6, energy_kwh=3.2, charging_state="in_use", start_time=datetime.now(timezone.utc), stop=stop)
    async def set_amperage_limit(self, cid, amps): self.calls.append(("amps", cid, amps))
    async def start_charging_session(self, cid): self.calls.append(("start", cid)); self.charging = True
    async def close(self): pass


def test_chargepoint_poll_and_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(cpm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cpm.asyncio, "sleep", _fast_sleep)
    hub = Hub()
    a = cpm.ChargePointAdapter(hub, {})
    fake = FakeCPClient(charging=True)

    async def fake_connect():
        a.client, a._username = fake, "me@x.com"

    async def setup():
        # run discovery + one refresh of _main, then stop its endless loop
        a._connect = fake_connect
        task = asyncio.ensure_future(a._main())
        for _ in range(20):
            await _real_sleep(0)
        task.cancel()
    run(setup())
    dev = hub.devices["chargepoint:1234"]
    assert dev.attributes["kind"] == "ev_charger"
    v = {k: c.value for k, c in dev.capabilities.items()}
    assert v["charger_status"] == "charging" and v["charging"] is True and v["power"] == 9600 and v["energy_session"] == 3.2
    amps = dev.capabilities["amperage_limit"]
    assert (amps.min, amps.max, amps.step, amps.writable, amps.unit) == (20, 40, 4, True, "A")
    assert dev.capabilities["action"].options == ["start", "stop"]
    run(a.command(dev, "amperage_limit", 24))
    assert ("amps", 1234, 24) in a.client.calls
    run(a.command(dev, "action", "stop"))
    assert ("stop", 99) in a.client.calls and dev.capabilities["charger_status"].value == "plugged_in"
    a._persist_token()
    assert json.loads((tmp_path / "chargepoint.json").read_text())["token"] == "tok-2"


def test_chargepoint_signin_captcha(monkeypatch):
    import python_chargepoint
    from python_chargepoint.exceptions import DatadomeCaptcha

    class C:
        coulomb_token = "t"
        @classmethod
        async def create(cls, u): return cls()
        async def login_with_password(self, p): raise DatadomeCaptcha("https://captcha", "blocked")
        async def close(self): pass
    monkeypatch.setattr(python_chargepoint, "ChargePoint", C)
    with pytest.raises(ValueError, match="bot protection"):
        run(cpm.ChargePointAdapter.signin({"username": "a@b.c", "password": "x"}, "/tmp"))


# ---------------- Roborock ----------------
class Code:   # stands in for a RoborockStateCode member
    def __init__(self, name): self.name = name


@pytest.mark.parametrize("state,battery,expected", [
    ("cleaning", 80, "cleaning"), ("segment_cleaning", 80, "cleaning"), ("returning_home", 50, "returning"),
    ("charging", 60, "charging"), ("charging", 100, "docked"), ("charging_complete", 100, "docked"),
    ("paused", 70, "paused"), ("error", 70, "error"), ("idle", 90, "idle"), ("brand_new_state", 90, "idle"),
])
def test_vacuum_state_mapping(state, battery, expected):
    assert rr.vacuum_state(Code(state), battery) == expected


def test_status_and_consumables():
    st = SimpleNamespace(battery=87, state=Code("cleaning"), state_name="segment_cleaning", clean_area=23_500_000,
                         square_meter_clean_area=None, clean_time=1830, fan_speed_name="balanced")
    v = rr.status_values(st)
    assert v == {"battery": 87, "vacuum_state": "cleaning", "status_text": "Segment cleaning", "clean_area": 23.5,
                 "clean_time": 30, "fan_speed": "balanced"}
    cons = SimpleNamespace(main_brush_work_time=150 * 3600, side_brush_work_time=0, filter_work_time=200 * 3600, sensor_dirty_time=None)
    assert rr.consumable_values(cons) == {"main_brush_life": 50, "side_brush_life": 100, "filter_life": 0}


def test_roborock_signin_two_steps(tmp_path, monkeypatch):
    import roborock.web_api as web_api

    class FakeClient:
        made = []
        def __init__(self, username): self.username = username; FakeClient.made.append(self)
        async def request_code_v4(self): self.requested = True
        async def code_login_v4(self, code):
            assert code == "123456" and getattr(self, "requested", False), "must reuse the client that requested the code"
            return SimpleNamespace(as_dict=lambda: {"rriot": {"u": "x"}})
        @property
        async def base_url(self): return "https://usiot.roborock.com"
    monkeypatch.setattr(web_api, "RoborockApiClient", FakeClient)
    r1 = run(rr.RoborockAdapter.signin({"email": "me@x.com"}, tmp_path))
    assert r1["need_code"] and "me@x.com" in r1["message"]
    r2 = run(rr.RoborockAdapter.signin({"email": "me@x.com", "code": "123456"}, tmp_path))
    assert r2 == {"ok": True} and len(FakeClient.made) == 1
    saved = json.loads((tmp_path / "roborock.json").read_text())
    assert saved == {"email": "me@x.com", "user_data": {"rriot": {"u": "x"}}, "base_url": "https://usiot.roborock.com"}
    assert oct((tmp_path / "roborock.json").stat().st_mode)[-3:] == "600"


def test_roborock_commands(monkeypatch):
    from roborock.roborock_typing import RoborockCommand
    monkeypatch.setattr(rr.asyncio, "sleep", _fast_sleep)
    sent = []

    class Mode(str):
        code = 102
        @property
        def value(self): return str(self)
    async def send(cmd, params=None): sent.append((cmd, params))
    async def refresh(): pass
    status = SimpleNamespace(battery=90, state=Code("charging"), state_name="charging", clean_area=None, square_meter_clean_area=None,
                             clean_time=None, fan_speed_name="balanced", fan_speed_options=[Mode("quiet"), Mode("balanced")], refresh=refresh)
    dev = SimpleNamespace(name="S8", duid="d1", v1_properties=SimpleNamespace(status=status, command=SimpleNamespace(send=send)))
    hub = Hub()
    a = rr.RoborockAdapter(hub, {})
    from app.models import Device, cap
    hub.upsert_device(Device(id="roborock:d1", name="S8", source="roborock", capabilities={"vacuum_state": cap("vacuum_state")}))
    a.devs["roborock:d1"] = dev
    d = hub.devices["roborock:d1"]
    run(a.command(d, "action", "start"))
    assert sent[-1] == (RoborockCommand.APP_START, None)
    run(a.command(d, "fan_speed", "quiet"))
    assert sent[-1] == (RoborockCommand.SET_CUSTOM_MODE, [102])
    with pytest.raises(ValueError):
        run(a.command(d, "fan_speed", "turbo"))
