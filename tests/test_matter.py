"""Matter adapter against a fake Matter server (the WebSocket API of matterjs-server / python-matter-server)."""
import asyncio
import json
import socket

import pytest
from aiohttp import web

from app.adapters import matter as M
from app.adapters.matter import MatterAdapter, map_endpoint, normalize_code, normalize_url
from app.hub import Hub

SERVER_INFO = {"fabric_id": 1, "compressed_fabric_id": 1234567890123, "schema_version": 15,
               "min_supported_schema_version": 11, "sdk_version": "matter-server/1.6.0 (matter.js/0.18.1)",
               "wifi_credentials_set": False, "thread_credentials_set": False, "bluetooth_enabled": False}

ROOT = {"0/29/0": [{"0": 22, "1": 1}]}


def node(nid, attrs, available=True, **extra):
    return {"node_id": nid, "date_commissioned": "2026-11-01T10:00:00", "last_interview": "2026-11-01T10:00:00",
            "interview_version": 6, "available": available, "is_bridge": False, "attributes": {**ROOT, **attrs},
            "attribute_subscriptions": [], **extra}


LIGHT = node(1, {   # extended color light (python-matter-server style: no endpoint_parents)
    "0/29/3": [1], "0/40/1": "Nanoleaf", "0/40/3": "Essentials A19", "0/40/5": "", "0/40/10": "3.5.10",
    "1/29/0": [{"0": 269, "1": 1}], "1/6/0": True, "1/8/0": 127,
    "1/768/65532": 0x1F, "1/768/8": 2, "1/768/7": 370, "1/768/3": 20000, "1/768/4": 20000,
    "1/768/16395": 153, "1/768/16396": 500})
CONTACT = node(2, {  # contact sensor, battery on the root endpoint
    "0/29/3": [1], "0/40/1": "Eve Systems", "0/40/3": "Eve Door & Window", "0/40/5": "Front Door",
    "0/47/12": 170, "0/47/14": 0, "1/29/0": [{"0": 21, "1": 1}], "1/69/0": True})
STRIP = node(3, {    # two-outlet strip; outlet 1 has a power/energy meter child endpoint (matterjs style)
    "0/29/3": [1, 2, 3], "0/40/1": "Acme", "0/40/3": "Smart Power Strip", "0/40/5": "Desk Strip",
    "1/29/0": [{"0": 266, "1": 1}], "1/6/0": True, "1/29/3": [3],
    "2/29/0": [{"0": 266, "1": 1}], "2/6/0": False,
    "3/29/0": [{"0": 0x0510, "1": 1}], "3/144/8": 12500, "3/144/4": 120100, "3/145/1": {"0": 2500000, "1": 0}},
    endpoint_parents={"1": 0, "2": 0, "3": 1})
BRIDGE = node(4, {   # a bridge with one bridged lamp that is unreachable
    "0/29/3": [1, 2], "0/40/1": "Philips", "0/40/3": "Hue Bridge",
    "1/29/0": [{"0": 14, "1": 1}], "1/29/3": [2],
    "2/29/0": [{"0": 19, "1": 1}, {"0": 256, "1": 1}], "2/57/5": "Hall Lamp", "2/57/17": False, "2/6/0": False})
NEW_LEAK = node(5, {"0/29/3": [1], "0/40/1": "Aqara", "0/40/3": "Water Leak Sensor",
                    "1/29/0": [{"0": 67, "1": 1}], "1/69/0": False})


