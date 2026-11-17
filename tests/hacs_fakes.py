"""A small stand-in for Home Assistant's `hass` (states, registries, event bus, services, auth) for the HACS
edition's tests (tests/test_hass_local.py, tests/test_hacs_build.py). Not a test module itself.

install() puts fake homeassistant.helpers.{area,device,entity}_registry modules in sys.modules (the real
package isn't installed in Haven's CI); each one's async_get(hass) returns the registry kept on the FakeHass.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EntityCategory(StrEnum):         # like homeassistant.const.EntityCategory
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class Disabler(StrEnum):
    USER = "user"
    INTEGRATION = "integration"


@dataclass
class State:
    entity_id: str
    state: str
    attributes: dict = field(default_factory=dict)


@dataclass
class Area:
    id: str
    name: str


@dataclass
class DeviceEntry:
    id: str
    name: str | None = None
    name_by_user: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    area_id: str | None = None
    config_entries: set = field(default_factory=set)
    identifiers: set = field(default_factory=set)
    disabled_by: Any = None


@dataclass
class EntityEntry:
    entity_id: str
    device_id: str | None = None
    area_id: str | None = None
    disabled_by: Any = None
    hidden_by: Any = None
    entity_category: Any = None
    platform: str = "demo"
    config_entry_id: str | None = None
    unique_id: str | None = None
    translation_key: str | None = None


@dataclass
class ConfigEntry:
    entry_id: str
    domain: str


class Event:
    def __init__(self, event_type: str, data: dict) -> None:
        self.event_type, self.data = event_type, data


class Bus:
    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}

    def async_listen(self, event_type, listener):
        assert getattr(listener, "_hass_callback", False), "listeners must run in the event loop (@callback)"
        self.listeners.setdefault(event_type, []).append(listener)

        def unsub():
            self.listeners[event_type].remove(listener)
        return unsub

    def async_fire(self, event_type, data):
        for fn in list(self.listeners.get(event_type, [])):
            fn(Event(event_type, data))


class States:
    def __init__(self) -> None:
        self.items: dict[str, State] = {}

    def async_all(self, domain_filter=None):
        domains = {domain_filter} if isinstance(domain_filter, str) else set(domain_filter or [])
        return [s for s in self.items.values() if not domains or s.entity_id.split(".")[0] in domains]


class Services:
    """Service calls are recorded in `calls`, except those asking for a response (weather.get_forecasts), which
    are recorded in `response_calls` and answered by responders[(domain, service)](service_data, target)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.response_calls: list[tuple] = []
        self.responders: dict[tuple[str, str], Any] = {}

    async def async_call(self, domain, service, service_data=None, blocking=False, context=None, target=None,
                         return_response=False, **kw):
        if return_response:
            self.response_calls.append((domain, service, dict(service_data or {}), target, blocking))
            return self.responders[(domain, service)](dict(service_data or {}), target)
        self.calls.append((domain, service, dict(service_data or {}), target, blocking))


@dataclass
class User:
    name: str
    is_admin: bool = False
    is_active: bool = True
    system_generated: bool = False


@dataclass
class RefreshToken:
    id: str
    user: User


class Auth:
    """Access tokens are "at-<refresh id>"; expired ones are "old-<refresh id>"."""

    def __init__(self) -> None:
        self.refresh: dict[str, RefreshToken] = {}

    def add(self, rid: str, user: User) -> str:
        self.refresh[rid] = RefreshToken(rid, user)
        return f"at-{rid}"

    def async_validate_access_token(self, token):
        return self.refresh.get(token[3:]) if token.startswith("at-") else None

    def async_get_refresh_token(self, rid):
        return self.refresh.get(rid)


class AreaRegistry:
    def __init__(self) -> None:
        self.areas: dict[str, Area] = {}

    def async_list_areas(self):
        return list(self.areas.values())


class DeviceRegistry:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceEntry] = {}


class EntityRegistry:
    def __init__(self) -> None:
        self.entities: dict[str, EntityEntry] = {}


class ConfigEntries:
    def __init__(self) -> None:
        self.entries: list[ConfigEntry] = []

    def async_entries(self, domain=None):
        return [e for e in self.entries if domain is None or e.domain == domain]


