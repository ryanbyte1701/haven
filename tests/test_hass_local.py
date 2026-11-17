"""HACS edition: the in-process Home Assistant device source (app/adapters/hass_local.py), against a fake `hass`."""
import asyncio
import datetime as dt
import os
import threading
import subprocess
import sys
from pathlib import Path

import pytest

from app import edition
from app.adapters import ADAPTERS
from app.adapters import hass_local
from app.hub import Hub

import hacs_fakes as fk

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fake_ha(monkeypatch):
    saved = {k: v for k, v in sys.modules.items() if k == "homeassistant" or k.startswith("homeassistant.")}
    for k in saved:
        monkeypatch.delitem(sys.modules, k)
    fk.install()
    yield
    for k in [k for k in sys.modules if k == "homeassistant" or k.startswith("homeassistant.")]:
        del sys.modules[k]
    sys.modules.update(saved)


def _adapter(hass, cfg=None):
    hub = Hub()
    hub.devices.clear()
    hass_local.set_hass(hass)
    a = hass_local.HassLocalAdapter(hub, cfg or {})
    hub.adapters["hass"] = a
    return hub, a


def test_docker_edition_does_not_load_it():
    assert not edition.HACS
    assert "hass" not in ADAPTERS
    assert not edition.adapter_allowed("hass_local")
    assert edition.adapter_allowed("yolink")


def test_imports_everything_from_home_assistant(fake_ha):
    hass = fk.sample_home(fk.FakeHass())

    async def run():
        hub, a = _adapter(hass)
        await a.start()
        try:
            return hub, a
        finally:
            await a.stop()

    hub, a = asyncio.run(run())
    devs = {d.id: d for d in hub.devices.values()}
    # one card per HA device, in its HA area; YoLink and Hue devices are imported too (no "native" skipping)
    door = devs["hass:d1"]
    assert door.room == "Garage" and door.name == "Front Door Sensor" and door.model == "YoLink YS7704"
    assert door.capabilities["contact"].value == "closed"
    assert door.capabilities["battery"].value == 87                 # diagnostic battery kept
    assert "rssi" not in door.capabilities                         # other diagnostics skipped
    lamp = devs["hass:d2"]
    assert lamp.name == "Floor Lamp" and lamp.room == "Living Room"
    assert lamp.capabilities["light"].writable and lamp.capabilities["brightness"].value == 50
    assert devs["hass:lock_front"].room == "Garage"                 # entity without a device: its own card
    assert devs["hass:sensor_outside"].capabilities["temperature"].value == 25.0
    ids = set(devs)
    for skipped in ("hass:switch_hidden", "hass:sensor_disabled", "hass:sensor_text_only", "hass:automation_night"):
        assert skipped not in ids
    assert a.skipped == {} and a.status.startswith("ok")


def test_live_updates_commands_and_registry_changes(fake_ha, monkeypatch):
    monkeypatch.setattr(hass_local, "RELOAD_DELAY", 0.01)
    hass = fk.sample_home(fk.FakeHass())

    async def run():
        hub, a = _adapter(hass)
        await a.start()
        out = {}
        hass.set_state("binary_sensor.front_door", "on", {"device_class": "door"})
        await asyncio.sleep(0)
        out["door"] = hub.devices["hass:d1"].capabilities["contact"].value
        await hub.command("hass:d2", "light", False)
        await hub.command("hass:d2", "brightness", 30)
        await hub.command("hass:lock_front", "lock", "unlock")
        out["calls"] = list(hass.services.calls)
        # a new device appears in Home Assistant (registry events come in bursts: one reload)
        hass.device_registry.devices["d3"] = fk.DeviceEntry("d3", "Kitchen Plug", area_id="living")
        hass.entity_registry.entities["switch.kitchen"] = fk.EntityEntry("switch.kitchen", "d3")
        hass.set_state("switch.kitchen", "off", {})
        hass.bus.async_fire("entity_registry_updated", {"action": "create", "entity_id": "switch.kitchen"})
        hass.bus.async_fire("device_registry_updated", {"action": "create", "device_id": "d3"})
        await asyncio.sleep(0.1)
        out["kitchen"] = hub.devices.get("hass:d3")
        # an entity is removed
        del hass.entity_registry.entities["lock.front"]
        hass.remove_state("lock.front")
        await asyncio.sleep(0.1)
        out["lock_gone"] = "hass:lock_front" not in hub.devices
        await a.stop()
        out["listeners"] = sum(len(v) for v in hass.bus.listeners.values())
        return out

    out = asyncio.run(run())
    assert out["door"] == "open"
    assert out["calls"] == [
        ("light", "turn_off", {}, {"entity_id": "light.lamp"}, True),
        ("light", "turn_on", {"brightness_pct": 30}, {"entity_id": "light.lamp"}, True),
        ("lock", "unlock", {}, {"entity_id": "lock.front"}, True),
    ]
    assert out["kitchen"] is not None and out["kitchen"].room == "Living Room"
    assert out["kitchen"].capabilities["switch"].value is False
    assert out["lock_gone"]
    assert out["listeners"] == 0                                   # stop() unsubscribes