class FakeMatterServer:
    def __init__(self):
        self.nodes = [LIGHT, CONTACT, STRIP, BRIDGE]
        self.calls: list[dict] = []
        self.clients: list[web.WebSocketResponse] = []
        self.commission_error = None
        self.commission_delay = 0.0

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.append(ws)
        await ws.send_json(SERVER_INFO)
        async for msg in ws:
            m = json.loads(msg.data)
            cmd, args, mid = m["command"], m.get("args") or {}, m["message_id"]
            self.calls.append(m)
            if cmd in ("start_listening", "get_nodes"):
                await ws.send_json({"message_id": mid, "result": self.nodes})
            elif cmd == "device_command":
                await ws.send_json({"message_id": mid, "result": None})
                if args["cluster_id"] == 6:   # the device reports its new state
                    await ws.send_json({"event": "attribute_updated",
                                        "data": [args["node_id"], f"{args['endpoint_id']}/6/0", args["command_name"] == "On"]})
            elif cmd == "write_attribute":
                await ws.send_json({"message_id": mid, "result": [{"Path": {}, "Status": 0}]})
            elif cmd == "commission_with_code":
                await asyncio.sleep(self.commission_delay)
                if self.commission_error:
                    await ws.send_json({"message_id": mid, **self.commission_error})
                else:
                    self.nodes = self.nodes + [NEW_LEAK]
                    await ws.send_json({"event": "node_added", "data": NEW_LEAK})
                    await ws.send_json({"message_id": mid, "result": NEW_LEAK})
            else:
                await ws.send_json({"message_id": mid, "error_code": 9, "details": f"unknown command {cmd}"})
        return ws

    async def dashboard(self, request):
        return web.Response(text="dashboard")

    async def push(self, event, data):
        for ws in list(self.clients):
            if not ws.closed:
                await ws.send_json({"event": event, "data": data})

    async def start(self):
        app = web.Application()
        app.router.add_get("/ws", self.ws)
        app.router.add_get("/", self.dashboard)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        for ws in list(self.clients):
            await ws.close()
        await self.runner.cleanup()


async def until(cond, timeout=4.0):
    for _ in range(int(timeout / 0.02)):
        try:
            if cond():
                return True
        except (KeyError, AttributeError):
            pass
        await asyncio.sleep(0.02)
    return False


def val(hub, did, capname):
    return hub.devices[did].capabilities[capname].value


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    from app import settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    return settings_mod


# ------------------------------------------------------------------ pure helpers
def test_pairing_codes():
    assert normalize_code("3497-011-2332") == "34970112332"
    assert normalize_code(" 3497 011 2332 ") == "34970112332"
    assert normalize_code("749701123365521327694") == "749701123365521327694"
    assert normalize_code("mt:y.k90afn00ka0648g00") == "MT:Y.K90AFN00KA0648G00"
    for bad, why in (("", "enter"), ("1234", "11 or 21"), ("3497-011-2333", "typo"), ("MT:<script>", "QR"),
                     ("abc-def", "11 or 21")):
        with pytest.raises(ValueError, match=why):
            normalize_code(bad)


def test_addresses():
    assert normalize_url("") == ""
    assert normalize_url("192.168.1.20") == "ws://192.168.1.20:5580/ws"
    assert normalize_url("nas.local:5581") == "ws://nas.local:5581/ws"
    assert normalize_url("http://192.168.1.20:5580") == "ws://192.168.1.20:5580/ws"
    assert normalize_url("ws://[fd00::5]:5580/ws/") == "ws://[fd00::5]:5580/ws"
    assert normalize_url("wss://matter.example.com/ws") == "wss://matter.example.com:5580/ws"
    for bad in ("ws://a b", "ftp://x", "ws://user:pw@host/ws", "ws://h:99999/ws"):
        with pytest.raises(ValueError):
            normalize_url(bad)
    auto = M.candidate_urls("")
    assert auto[0] == "ws://127.0.0.1:5580/ws" and auto[-1] == "ws://host.docker.internal:5580/ws"


def caps_of(clusters, dts=()):
    return {n: v for n, v, _kw, _c in map_endpoint(clusters, set(dts), 1)}