class FakeHass:
    def __init__(self) -> None:
        self.loop = None                 # Home Assistant's event loop; None = the caller's (same thread)
        self.states = States()
        self.bus = Bus()
        self.services = Services()
        self.auth = Auth()
        self.config_entries = ConfigEntries()
        self.area_registry = AreaRegistry()
        self.device_registry = DeviceRegistry()
        self.entity_registry = EntityRegistry()

    def set_state(self, entity_id: str, state: str, attributes: dict | None = None, fire: bool = True) -> None:
        old = self.states.items.get(entity_id)
        new = State(entity_id, state, dict(attributes or {}))
        self.states.items[entity_id] = new
        if fire:
            self.bus.async_fire("state_changed", {"entity_id": entity_id, "old_state": old, "new_state": new})

    def remove_state(self, entity_id: str) -> None:
        old = self.states.items.pop(entity_id, None)
        self.bus.async_fire("state_changed", {"entity_id": entity_id, "old_state": old, "new_state": None})


def install() -> None:
    """Fake homeassistant.helpers.*_registry modules (idempotent)."""
    if "homeassistant.helpers.area_registry" in sys.modules and getattr(sys.modules["homeassistant"], "_haven_fake", False):
        return
    ha = types.ModuleType("homeassistant")
    ha._haven_fake = True
    helpers = types.ModuleType("homeassistant.helpers")
    mods = {}
    for name, attr in (("area_registry", "area_registry"), ("device_registry", "device_registry"),
                       ("entity_registry", "entity_registry")):
        m = types.ModuleType(f"homeassistant.helpers.{name}")
        m.async_get = (lambda a: (lambda hass: getattr(hass, a)))(attr)
        setattr(helpers, name, m)
        mods[name] = m
    ha.helpers = helpers
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.helpers"] = helpers
    for name, m in mods.items():
        sys.modules[f"homeassistant.helpers.{name}"] = m


def sample_home(hass: FakeHass) -> FakeHass:
    """Two areas, a YoLink door sensor (a brand the Docker edition would skip), a lamp, a lock without a device,
    a hidden switch, a disabled sensor, a diagnostic RSSI (skipped) and a diagnostic battery (kept)."""
    hass.area_registry.areas = {"living": Area("living", "Living Room"), "garage": Area("garage", "Garage")}
    hass.config_entries.entries = [ConfigEntry("e-yolink", "yolink"), ConfigEntry("e-hue", "hue")]
    hass.device_registry.devices = {
        "d1": DeviceEntry("d1", "Front Door Sensor", manufacturer="YoLink", model="YS7704", area_id="garage",
                          config_entries={"e-yolink"}, identifiers={("yolink", "abc")}),
        "d2": DeviceEntry("d2", "Lamp", name_by_user="Floor Lamp", area_id="living", config_entries={"e-hue"},
                          identifiers={("hue", "lamp1")}),
    }
    E = EntityEntry
    hass.entity_registry.entities = {e.entity_id: e for e in [
        E("binary_sensor.front_door", "d1", platform="yolink"),
        E("sensor.front_door_battery", "d1", entity_category=EntityCategory.DIAGNOSTIC, platform="yolink"),
        E("sensor.front_door_rssi", "d1", entity_category=EntityCategory.DIAGNOSTIC, platform="yolink"),
        E("light.lamp", "d2", platform="hue"),
        E("switch.hidden", hidden_by=Disabler.USER),
        E("sensor.disabled", disabled_by=Disabler.INTEGRATION),
        E("lock.front", area_id="garage"),
    ]}
    for eid, st, attrs in [
        ("binary_sensor.front_door", "off", {"device_class": "door", "friendly_name": "Front Door"}),
        ("sensor.front_door_battery", "87", {"device_class": "battery", "unit_of_measurement": "%"}),
        ("sensor.front_door_rssi", "-60", {"device_class": "signal_strength"}),
        ("light.lamp", "on", {"brightness": 128, "supported_color_modes": ["color_temp"], "color_temp_kelvin": 2700}),
        ("switch.hidden", "on", {}),
        ("sensor.disabled", "1", {}),
        ("lock.front", "locked", {"friendly_name": "Front Lock"}),
        ("sensor.outside", "77", {"device_class": "temperature", "unit_of_measurement": "°F"}),
        ("sensor.text_only", "hello", {}),
        ("automation.night", "on", {}),
    ]:
        hass.set_state(eid, st, attrs, fire=False)
    return hass


