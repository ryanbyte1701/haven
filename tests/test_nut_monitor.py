"""NUT: Haven only monitors UPSes (no NUT server of its own)."""
import asyncio

from app.adapters.nut import NutAdapter, parse_status
from app.hub import Hub

VARS = {"ups.status": "OB DISCHRG", "battery.charge": "64", "battery.runtime": "1380", "ups.load": "30",
        "input.voltage": "0.0", "output.voltage": "120.0", "device.mfr": "Ubiquiti", "device.model": "UPS Tower"}


async def fake_upsd(reader, writer):
    while line := await reader.readline():
        cmd = line.decode().strip()
        if cmd == "LIST UPS":
            writer.write(b'BEGIN LIST UPS\nUPS ups "Rack UPS"\nEND LIST UPS\n')
        elif cmd == "LIST VAR ups":
            body = "".join(f'VAR ups {k} "{v}"\n' for k, v in VARS.items())
            writer.write(f"BEGIN LIST VAR ups\n{body}END LIST VAR ups\n".encode())
        elif cmd.startswith(("USERNAME", "PASSWORD")):
            writer.write(b"OK\n")
        elif cmd == "LOGOUT":
            writer.write(b"OK Goodbye\n"); await writer.drain(); break
        else:
            writer.write(b"ERR UNKNOWN-COMMAND\n")
        await writer.drain()
    writer.close()


def test_monitors_ups_and_opens_no_server():
    async def run():
        srv = await asyncio.start_server(fake_upsd, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        hub = Hub()
        a = NutAdapter(hub, {"servers": [{"host": "127.0.0.1", "port": port, "name": "Rack"}], "poll_seconds": 2,
                             "server": {"enabled": True}})          # old saved setting: ignored
        await a.start()
        for _ in range(50):
            await asyncio.sleep(0.05)
            d = hub.devices.get(a.ups_id("127.0.0.1", port, "ups"))
            if d and d.capabilities["battery"].value is not None:
                break
        await a.stop(); srv.close()
        assert not hasattr(a, "server") and not hasattr(a, "_handle")
        return d, a.status
    d, status = asyncio.run(run())
    assert status == "ok"
    assert d.capabilities["battery"].value == 64 and d.capabilities["ups_status"].value == "on_battery"
    assert d.model == "Ubiquiti UPS Tower"


def test_parse_status():
    v = parse_status(VARS)
    assert v["runtime"] == 23.0 and v["load"] == 30


def test_no_servers_status():
    a = NutAdapter(Hub(), {})
    asyncio.run(a.start())
    assert a.status == "no UPS servers configured"


MULTI = {"rack": {"ups.status": "OL", "battery.charge": "100", "device.model": "Smart-UPS"},
         "desk": {"ups.status": "OB DISCHRG", "battery.charge": "71", "device.model": "CP1500"}}


def server_for(upses):
    async def handle(reader, writer):
        while line := await reader.readline():
            cmd = line.decode().strip()
            if cmd == "LIST UPS":
                writer.write(b"BEGIN LIST UPS\n" + b"".join(f'UPS {n} "{v["device.model"]}"\n'.encode() for n, v in upses.items()) + b"END LIST UPS\n")
            elif cmd.startswith("LIST VAR "):
                n = cmd.split()[2]
                writer.write(f"BEGIN LIST VAR {n}\n".encode() + "".join(f'VAR {n} {k} "{x}"\n' for k, x in upses[n].items()).encode() + f"END LIST VAR {n}\n".encode())
            elif cmd == "LOGOUT":
                writer.write(b"OK Goodbye\n"); await writer.drain(); break
            else:
                writer.write(b"OK\n")
            await writer.drain()
        writer.close()
    return handle


def test_several_servers_and_several_ups_per_server():
    async def run():
        a1 = await asyncio.start_server(server_for({"UPS": {"ups.status": "OL", "battery.charge": "97", "device.model": "UPS Tower"}}), "127.0.0.1", 0)
        a2 = await asyncio.start_server(server_for({"UPS": {"ups.status": "OB", "battery.charge": "50", "device.model": "UPS Tower"}}), "127.0.0.1", 0)
        m = await asyncio.start_server(server_for(MULTI), "127.0.0.1", 0)
        port = lambda s: s.sockets[0].getsockname()[1]   # noqa: E731
        hub = Hub()
        a = NutAdapter(hub, {"poll_seconds": 2, "servers": [
            {"host": "127.0.0.1", "port": port(a1), "name": "Rack UPS"},
            {"host": "127.0.0.1", "port": port(a2), "name": "Garage UPS"},     # same host, same UPS name "UPS"
            {"host": "127.0.0.1", "port": port(m), "name": "NAS"}]})
        await a.start()
        for _ in range(60):
            await asyncio.sleep(0.05)
            ups = [d for d in hub.devices.values() if d.source == "nut" and d.capabilities["battery"].value is not None]
            if len(ups) == 4:
                break
        await a.stop()
        for s in (a1, a2, m):
            s.close()
        return {d.name: d.capabilities["battery"].value for d in hub.devices.values() if d.source == "nut"}
    got = asyncio.run(run())
    assert got == {"Rack UPS": 97, "Garage UPS": 50, "NAS · Smart-UPS": 100, "NAS · CP1500": 71}


def test_default_port_keeps_short_id():
    a = NutAdapter(Hub(), {})
    assert a.ups_id("192.168.1.40", 3493, "UPS") == "nut:192.168.1.40_UPS"
    assert a.ups_id("192.168.1.40", 3494, "UPS") == "nut:192.168.1.40_3494_UPS"
