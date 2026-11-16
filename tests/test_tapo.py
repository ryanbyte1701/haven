import asyncio
from types import SimpleNamespace as NS
from app.adapters import tapo
from app.adapters.tapo import is_tapo_power, parse_hosts, read_energy, plug_caps


def fake(family, dtype, children=()):
    return NS(config=NS(connection_type=NS(device_family=NS(value=family))), device_type=NS(name=dtype),
              children=list(children), modules={})


def test_only_tapo_power_products():
    assert is_tapo_power(fake("SMART.TAPOPLUG", "Plug"))
    assert is_tapo_power(fake("SMART.TAPOPLUG", "Strip"))
    assert not is_tapo_power(fake("SMART.TAPOBULB", "Bulb"))
    assert not is_tapo_power(fake("SMART.TAPOHUB", "Hub"))
    assert not is_tapo_power(fake("SMART.IPCAMERA", "Camera"))
    assert not is_tapo_power(fake("SMART.KASAPLUG", "Plug"))          # Kasa, not Tapo
    assert not is_tapo_power(fake("SMART.TAPOSWITCH", "WallSwitch"))


def test_parse_hosts():
    assert parse_hosts("192.168.1.5, 192.168.1.6\nplug-1.lan;  ") == ["192.168.1.5", "192.168.1.6", "plug-1.lan"]
    assert parse_hosts("http://evil/x, ../etc, 10.0.0.1") == ["10.0.0.1"]


def test_energy_and_caps():
    from kasa import Module
    em = NS(current_consumption=12.345, consumption_today=0.5, consumption_this_month=float("nan"), voltage=120.2, current=None)
    d = NS(modules={Module.Energy: em})
    e = read_energy(d)
    assert e == {"power": 12.3, "energy_today": 0.5, "voltage": 120.2}
    caps = plug_caps(d, e)
    assert caps["switch"].writable and caps["power"].unit == "W" and "energy_month" not in caps
    assert read_energy(NS(modules={})) == {}


def test_settings_registered():
    from app.settings import SCHEMA_BY_ID
    assert "tapo" in SCHEMA_BY_ID and tapo.ADAPTER.source == "tapo"


def test_missing_credentials_status():
    a = tapo.TapoAdapter(hub=None, cfg={})
    asyncio.run(a.start())
    assert "email" in a.status