# A UPS from Home Assistant's NUT integration and a weather entity with a daily forecast (weather.get_forecasts).
FORECAST = [
    {"datetime": "2026-11-16T12:00:00+00:00", "condition": "partlycloudy", "temperature": 68, "templow": 50,
     "precipitation": 0.0, "precipitation_probability": 10, "uv_index": 4},
    {"datetime": "2026-11-17T12:00:00+00:00", "condition": "pouring", "temperature": 59, "templow": 41,
     "precipitation": 0.8, "precipitation_probability": 90},
    {"datetime": "2026-11-18T12:00:00+00:00", "condition": "lightning-rainy", "temperature": 50, "templow": 32},
]
FORECAST_LATER = [{"datetime": "2026-11-16T12:00:00+00:00", "condition": "snowy", "temperature": 32, "templow": 23}]
WEATHER_ATTRS = {"temperature": 64.4, "apparent_temperature": 62.6, "humidity": 40, "wind_speed": 5, "wind_gust_speed": 12,
                 "wind_bearing": 225, "pressure": 1015, "uv_index": 3, "temperature_unit": "°F", "wind_speed_unit": "mph",
                 "pressure_unit": "hPa", "precipitation_unit": "in", "friendly_name": "Home"}


def ups_and_weather(hass: FakeHass, forecast=None) -> FakeHass:
    """A NUT UPS (on battery, low; "Status data" found by translation key, the rest by unique id or entity id;
    runtime shown in minutes), and weather.home whose get_forecasts answer is `forecast` (a list, or a list holder)."""
    hass.area_registry.areas.setdefault("rack", Area("rack", "Rack"))
    hass.config_entries.entries.append(ConfigEntry("e-nut", "nut"))
    hass.device_registry.devices["ups1"] = DeviceEntry("ups1", "Rack UPS", manufacturer="CyberPower", model="CP1500",
                                                       area_id="rack", config_entries={"e-nut"},
                                                       identifiers={("nut", "cp1500-abc")})
    E = EntityEntry
    for e in [
        E("sensor.rack_ups_status_data", "ups1", platform="nut", unique_id="cp1500-abc_ups.status",
          translation_key="ups_status"),
        E("sensor.rack_ups_status", "ups1", platform="nut", unique_id="cp1500-abc_ups.status.display"),
        E("sensor.rack_ups_battery_charge", "ups1", platform="nut"),
        E("sensor.rack_ups_battery_runtime", "ups1", platform="nut", entity_category=EntityCategory.DIAGNOSTIC),
        E("sensor.rack_ups_load", "ups1", platform="nut", unique_id="cp1500-abc_ups.load"),
        E("sensor.rack_ups_input_voltage", "ups1", platform="nut"),
        E("sensor.rack_ups_output_voltage", "ups1", platform="nut"),
        E("sensor.rack_ups_battery_voltage", "ups1", platform="nut", entity_category=EntityCategory.DIAGNOSTIC),
        E("weather.home", platform="met"),
    ]:
        hass.entity_registry.entities[e.entity_id] = e
    for eid, st, attrs in [
        ("sensor.rack_ups_status_data", "OB DISCHRG LB", {}),
        ("sensor.rack_ups_status", "On Battery, Battery Discharging, Low Battery", {}),
        ("sensor.rack_ups_battery_charge", "18", {"device_class": "battery", "unit_of_measurement": "%"}),
        ("sensor.rack_ups_battery_runtime", "4.5", {"device_class": "duration", "unit_of_measurement": "min"}),
        ("sensor.rack_ups_load", "37", {"unit_of_measurement": "%"}),
        ("sensor.rack_ups_input_voltage", "0", {"device_class": "voltage", "unit_of_measurement": "V"}),
        ("sensor.rack_ups_output_voltage", "120.4", {"device_class": "voltage", "unit_of_measurement": "V"}),
        ("sensor.rack_ups_battery_voltage", "23.1", {"device_class": "voltage", "unit_of_measurement": "V"}),
        ("weather.home", "partlycloudy", WEATHER_ATTRS),
    ]:
        hass.set_state(eid, st, attrs, fire=False)
    box = forecast if forecast is not None else [FORECAST]
    hass.services.responders[("weather", "get_forecasts")] = lambda data, target: {
        eid: {"forecast": box[0]} for eid in (target or {}).get("entity_id", "").split(",") if eid}
    return hass