def test_haven_in_its_own_thread(fake_ha):
    """The HACS wrapper runs Haven's loop in its own thread: every use of `hass` must happen in hass.loop."""
    hass = fk.sample_home(fk.FakeHass())
    hloop = asyncio.new_event_loop()
    ha_thread = threading.Thread(target=hloop.run_forever, daemon=True)
    ha_thread.start()
    hass.loop = hloop
    seen = []
    real_call = hass.services.async_call

    async def call(*a, **k):
        seen.append(threading.current_thread() is ha_thread)
        await real_call(*a, **k)
    hass.services.async_call = call
    real_listen = hass.bus.async_listen

    def listen(*a):
        seen.append(threading.current_thread() is ha_thread)
        return real_listen(*a)
    hass.bus.async_listen = listen

    async def run():
        hub, a = _adapter(hass)
        await a.start()
        hloop.call_soon_threadsafe(hass.set_state, "light.lamp", "off", {"supported_color_modes": ["color_temp"]})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if hub.devices["hass:d2"].capabilities["light"].value is False:
                break
        await hub.command("hass:d2", "light", True)
        await a.stop()
        return hub.devices["hass:d2"].capabilities["light"].value, len(hub.devices)

    try:
        light, n = asyncio.run(run())
    finally:
        hloop.call_soon_threadsafe(hloop.stop)
        ha_thread.join(5)
    assert light is False and n >= 3
    assert seen and all(seen)
    assert hass.services.calls == [("light", "turn_on", {}, {"entity_id": "light.lamp"}, True)]


def test_filters_from_settings(fake_ha):
    hass = fk.sample_home(fk.FakeHass())

    async def run():
        hub, a = _adapter(hass, {"areas": ["Garage"], "exclude": ["lock.*"]})
        await a.start()
        await a.stop()
        return set(hub.devices)

    assert asyncio.run(run()) == {"hass:d1"}


def test_without_hass_it_says_so():
    async def run():
        hass_local.set_hass(None)
        hub = Hub()
        a = hass_local.HassLocalAdapter(hub, {})
        await a.start()
        return a.status

    assert "only runs inside Home Assistant" in asyncio.run(run())