def test_cluster_mapping():
    # BooleanState: TRUE = contact = closed for contact sensors; TRUE = leak for water leak detectors
    assert caps_of({69: {0: True}}, [21]) == {"contact": "closed"}
    assert caps_of({69: {0: False}}, [21]) == {"contact": "open"}
    assert caps_of({69: {0: True}}, [67]) == {"leak": True}
    # window covering: Matter 0 % = open; 100 % (10000) = closed
    wc = caps_of({258: {65532: 0x05, 14: 10000}})
    assert wc["contact"] == "closed" and wc["position"] == 0 and "action" in wc
    assert caps_of({258: {65532: 0x05, 14: 2500}})["position"] == 75
    assert caps_of({257: {0: 1}})["lock"] == "locked"
    assert caps_of({257: {0: 2, 3: 0}}) == {"lock": "unlocked", "contact": "open"}
    assert caps_of({257: {0: 0}})["lock"] == "jammed"
    th = caps_of({513: {0: 2150, 28: 4, 41: 1}})
    assert th == {"temperature": 21.5, "status_text": "Heat · heating"}
    assert caps_of({514: {0: 3, 2: 66, 3: 66}}) == {"switch": True, "fan_speed": 66}
    sm = caps_of({92: {65532: 3, 1: 0, 2: 2, 3: 0}})
    assert sm["smoke"] is False and sm["co"] is True and sm["battery_low"] is False
    aq = caps_of({1037: {0: 612.4, 8: 0}, 1066: {0: 8.25, 8: 4}, 1070: {10: 2}, 91: {0: 3}})
    assert aq == {"co2": 612.4, "pm25": 8.2, "voc_level": "Medium", "air_quality": "Moderate"}
    assert caps_of({1026: {0: 2234}, 1029: {0: 4550}, 1024: {0: 30001}, 1030: {0: 1}}) == \
        {"temperature": 22.34, "humidity": 45.5, "illuminance": 1000.0, "motion": True}
    assert caps_of({1030: {65532: 0x20, 0: 0}}) == {"presence": False}
    assert caps_of({47: {12: 101, 14: 1}}) == {"battery": 50, "battery_low": True}
    assert caps_of({144: {8: 1234567}})["power"] == 1234.6           # mW -> W
    assert caps_of({145: {1: {"0": 1500000}}})["energy_total"] == 1.5  # mWh -> kWh
    # null readings stay unknown instead of becoming 0 / False
    assert caps_of({1026: {0: None}, 69: {0: None}}, [21]) == {"temperature": None, "contact": None}


