"""NUT (Network UPS Tools) monitoring.

Polls one or more NUT servers, e.g. the one built into UniFi UPS Tower / UPS-2U
(UniFi Network > the UPS > Settings > NUT Server), or APC/CyberPower via a NAS, and shows each UPS's
status, charge, runtime, load and voltages. Haven only reads: it never sends commands to a UPS.
"""
from __future__ import annotations

import asyncio
import shlex
from typing import Any

from ..models import Device, cap
from .base import Adapter

class NutClient:
    def __init__(self, host: str, port: int = 3493, username: str | None = None,
                 password: str | None = None, timeout: float = 5):
        self.host, self.port, self.username, self.password, self.timeout = host, port, username, password, timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        if self.username:
            await self._cmd(f"USERNAME {self.username}")
            await self._cmd(f"PASSWORD {self.password or ''}")

    async def close(self):
        if self.writer:
            try:
                self.writer.write(b"LOGOUT\n")
                await self.writer.drain()
                self.writer.close()
            except Exception:  # noqa: BLE001
                pass
        self.reader = self.writer = None

    async def _line(self) -> str:
        line = await asyncio.wait_for(self.reader.readline(), self.timeout)
        if not line:
            raise ConnectionError("NUT server closed the connection")
        return line.decode(errors="replace").rstrip("\r\n")

    async def _cmd(self, cmd: str) -> str:
        self.writer.write(cmd.encode() + b"\n")
        await self.writer.drain()
        resp = await self._line()
        if resp.startswith("ERR"):
            raise RuntimeError(f"{cmd.split()[0]}: {resp}")
        return resp

    async def _list(self, what: str) -> list[list[str]]:
        self.writer.write(f"LIST {what}\n".encode())
        await self.writer.drain()
        first = await self._line()
        if first.startswith("ERR"):
            raise RuntimeError(f"LIST {what}: {first}")
        rows = []
        while True:
            line = await self._line()
            if line.startswith("END LIST"):
                return rows
            rows.append(shlex.split(line))

    async def list_ups(self) -> dict[str, str]:
        return {r[1]: (r[2] if len(r) > 2 else r[1]) for r in await self._list("UPS")}

    async def list_vars(self, ups: str) -> dict[str, str]:
        return {r[2]: r[3] for r in await self._list(f"VAR {ups}") if len(r) >= 4}


def parse_status(vars_: dict[str, str]) -> dict[str, Any]:
    """Turn NUT variables into capability values."""
    flags = set(vars_.get("ups.status", "").split())
    out: dict[str, Any] = {}
    if "OB" in flags:
        out["ups_status"] = "low_battery" if "LB" in flags else "on_battery"
    elif "OL" in flags:
        out["ups_status"] = "online"
    elif "OFF" in flags:
        out["ups_status"] = "offline"
    out["charging"] = "CHRG" in flags
    out["replace_battery"] = "RB" in flags
    out["overload"] = "OVER" in flags

    def num(k):
        try:
            return float(vars_[k])
        except (KeyError, ValueError):
            return None

    if (c := num("battery.charge")) is not None:
        out["battery"] = int(round(c))
    if (r := num("battery.runtime")) is not None:
        out["runtime"] = round(r / 60, 1)
    if (l := num("ups.load")) is not None:
        out["load"] = round(l, 1)
    for src, dst in (("input.voltage", "input_voltage"), ("output.voltage", "output_voltage")):
        if (v := num(src)) is not None:
            out[dst] = round(v, 1)
    p = num("ups.realpower")
    if p is None and (nom := num("ups.realpower.nominal")) is not None and l is not None:
        p = nom * l / 100
    if p is not None:
        out["power"] = round(p)
    return out


UPS_CAPS = ["ups_status", "battery", "runtime", "load", "input_voltage", "output_voltage", "power"]


# ------------------------------------------------------------------ adapter
class NutAdapter(Adapter):
    source = "nut"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.poll_seconds = max(2.0, float(cfg.get("poll_seconds", 5)))
        self.servers: list[dict] = [s for s in (cfg.get("servers") or []) if isinstance(s, dict)]

    async def start(self):
        if not any(s.get("host") for s in self.servers):
            self.status = "no UPS servers configured"
        for i, s in enumerate(self.servers):
            if s.get("host"):
                self.spawn(f"poll{i}", lambda s=s: self._poll_server(s), max_delay=10)

    async def _poll_server(self, s: dict):
        host, port = s["host"], int(s.get("port", 3493))
        client = NutClient(host, port, s.get("username") or None, s.get("password") or None)
        try:
            await client.connect()
            names = await client.list_ups()
            if s.get("ups"):
                names = {n: d for n, d in names.items() if n == s["ups"]}
            for ups, desc in names.items():
                did = self.dev_id(f"{host}_{ups}")
                if did not in self.hub.devices:
                    self.hub.upsert_device(Device(
                        id=did, name=s.get("name") or desc or ups, source=self.source, room=s.get("room"),
                        capabilities={c: cap(c) for c in UPS_CAPS}))
            self.status = "ok" if names else f"{host}: no UPS found"
            while True:
                for ups in names:
                    did = self.dev_id(f"{host}_{ups}")
                    v = await client.list_vars(ups)
                    dev = self.hub.devices[did]
                    if dev.model is None:
                        dev.model = " ".join(x for x in (v.get("device.mfr") or v.get("ups.mfr"),
                                                         v.get("device.model") or v.get("ups.model")) if x) or None
                    self.hub.set_attributes(did, {"nut_vars": v, "nut_host": f"{host}:{port}", "nut_ups": ups})
                    self.hub.set_values(did, parse_status(v), online=True)
                await asyncio.sleep(self.poll_seconds)
        except Exception:
            for ups_dev in [d for d in self.hub.devices.values() if d.id.startswith(self.dev_id(host + "_"))]:
                self.hub.set_values(ups_dev.id, {}, online=False)
            raise
        finally:
            await client.close()