def test_hacs_edition_registry_and_settings(tmp_path):
    """With HAVEN_EDITION=hacs only the Home Assistant source is registered and Settings lists only what applies."""
    code = (
        "from app.adapters import ADAPTERS\n"
        "from app import settings, edition\n"
        "assert edition.HACS\n"
        "print(sorted(ADAPTERS))\n"
        "print([s['id'] for s in settings.SCHEMA])\n"
        "print(sorted(f['key'] for f in settings.SCHEMA_BY_ID['history']['fields']))\n"
    )
    env = {**os.environ, "HAVEN_EDITION": "hacs", "HAVEN_DATA": str(tmp_path), "HAVEN_CONF_DIR": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    adapters, sections, history_fields = out.stdout.strip().splitlines()
    # Home Assistant's devices, plus the non-brand integrations the HACS edition ships (UPS over NUT, forecast, AQI)
    assert adapters == "['airquality', 'hass', 'nut', 'weather']"
    assert sections == "['hass', 'nut', 'airquality', 'weather', 'history']"
    assert "metrics_token" not in history_fields and "backend" in history_fields


async def _until(pred, tries=200):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def test_nut_ups_and_weather_forecast_from_home_assistant(fake_ha):
    """Home Assistant's NUT UPS becomes the same UPS device app/adapters/nut.py makes (Power page), and a weather
    entity gets its daily forecast from weather.get_forecasts (Home page weather card); both follow live changes."""
    box = [fk.FORECAST]
    hass = fk.ups_and_weather(fk.sample_home(fk.FakeHass()), box)

    async def run():
        hub, a = _adapter(hass)
        await a.start()
        out = {}
        try:
            ups = hub.devices["hass:ups1"]
            out["ups"] = {k: c.value for k, c in ups.capabilities.items()}
            out["ups_meta"] = (ups.name, ups.room, ups.model, dict(ups.attributes))
            wx = hub.devices["hass:weather_home"]
            out["forecast_ok"] = await _until(lambda: wx.attributes.get("daily"))
            wx = hub.devices["hass:weather_home"]
            out["wx"] = ({k: c.value for k, c in wx.capabilities.items()}, dict(wx.attributes))
            # the UPS goes back on line power; the weather turns to snow (the forecast is fetched again)
            hass.set_state("sensor.rack_ups_status_data", "OL CHRG", {})
            box[0] = fk.FORECAST_LATER
            hass.set_state("weather.home", "snowy", fk.WEATHER_ATTRS)
            out["refetched"] = await _until(lambda: hub.devices["hass:weather_home"].attributes["daily"][0]["icon"] == "snow")
            out["ups_after"] = {k: hub.devices["hass:ups1"].capabilities[k].value for k in ("ups_status", "charging")}
            out["wx_after"] = hub.devices["hass:weather_home"].attributes["icon"]
            # a registry reload re-creates the devices: the forecast stays
            await a.action("refresh")
            out["daily_after_reload"] = len(hub.devices["hass:weather_home"].attributes.get("daily") or [])
            out["response_calls"] = list(hass.services.response_calls)
        finally:
            await a.stop()
        return out

    out = asyncio.run(run())
    ups = out["ups"]
    assert ups["ups_status"] == "low_battery"                     # "OB DISCHRG LB"
    assert ups["battery"] == 18 and ups["runtime"] == 4.5 and ups["load"] == 37
    assert ups["input_voltage"] == 0 and ups["output_voltage"] == 120.4 and ups["charging"] is False
    assert set(ups) >= {"ups_status", "battery", "runtime", "load", "input_voltage", "output_voltage", "power"}
    name, room, model, attrs = out["ups_meta"]
    assert (name, room, model) == ("Rack UPS", "Rack", "CyberPower CP1500")
    assert attrs["nut_vars"]["ups.status"] == "OB DISCHRG LB" and attrs["nut_vars"]["battery.runtime"] == "270"
    assert out["ups_after"] == {"ups_status": "online", "charging": True}
    assert out["forecast_ok"]
    vals, wattrs = out["wx"]
    assert wattrs["kind"] == "weather" and wattrs["icon"] == "partly" and wattrs["place"] == "Home"
    assert vals["temperature"] == 18.0 and vals["status_text"] == "Partly cloudy" and vals["wind_gust"] == 12
    assert wattrs["units"] == {"wind": "mph", "rain": "in"} and wattrs["wind_dir"] == 225
    d0, d1, d2 = wattrs["daily"]
    noon = int(dt.datetime(2026, 11, 16, 12, tzinfo=dt.timezone.utc).timestamp())
    assert d0 == {"t": noon, "text": "Partly cloudy", "icon": "partly", "hi": 20.0, "lo": 10.0, "pop": 10,
                  "rain": 0.0, "uv": 4, "sunrise": None, "sunset": None}
    assert (d1["text"], d1["icon"], d1["pop"]) == ("Heavy rain", "rain", 90)
    assert (d2["text"], d2["icon"], d2["lo"]) == ("Thunderstorm", "storm", 0.0)
    assert out["refetched"] and out["wx_after"] == "snow"
    assert out["daily_after_reload"] == 1
    assert out["response_calls"][0] == ("weather", "get_forecasts", {"type": "daily"}, {"entity_id": "weather.home"}, True)
    assert hass.services.calls == []                               # nothing was controlled