# ------------------------------------------------------------------ against the fake server
def test_import_live_updates_and_commands(settings_file):
    async def run():
        srv = FakeMatterServer()
        port = await srv.start()
        hub = Hub()
        a = MatterAdapter(hub, {"url": f"ws://127.0.0.1:{port}/ws"})
        hub.adapters["matter"] = a
        await a.start()
        try:
            assert await until(lambda: a.status.startswith("ok")), a.status
            # one device per node, one per outlet of the strip, one per bridged device
            assert set(hub.devices) == {"matter:1", "matter:2", "matter:3-1", "matter:3-2", "matter:4-2"}
            assert a.status == "ok · 5 Matter devices"

            light = hub.devices["matter:1"]
            assert light.name == "Essentials A19" and light.model == "Nanoleaf Essentials A19" and light.online
            assert light.attributes["kind"] == "light" and light.attributes["firmware"] == "3.5.10"
            assert light.attributes["ct_min"] == 2000 and light.attributes["ct_max"] == 6536
            assert val(hub, "matter:1", "light") is True and val(hub, "matter:1", "brightness") == 50
            assert val(hub, "matter:1", "color_temp") == 2703
            assert light.capabilities["color"].writable and light.capabilities["color"].value is None   # in CT mode

            door = hub.devices["matter:2"]
            assert door.name == "Front Door" and val(hub, "matter:2", "contact") == "closed"
            assert val(hub, "matter:2", "battery") == 85 and val(hub, "matter:2", "battery_low") is False

            o1, o2 = hub.devices["matter:3-1"], hub.devices["matter:3-2"]
            assert (o1.name, o2.name) == ("Desk Strip 1", "Desk Strip 2")
            assert o1.attributes["kind"] == "smart_plug" and o2.attributes.get("kind") == "smart_plug"
            assert val(hub, "matter:3-1", "switch") is True and val(hub, "matter:3-1", "power") == 12.5
            assert val(hub, "matter:3-1", "voltage") == 120.1 and val(hub, "matter:3-1", "energy_total") == 2.5
            assert val(hub, "matter:3-2", "switch") is False and "power" not in o2.capabilities

            lamp = hub.devices["matter:4-2"]
            assert lamp.name == "Hall Lamp" and lamp.model == "Philips" and lamp.online is False

            # live updates
            await srv.push("attribute_updated", [2, "1/69/0", False])
            assert await until(lambda: val(hub, "matter:2", "contact") == "open")
            await srv.push("attribute_updated", [3, "3/144/8", 60000])
            assert await until(lambda: val(hub, "matter:3-1", "power") == 60.0)
            await srv.push("attribute_updated", [1, "1/768/8", 1])        # bulb switched to XY color
            assert await until(lambda: val(hub, "matter:1", "color") == M.xy_to_hex(20000 / 65536, 20000 / 65536))
            await srv.push("attribute_updated", [4, "2/57/17", True])     # bridged lamp reachable again
            assert await until(lambda: hub.devices["matter:4-2"].online)

            # commands
            calls = lambda: [c["args"] for c in srv.calls if c["command"] == "device_command"]  # noqa: E731
            await hub.command("matter:1", "light", False)
            assert await until(lambda: len(calls()) == 1)
            assert calls()[0] == {"node_id": 1, "endpoint_id": 1, "cluster_id": 6, "command_name": "Off", "payload": {}}
            assert await until(lambda: val(hub, "matter:1", "light") is False)
            await hub.command("matter:1", "brightness", 100)
            await hub.command("matter:1", "color_temp", 4000)
            await hub.command("matter:1", "color", "#ff0000")
            assert calls()[1]["command_name"] == "MoveToLevelWithOnOff"
            assert calls()[1]["payload"] == {"level": 254, "transitionTime": 0, "optionsMask": 0, "optionsOverride": 0}
            assert calls()[2]["cluster_id"] == 768 and calls()[2]["payload"]["colorTemperatureMireds"] == 250
            assert calls()[3]["command_name"] == "MoveToColor" and calls()[3]["payload"]["colorX"] > 40000
            await hub.command("matter:3-2", "switch", True)
            assert calls()[4] == {"node_id": 3, "endpoint_id": 2, "cluster_id": 6, "command_name": "On", "payload": {}}
            assert await until(lambda: val(hub, "matter:3-2", "switch") is True)
            assert val(hub, "matter:3-1", "switch") is True                 # the other outlet is untouched
            with pytest.raises(ValueError):
                await hub.command("matter:2", "contact", "open")            # sensors aren't controllable

            # node goes offline / comes back / is removed
            await srv.push("node_updated", {**CONTACT, "available": False})
            assert await until(lambda: hub.devices["matter:2"].online is False)
            await srv.push("node_updated", CONTACT)
            assert await until(lambda: hub.devices["matter:2"].online)
            await srv.push("node_removed", 4)
            assert await until(lambda: "matter:4-2" not in hub.devices)

            # refresh button
            msg = await a.action("refresh")
            assert "Read 5 Matter devices" in msg and "matter:4-2" in hub.devices   # the fake still lists node 4

            # server goes away: every device shows offline, the status says why
            await srv.stop()
            assert await until(lambda: not any(d.online for d in hub.devices.values()))
            assert await until(lambda: a.status.startswith("error"))
        finally:
            await a.stop()
            await srv.stop()
    asyncio.run(run())


