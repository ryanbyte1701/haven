"""NUT (Network UPS Tools) support.

Client: polls one or more NUT servers, e.g. the one built into UniFi UPS Tower / UPS-2U
        (UniFi Network > the UPS > Settings > NUT Server), APC/CyberPower via a NAS, etc.
Server: re-shares every monitored UPS on port 3493 using the standard NUT protocol, so a
        Synology NAS, Proxmox, TrueNAS or any upsmon client can shut down safely. It reports
        ERR DATA-STALE when the upstream UPS stops answering, so clients never act on old data.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import shlex
import time
from typing import Any

from ..models import Device, cap
from .base import Adapter

STALE_MIN = 30        # seconds without a successful poll before data counts as stale (at least)
MAX_CLIENTS = 64      # simultaneous NUT client connections
LOGIN_TIMEOUT = 30    # idle seconds allowed before a client logs in
IDLE_TIMEOUT = 300    # idle seconds allowed after login (upsmon polls every few seconds)
PRELOGIN_LIMIT = 120  # total seconds a connection may stay open without ever logging in
MAX_PER_IP = 8        # concurrent connections from one address
PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fd00::/8", "fe80::/10"]


# ------------------------------------------------------------------ client
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
        self.poll_seconds = float(cfg.get("poll_seconds", 5))
        self.servers: list[dict] = cfg.get("servers") or []
        # name exposed by our NUT server -> (device_id, upstream vars, last ok time)
        self.relay: dict[str, dict[str, Any]] = {}
        self.server_cfg = cfg.get("server") or {}
        self.server: asyncio.base_events.Server | None = None
        self.clients: dict[str, dict] = {}
        self._writers: set[asyncio.StreamWriter] = set()
        self._slots = asyncio.Semaphore(MAX_CLIENTS)
        self.stale_after = max(STALE_MIN, 3 * self.poll_seconds)
        self._real_names: set[str] = set()      # relay names that are real UPSes (not the alias)
        self._per_ip: dict[str, int] = {}
        self.server_error: str | None = None

    def _ok_status(self) -> str:
        return f"ok · NUT server failed: {self.server_error}" if self.server_error else "ok"

    async def start(self):
        if not self.servers:
            self.status = "no UPS servers configured"
        for i, s in enumerate(self.servers):
            if s.get("host"):
                self.spawn(f"poll{i}", lambda s=s: self._poll_server(s), max_delay=10)
        if self.server_cfg.get("enabled", False):
            await self._start_server()

    async def stop(self):
        await super().stop()
        if self.server:
            self.server.close()
        # Drop connected clients too, so upsmon reconnects to the new adapter instead of
        # being stuck on this one's frozen data.
        for w in list(self._writers):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
        if self.server:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass

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
                self._register_relay(ups, did, host, primary=s is self.servers[0] and ups == next(iter(names)))
            self.status = self._ok_status()
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
                    name = self._relay_name_for(did)
                    if name:
                        r = self.relay[name]
                        if r.get("fsd") and "OL" in v.get("ups.status", "").split():
                            r["fsd"] = False          # power is back: clear forced shutdown
                        r.update(vars=v, ok=time.time())
                await asyncio.sleep(self.poll_seconds)
        except Exception:
            for ups_dev in [d for d in self.hub.devices.values() if d.id.startswith(self.dev_id(host + "_"))]:
                self.hub.set_values(ups_dev.id, {}, online=False)
            raise
        finally:
            await client.close()

    # ---------------- built-in NUT server ----------------
    def _register_relay(self, ups: str, did: str, host: str, primary: bool = False):
        name = self._relay_name_for(did)
        if not name:
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", ups)
            if name in self._real_names and self.relay[name]["device"] != did:
                name = f"{name}_{re.sub(r'[^A-Za-z0-9]', '', host)[-6:]}"
            self.relay[name] = {"device": did, "vars": {}, "ok": 0.0, "fsd": False}
            self._real_names.add(name)
        alias = self.server_cfg.get("primary_alias", "ups")
        # The alias (Synology always asks for "ups") is pinned to the first UPS of the first
        # configured server, whichever server answers first; before that, any UPS stands in.
        if alias and alias not in self._real_names and (primary or alias not in self.relay):
            self.relay[alias] = self.relay[name]      # same dict

    def _relay_name_for(self, did: str) -> str | None:
        return next((n for n in self._real_names if self.relay[n]["device"] == did), None)

    def _allowed(self, ip: str) -> bool:
        nets = self.server_cfg.get("allow") or PRIVATE_NETS     # empty = private ranges only
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
            if addr.version == 6 and addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            return any(addr in ipaddress.ip_network(n, strict=False) for n in nets)
        except ValueError:
            return False

    async def _start_server(self):
        port = int(self.server_cfg.get("port", 3493))
        try:
            self.server = await asyncio.start_server(self._handle, host=self.server_cfg.get("bind", "0.0.0.0"), port=port)
        except (OSError, ValueError) as e:     # port in use, bad bind address: keep polling UPSes anyway
            self.server_error = str(e) or type(e).__name__
            self.log.error("NUT server could not listen on port %s: %s", port, e)
            self.status = self._ok_status() if self.status == "ok" else f"{self.status} · NUT server failed: {e}"
            return
        self.log.info("NUT server listening on port %s", port)

    def server_info(self) -> dict:
        return {"enabled": bool(self.server), "port": int(self.server_cfg.get("port", 3493)),
                "names": sorted(self.relay), "alias": self.server_cfg.get("primary_alias", "ups"),
                "clients": [{"address": a, **c} for a, c in self.clients.items()]}

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername") or ("?", 0)
        addr = f"{peer[0]}:{peer[1]}"
        if not self._allowed(peer[0]):
            writer.write(b"ERR ACCESS-DENIED\n")
            await writer.drain()
            writer.close()
            return
        ip = peer[0]
        if self._slots.locked() or self._per_ip.get(ip, 0) >= MAX_PER_IP:
            writer.write(b"ERR MAX-CLIENTS\n")
            await writer.drain()
            writer.close()
            return
        await self._slots.acquire()
        self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
        opened = time.monotonic()
        self._writers.add(writer)
        users: dict = self.server_cfg.get("users") or (
            {self.server_cfg["user"]: str(self.server_cfg.get("password") or "")} if self.server_cfg.get("user") else {})
        sess = {"user": None, "password": None, "login": None, "primary": False}
        self.clients[addr] = {"since": time.time(), "ups": None, "user": None}
        try:
            while True:
                try:
                    wait = IDLE_TIMEOUT if sess["login"] else \
                        min(LOGIN_TIMEOUT, PRELOGIN_LIMIT - (time.monotonic() - opened))
                    if wait <= 0:
                        break
                    raw = await asyncio.wait_for(reader.readline(), wait)
                except (ValueError, asyncio.LimitOverrunError):      # absurdly long line
                    writer.write(b"ERR INVALID-ARGUMENT\n")
                    break
                if not raw:
                    break
                try:
                    args = shlex.split(raw.decode(errors="replace").strip())
                except ValueError:
                    args = []
                reply = self._reply(args, sess, users, addr)
                writer.write(reply.encode())
                await writer.drain()
                if args[:1] == ["LOGOUT"]:
                    break
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            self.clients.pop(addr, None)
            self._writers.discard(writer)
            self._slots.release()
            self._per_ip[ip] -= 1
            if self._per_ip[ip] <= 0:
                self._per_ip.pop(ip, None)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _vars(r: dict) -> dict:
        if not r.get("fsd"):
            return r["vars"]
        v = dict(r["vars"])
        v["ups.status"] = (v.get("ups.status", "") + " FSD").strip()
        return v

    def _reply(self, a: list[str], sess: dict, users: dict, addr: str) -> str:  # noqa: C901
        q = lambda s: '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'   # noqa: E731
        if not a:
            return "ERR UNKNOWN-COMMAND\n"
        cmd = a[0].upper()
        if cmd == "VER":
            return "Network UPS Tools upsd 2.8.2 - Haven relay\n"
        if cmd == "NETVER":
            return "1.3\n"
        if cmd == "HELP":
            return "Commands: HELP VER GET LIST USERNAME PASSWORD STARTTLS LOGIN LOGOUT PRIMARY MASTER FSD\n"
        if cmd == "STARTTLS":
            return "ERR FEATURE-NOT-CONFIGURED\n"
        if cmd == "LOGOUT":
            return "OK Goodbye\n"
        if cmd == "USERNAME" and len(a) > 1:
            if sess["user"]:
                return "ERR ALREADY-SET-USERNAME\n"
            sess["user"] = a[1]
            return "OK\n"
        if cmd == "PASSWORD" and len(a) > 1:
            if sess["password"] is not None:
                return "ERR ALREADY-SET-PASSWORD\n"
            sess["password"] = a[1]
            return "OK\n"

        def authed() -> bool:
            return not users or (sess["user"] in users and str(users[sess["user"]]) == sess["password"])

        def ups_entry(name):
            return self.relay.get(name)

        if cmd in ("LOGIN", "PRIMARY", "MASTER", "FSD"):
            if len(a) < 2:
                return "ERR INVALID-ARGUMENT\n"
            if not authed():
                return "ERR ACCESS-DENIED\n"
            # Forced shutdown powers off every client of this UPS: only with server.allow_fsd on
            # and a real username/password configured (never for an open, password-less server).
            if cmd != "LOGIN" and not (self.server_cfg.get("allow_fsd") is True and users and sess["user"] in users):
                return "ERR ACCESS-DENIED\n"
            if not ups_entry(a[1]):
                return "ERR UNKNOWN-UPS\n"
            if cmd == "LOGIN":
                sess["login"] = a[1]
                self.clients[addr].update(ups=a[1], user=sess["user"])
                return "OK\n"
            if cmd == "FSD":
                if not sess["primary"]:
                    return "ERR ACCESS-DENIED\n"
                ups_entry(a[1])["fsd"] = True
                return "OK FSD-SET\n"
            sess["primary"] = True
            return f"OK {'PRIMARY' if cmd == 'PRIMARY' else 'MASTER'}-GRANTED\n"

        if cmd == "LIST" and len(a) >= 2:
            sub = a[1].upper()
            if sub == "UPS":
                lines = [f"UPS {n} {q(self.hub.devices.get(r['device']).name if r['device'] in self.hub.devices else n)}"
                         for n, r in self.relay.items()]
                return "BEGIN LIST UPS\n" + "".join(l + "\n" for l in lines) + "END LIST UPS\n"
            if len(a) < 3:
                return "ERR INVALID-ARGUMENT\n"
            name = a[2]
            r = ups_entry(name)
            if not r:
                return "ERR UNKNOWN-UPS\n"
            if sub == "VAR":
                if time.time() - r["ok"] > self.stale_after:
                    return "ERR DATA-STALE\n"
                body = "".join(f"VAR {name} {k} {q(v)}\n" for k, v in sorted(self._vars(r).items()))
                return f"BEGIN LIST VAR {name}\n{body}END LIST VAR {name}\n"
            if sub in ("CMD", "RW", "CLIENT"):
                body = "".join(f"CLIENT {name} {c.split(':')[0]}\n" for c, v in self.clients.items()
                               if v.get("ups") == name) if sub == "CLIENT" else ""
                return f"BEGIN LIST {sub} {name}\n{body}END LIST {sub} {name}\n"
            return "ERR INVALID-ARGUMENT\n"

        if cmd == "GET" and len(a) >= 3:
            sub, name = a[1].upper(), a[2]
            r = ups_entry(name)
            if not r:
                return "ERR UNKNOWN-UPS\n"
            if sub == "NUMLOGINS":
                return f"NUMLOGINS {name} {sum(1 for c in self.clients.values() if c.get('ups') == name)}\n"
            if sub == "UPSDESC":
                d = self.hub.devices.get(r["device"])
                return f"UPSDESC {name} {q(d.name if d else name)}\n"
            if len(a) < 4:
                return "ERR INVALID-ARGUMENT\n"
            var = a[3]
            if sub == "VAR":
                if time.time() - r["ok"] > self.stale_after:
                    return "ERR DATA-STALE\n"
                v = self._vars(r)
                if var not in v:
                    return "ERR VAR-NOT-SUPPORTED\n"
                return f"VAR {name} {var} {q(v[var])}\n"
            if sub == "TYPE":
                v = r["vars"].get(var)
                if v is None:
                    return "ERR VAR-NOT-SUPPORTED\n"
                return f"TYPE {name} {var} {'NUMBER' if re.fullmatch(r'-?[0-9.]+', v) else 'STRING:64'}\n"
            if sub == "DESC":
                return f"DESC {name} {var} \"Description unavailable\"\n"
            if sub == "CMDDESC":
                return f"CMDDESC {name} {var} \"Description unavailable\"\n"
            return "ERR INVALID-ARGUMENT\n"
        if cmd in ("SET", "INSTCMD"):
            return "ERR CMD-NOT-SUPPORTED\n"
        return "ERR UNKNOWN-COMMAND\n"
