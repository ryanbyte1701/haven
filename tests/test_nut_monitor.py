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
            d = hub.devices.get(a.dev_id(f"127.0.0.1_ups"))
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