def test_add_device_with_pairing_code(settings_file):
    async def run():
        srv = FakeMatterServer()
        port = await srv.start()
        hub = Hub()
        a = MatterAdapter(hub, {"url": f"127.0.0.1:{port}"})       # bare host:port is accepted
        hub.adapters["matter"] = a
        await a.start()
        try:
            assert await until(lambda: a.status.startswith("ok")), a.status
            # nothing saved yet / a typo: refused before anything is sent
            with pytest.raises(RuntimeError, match="pairing code first"):
                await a.action("add_device")
            settings_file.save_section("matter", True, {"pairing_code": "3497-011-2333"})
            with pytest.raises(RuntimeError, match="typo"):
                await a.action("add_device")
            assert not [c for c in srv.calls if c["command"] == "commission_with_code"]

            # the code from Settings (saved after the adapter started) is normalised and sent network-only
            settings_file.save_section("matter", True, {"pairing_code": "3497-011-2332"})
            msg = await a.action("add_device")
            sent = [c["args"] for c in srv.calls if c["command"] == "commission_with_code"]
            assert sent == [{"code": "34970112332", "network_only": True}]
            assert msg.startswith("Added Water Leak Sensor")
            assert val(hub, "matter:5", "leak") is False
            assert settings_file.load_saved()["matter"]["pairing_code"] == ""     # cleared after success
            assert a.status == "ok · 6 Matter devices · added Water Leak Sensor"

            # a failure is explained in plain words
            srv.commission_error = {"error_code": 1, "details": "Commission failed", "reason": "not-discovered"}
            settings_file.save_section("matter", True, {"pairing_code": "MT:Y.K90AFN00KA0648G00"})
            with pytest.raises(RuntimeError, match="no device in pairing mode"):
                await a.action("add_device")
            assert [c["args"]["code"] for c in srv.calls if c["command"] == "commission_with_code"][-1] == "MT:Y.K90AFN00KA0648G00"
            assert settings_file.load_saved()["matter"]["pairing_code"] == "MT:Y.K90AFN00KA0648G00"   # kept to retry
            assert "couldn't add the device" in a.status

            # slow commissioning: the button answers at once, the result lands in the status
            srv.commission_error, srv.commission_delay = None, 0.3
            srv.nodes = [n for n in srv.nodes if n["node_id"] != 5]
            hub.remove_device("matter:5")
            a._drop_node(5)
            monkey_wait = M.ACTION_WAIT
            M.ACTION_WAIT = 0.05
            try:
                settings_file.save_section("matter", True, {"pairing_code": "34970112332"})
                msg = await a.action("add_device")
                assert msg.startswith("Adding the device")
                with pytest.raises(RuntimeError, match="Already adding"):
                    await a.action("add_device")
                assert await until(lambda: "matter:5" in hub.devices and "added Water Leak Sensor" in a.status)
            finally:
                M.ACTION_WAIT = monkey_wait

            with pytest.raises(ValueError):
                await a.action("nope")
        finally:
            await a.stop()
            await srv.stop()
    asyncio.run(run())


def test_bad_addresses():
    async def run():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
        s.close()
        hub = Hub()
        a = MatterAdapter(hub, {"url": f"ws://127.0.0.1:{closed_port}/ws"})
        await a.start()
        assert await until(lambda: a.status.startswith("error"))
        assert a.status == f"error: no Matter server at 127.0.0.1:{closed_port}: is the matter-server container running?"
        with pytest.raises(RuntimeError, match="Not connected"):
            await a.action("refresh")
        await a.stop()

        # something answers, but it's not a Matter server's WebSocket
        srv = FakeMatterServer()
        port = await srv.start()
        b = MatterAdapter(hub, {"url": f"http://127.0.0.1:{port}/"})
        assert b.url == f"ws://127.0.0.1:{port}/ws"
        c = MatterAdapter(hub, {"url": f"ws://127.0.0.1:{port}/nope"})
        await c.start()
        assert await until(lambda: c.status.startswith("error"))
        assert "isn't a Matter server" in c.status
        await c.stop()
        await srv.stop()

        d = MatterAdapter(hub, {"url": "ws://bad host/ws"})
        await d.start()
        assert d.status.startswith("error: the Matter server address should look like")
    asyncio.run(run())


def test_settings_section_registered():
    from app.adapters import ADAPTERS, PLUGIN_SETTINGS
    from app import settings as settings_mod
    assert ADAPTERS["matter"] is MatterAdapter
    sec = next(s for s in PLUGIN_SETTINGS if s["id"] == "matter")
    assert {x["id"] for x in sec["actions"]} == {"add_device", "refresh"}
    assert "matter" in settings_mod.SCHEMA_BY_ID
    assert {f["key"] for f in sec["fields"]} >= {"pairing_code", "url", "bluetooth", "lock_pin"}
