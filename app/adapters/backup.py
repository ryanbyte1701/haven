"""Backups of Haven's configuration (and optionally its history database) to another computer.

Builds haven-backup-YYYY-MM-DD-HHMM.tar.gz in a private temp folder:

    config/settings.json, users.json, layout.json, overrides.json, automations.json,
           ecowitt_gateways.json, config.yaml        (whichever exist)
    data/history.db                                    (optional; a consistent copy made with SQLite's
                                                        online backup API, never the live file)
    manifest.json                                      (Haven version, time, file list)

and uploads it with SFTP or SCP (asyncssh) or FTP / FTPS (ftplib in a worker thread), then deletes the
oldest haven-backup-*.tar.gz in the remote folder beyond "keep last N". Sign-in tokens, sessions and
certificates are never included.

SSH host keys are trusted on first use: the server key's SHA256 fingerprint is remembered in
DATA_DIR/backup_state.json (per host:port) and a different key later is refused, unless the new
fingerprint is pasted under Advanced. That file also remembers the last backup, so restarts don't run
a scheduled backup twice and a missed one is made up once at startup.

Not a device integration: it creates no devices. Like every adapter it is stopped on a high-availability
standby, so only the active Haven backs up.
"""
from __future__ import annotations

import asyncio
import ftplib
import io
import json
import os
import posixpath
import re
import secrets as _secrets
import shlex
import shutil
import socket
import sqlite3
import ssl
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import config as config_mod
from ..security import redact
from .base import Adapter

try:                        # optional at import time so the rest of Haven still loads without it
    import asyncssh
except ImportError:         # pragma: no cover
    asyncssh = None

PROTOCOLS = ("sftp", "scp", "ftps", "ftp")
DEFAULT_PORT = {"sftp": 22, "scp": 22, "ftps": 21, "ftp": 21}
SCHEDULES = ("manual", "every_6h", "daily", "weekly")
WEEKLY_DAY = 6                         # Sunday (Monday = 0)
CONNECT_TIMEOUT = 30
STARTUP_DELAY = 60                     # a missed backup is made up this long after start
RETRY_AFTER = 3600                     # a failed scheduled backup is retried after an hour ...
MAX_RETRIES = 3                        # ... at most this many times before waiting for the next slot
ACTION_WAIT = 25                       # "Back up now" answers after this long even if still uploading
NAME_RE = re.compile(r"^haven-backup-\d{4}-\d{2}-\d{2}-\d{4}\.tar\.gz$")
STATE_NAME = "backup_state.json"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class BackupError(RuntimeError):
    """A problem to show the user as is (never a ValueError: the action endpoint maps those to 404)."""


# ---------------------------------------------------------------- settings
@dataclass
class Target:
    protocol: str
    host: str
    port: int
    username: str
    password: str
    private_key: str
    folder: str
    fingerprint: str          # pasted under Advanced (normalized, without "SHA256:"), "" = trust on first use
    verify_tls: bool
    passive: bool

    @property
    def url(self) -> str:
        port = "" if self.port == DEFAULT_PORT[self.protocol] else f":{self.port}"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.protocol}://{host}{port}/{self.folder.lstrip('/')}".rstrip("/")

    @property
    def secrets(self) -> list[str]:
        return [s for s in (self.password, self.private_key) if s]


def _norm_fp(fp: str) -> str:
    fp = str(fp or "").strip()
    if fp.lower().startswith("sha256:"):
        fp = fp[7:]
    return fp.strip().rstrip("=")


def normalize_key(text: str) -> str:
    """A private key pasted into a one-line box loses its line breaks: put them back."""
    text = str(text or "").strip().replace("\r\n", "\n")
    if not text or "\n" in text:
        return text + "\n" if text else ""
    m = re.match(r"^(-----BEGIN [A-Z0-9 ]+-----)\s*(.*?)\s*(-----END [A-Z0-9 ]+-----)$", text)
    if not m:
        return text
    body = re.sub(r"\s+", "", m.group(2))
    width = 70 if "OPENSSH" in m.group(1) else 64
    return "\n".join([m.group(1), *(body[i:i + width] for i in range(0, len(body), width)), m.group(3)]) + "\n"


def target_from(cfg: dict) -> Target:
    proto = str(cfg.get("protocol") or "sftp").lower()
    if proto not in PROTOCOLS:
        raise BackupError(f"unknown protocol {proto[:20]!r}: choose SFTP, SCP, FTPS or FTP")
    host = str(cfg.get("host") or "").strip()
    if not host:
        raise BackupError("needs a server address (host)")
    from ..settings import valid_host
    if not valid_host(host):
        raise BackupError(f"{host[:80]!r} is not a valid host: use an IP address or host name")
    port = cfg.get("port")
    try:
        port = int(port) if port not in (None, "") else DEFAULT_PORT[proto]
    except (TypeError, ValueError):
        raise BackupError("port must be a number") from None
    if not 1 <= port <= 65535:
        raise BackupError("port must be 1-65535")
    user = str(cfg.get("username") or "").strip()
    folder = str(cfg.get("remote_dir") or "").strip()
    for label, v in (("username", user), ("remote folder", folder)):
        if _CTRL.search(v):
            raise BackupError(f"{label} contains control characters")
    if proto in ("sftp", "scp") and not user:
        raise BackupError("needs a username")
    return Target(protocol=proto, host=host, port=port, username=user, password=str(cfg.get("password") or ""),
                  private_key=normalize_key(cfg.get("private_key") or "") if proto in ("sftp", "scp") else "",
                  folder=folder.rstrip("/") if folder not in ("/",) else "/",
                  fingerprint=_norm_fp(cfg.get("host_fingerprint")),
                  verify_tls=bool(cfg.get("verify_tls", True)), passive=bool(cfg.get("passive", True)))


# ---------------------------------------------------------------- schedule
def parse_time_of_day(s) -> dtime:
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.h]?(\d{2}))?\s*([ap]\.?m\.?)?\s*", str(s or "").lower())
    if not m:
        raise BackupError(f"time of day {str(s)[:20]!r}: use HH:MM, e.g. 03:10")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            raise BackupError(f"time of day {str(s)[:20]!r}: use HH:MM, e.g. 03:10")
        h = h % 12 + (12 if ap.startswith("p") else 0)
    if h > 23 or mi > 59:
        raise BackupError(f"time of day {str(s)[:20]!r}: use HH:MM, e.g. 03:10")
    return dtime(h, mi)


def _slots_around(now: datetime, schedule: str, tod: dtime) -> list[datetime]:
    """Every scheduled time from 8 days before to 8 days after `now` (tz-aware, in now's zone)."""
    tz = now.tzinfo
    out = []
    for d in range(-8, 9):
        day = (now + timedelta(days=d)).date()
        if schedule == "weekly" and day.weekday() != WEEKLY_DAY:
            continue
        if schedule == "every_6h":
            times = sorted({dtime((tod.hour + 6 * k) % 24, tod.minute) for k in range(4)})
        else:
            times = [tod]
        for t in times:
            out.append(datetime.combine(day, t, tzinfo=tz))
    return sorted(out)


def next_due(now: datetime, schedule: str, tod: dtime, last_attempt: float | None) -> datetime | None:
    """When the next scheduled backup should run. A slot that passed since the last attempt is due now
    (returned as that past time); never-run schedules wait for the next slot. None = manual only."""
    if schedule not in SCHEDULES or schedule == "manual":
        return None
    slots = _slots_around(now, schedule, tod)
    nxt = next(s for s in slots if s > now)
    if last_attempt is None:
        return nxt
    prev = [s for s in slots if s <= now]
    if prev and prev[-1].timestamp() > last_attempt:
        return prev[-1]
    return nxt


def to_delete(names, keep: int) -> list[str]:
    """Remote backups beyond the newest `keep` (0 = keep everything). Only Haven's own file names count."""
    ours = sorted(n for n in set(names) if NAME_RE.match(n))
    if keep <= 0 or len(ours) <= keep:
        return []
    return ours[:-keep]


# ---------------------------------------------------------------- archive
def _sqlite_copy(src_path: Path, dst_path: Path) -> None:
    """Consistent copy of a live (WAL) database: one backup step under a single read transaction."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")       # a single self-contained file
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def build_archive(work: Path, now: datetime, include_history: bool) -> tuple[Path, dict]:
    """Write the .tar.gz into `work` (a private temp folder) and return (path, manifest)."""
    conf, data = config_mod.conf_dir(), config_mod.DATA_DIR
    entries: list[tuple[str, Path]] = []
    for name in config_mod.CONFIG_FILES:
        p = conf / name
        if p.is_file():
            entries.append((f"config/{name}", p))
    if config_mod.CONFIG_PATH.is_file():
        entries.append(("config/config.yaml", config_mod.CONFIG_PATH))
    history_note = None
    if include_history:
        live = data / "history.db"
        if live.is_file():
            copy = work / "history.db"
            _sqlite_copy(live, copy)
            entries.append(("data/history.db", copy))
        else:
            history_note = "no history.db (history is not stored in SQLite, or nothing recorded yet)"
    manifest = {
        "app": "haven",
        "haven_version": os.getenv("HAVEN_VERSION") or None,
        "created": now.isoformat(timespec="seconds"),
        "files": [{"name": arc, "size": p.stat().st_size} for arc, p in entries],
        "history_included": any(arc == "data/history.db" for arc, _ in entries),
        "restore": "Stop Haven, copy config/* into the config folder and data/history.db into the data folder, "
                   "then start Haven.",
    }
    if history_note:
        manifest["note"] = history_note
    name = f"haven-backup-{now:%Y-%m-%d-%H%M}.tar.gz"
    out = work / name
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w:gz") as tar:
        mbytes = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime, info.mode = len(mbytes), int(now.timestamp()), 0o600
        tar.addfile(info, io.BytesIO(mbytes))
        for arc, p in entries:
            info = tar.gettarinfo(str(p), arcname=arc)
            info.mode, info.uid, info.gid, info.uname, info.gname = 0o600, 0, 0, "", ""
            with open(p, "rb") as fh:
                tar.addfile(info, fh)
    return out, manifest


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


# ---------------------------------------------------------------- remotes
class HostKeyChanged(BackupError):
    pass


def _conn_error(t: Target, e: OSError) -> str:
    if isinstance(e, socket.gaierror):
        return f"can't find the server {t.host!r} (check the address)"
    if isinstance(e, ConnectionRefusedError) or getattr(e, "errno", None) == 111 or "refused" in str(e).lower():
        return f"connection refused by {t.host}:{t.port} (is the {t.protocol.upper()} service on, and the port right?)"
    if isinstance(e, ssl.SSLError):
        return f"TLS problem with {t.host}:{t.port}: {e.reason or e}"
    return f"can't connect to {t.host}:{t.port}: {e.strerror or e}"


class _SSHRemote:
    """SFTP (subsystem) or SCP (scp + shell commands for listing and cleanup)."""

    def __init__(self, t: Target, known: dict):
        self.t, self.known = t, known        # known: {"host:port": "SHA256:..."} (updated on success)
        self.conn = None
        self.sftp = None
        self.fingerprint = ""
        self.first_use = False

    @property
    def hostport(self) -> str:
        return f"{self.t.host}:{self.t.port}"

    async def __aenter__(self):
        if asyncssh is None:
            raise BackupError("SFTP/SCP needs the asyncssh package (pip install asyncssh)")
        t, seen = self.t, []
        remembered = self.known.get(self.hostport, "")
        pinned = t.fingerprint

        class Client(asyncssh.SSHClient):
            def validate_host_public_key(self, host, addr, port, key):
                fp = key.get_fingerprint("sha256")
                seen.append(fp)
                if pinned:
                    return _norm_fp(fp) == pinned
                if remembered:
                    return _norm_fp(fp) == _norm_fp(remembered)
                return True                 # first use: remembered below once sign-in worked

        keys = None
        if t.private_key:
            try:
                keys = [asyncssh.import_private_key(t.private_key, t.password or None)]
            except (asyncssh.KeyImportError, ValueError) as e:
                raise BackupError(f"the private key can't be read ({e}); paste the whole key including the "
                                  "BEGIN and END lines (put its passphrase in Password)") from None
        try:
            self.conn = await asyncio.wait_for(asyncssh.connect(
                t.host, t.port, username=t.username, password=t.password or None, client_keys=keys,
                known_hosts=([], [], []), client_factory=Client, agent_path=None, config=None,
                connect_timeout=CONNECT_TIMEOUT, login_timeout=CONNECT_TIMEOUT,
                keepalive_interval=15, keepalive_count_max=4), CONNECT_TIMEOUT + 5)
        except asyncssh.HostKeyNotVerifiable:
            got = seen[-1] if seen else "unknown"
            if pinned:
                raise HostKeyChanged(
                    f"the server's SSH key is {got}, not the fingerprint under Advanced (SHA256:{pinned}). "
                    "If the server really changed, paste the new fingerprint there; otherwise someone may be "
                    "intercepting the connection.") from None
            raise HostKeyChanged(
                f"the SSH host key of {self.hostport} changed (was {remembered}, now {got}). If you reinstalled or "
                "replaced that server, paste the new fingerprint under Advanced → Host key fingerprint and save; "
                "if not, someone may be intercepting the connection.") from None
        except asyncssh.PermissionDenied:
            raise BackupError(f"SSH sign-in denied for {t.username!r}: check the username, password or key") from None
        except asyncssh.DisconnectError as e:
            raise BackupError(f"the SSH server closed the connection: {e.reason}") from None
        except (asyncio.TimeoutError, socket.timeout):
            raise BackupError(f"timed out connecting to {self.hostport}") from None
        except OSError as e:
            raise BackupError(_conn_error(self.t, e)) from None
        self.fingerprint = seen[-1] if seen else ""
        if self.fingerprint and _norm_fp(self.known.get(self.hostport, "")) != _norm_fp(self.fingerprint):
            self.first_use = not remembered and not pinned
            self.known[self.hostport] = self.fingerprint
        if t.protocol == "sftp":
            try:
                self.sftp = await asyncio.wait_for(self.conn.start_sftp_client(), CONNECT_TIMEOUT)
            except asyncssh.Error as e:
                await self.__aexit__()
                raise BackupError(f"the server doesn't offer SFTP ({e.reason}); try SCP") from None
        return self

    async def __aexit__(self, *exc):
        if self.sftp:
            self.sftp.exit()
        if self.conn:
            self.conn.close()
            try:
                await asyncio.wait_for(self.conn.wait_closed(), 5)
            except Exception:  # noqa: BLE001
                pass

    def _path(self, name: str = "") -> str:
        return posixpath.join(self.t.folder, name) if self.t.folder else name

    async def _run(self, cmd: str) -> str:
        r = await asyncio.wait_for(self.conn.run(cmd, check=False), 120)
        if r.exit_status != 0:
            raise BackupError(f"`{cmd.split()[0]}` on the server failed: {str(r.stderr or r.stdout).strip()[:200]}")
        return str(r.stdout or "")

    async def ensure_dir(self) -> None:
        if not self.t.folder or self.t.folder in ("/", "."):
            return
        if self.sftp:
            try:
                await self.sftp.makedirs(self.t.folder, exist_ok=True)
            except asyncssh.SFTPError as e:
                raise BackupError(f"can't create the remote folder {self.t.folder!r}: {e.reason}") from None
        else:
            await self._run(f"mkdir -p -- {shlex.quote(self.t.folder)}")

    async def put(self, local: Path, name: str) -> None:
        dest = self._path(name)
        if self.sftp:
            part = dest + ".part"
            try:
                await self.sftp.put(str(local), part)
                try:
                    await self.sftp.posix_rename(part, dest)
                except (asyncssh.SFTPOpUnsupported, asyncssh.SFTPFailure):
                    if await self.sftp.exists(dest):
                        await self.sftp.remove(dest)
                    await self.sftp.rename(part, dest)
            except asyncssh.SFTPError as e:
                try:
                    await self.sftp.remove(part)
                except Exception:  # noqa: BLE001
                    pass
                raise BackupError(f"upload to {self.t.folder or 'the login folder'!r} failed: {e.reason}") from None
        else:
            try:
                await asyncssh.scp(str(local), (self.conn, shlex.quote(dest)))
            except asyncssh.SFTPError as e:
                raise BackupError(f"SCP upload failed: {e.reason}") from None

    async def list(self) -> list[str]:
        if self.sftp:
            return [posixpath.basename(n) for n in await self.sftp.listdir(self.t.folder or ".")]
        out = await self._run(f"ls -1a -- {shlex.quote(self.t.folder or '.')}")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    async def delete(self, name: str) -> None:
        if self.sftp:
            await self.sftp.remove(self._path(name))
        else:
            await self._run(f"rm -f -- {shlex.quote(self._path(name))}")


class _ReuseSessionFTP_TLS(ftplib.FTP_TLS):
    """Many FTPS servers (vsftpd, ProFTPD, Synology) insist the data connection resumes the control
    connection's TLS session; plain ftplib doesn't, so uploads would be refused."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class _FTPRemote:
    """FTP / FTPS (explicit TLS, AUTH TLS) with ftplib; every call runs in a worker thread."""

    def __init__(self, t: Target):
        self.t = t
        self.ftp: ftplib.FTP | None = None

    def _connect(self) -> None:
        t = self.t
        if t.protocol == "ftps":
            ctx = ssl.create_default_context()
            if not t.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            ftp = _ReuseSessionFTP_TLS(context=ctx, timeout=CONNECT_TIMEOUT)
        else:
            ftp = ftplib.FTP(timeout=CONNECT_TIMEOUT)
        self.ftp = ftp
        ftp.connect(t.host, t.port, timeout=CONNECT_TIMEOUT)
        try:
            if t.protocol == "ftps":
                ftp.auth()
            ftp.login(t.username or "anonymous", t.password or "")
        except ftplib.error_perm as e:
            if str(e).startswith("530"):
                raise BackupError(f"FTP sign-in denied: {e}") from None
            raise
        if t.protocol == "ftps":
            ftp.prot_p()
        ftp.set_pasv(t.passive)
        self.home = ftp.pwd()

    async def __aenter__(self):
        try:
            await asyncio.to_thread(self._connect)
        except ssl.SSLCertVerificationError as e:
            await self.__aexit__()
            raise BackupError(f"the server's TLS certificate isn't trusted ({e.verify_message}). For a self-signed "
                              "certificate turn off Advanced → Verify the FTPS certificate") from None
        except ftplib.error_perm as e:
            await self.__aexit__()
            if str(e).startswith(("500", "502", "504", "534")) and self.t.protocol == "ftps":
                raise BackupError(f"the server doesn't support FTPS (AUTH TLS): {e}") from None
            raise BackupError(f"FTP error: {e}") from None
        except (socket.timeout, TimeoutError):
            await self.__aexit__()
            raise BackupError(f"timed out connecting to {self.t.host}:{self.t.port}") from None
        except OSError as e:
            await self.__aexit__()
            raise BackupError(_conn_error(self.t, e)) from None
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *exc):
        ftp, self.ftp = self.ftp, None
        if ftp is not None:
            def close():
                try:
                    ftp.quit()
                except Exception:  # noqa: BLE001
                    ftp.close()
            await asyncio.to_thread(close)

    def _cd_folder(self, create: bool) -> None:
        ftp, folder = self.ftp, self.t.folder
        ftp.cwd(self.home)
        if not folder:
            return
        if folder.startswith("/"):
            ftp.cwd("/")
        for part in [p for p in folder.split("/") if p and p != "."]:
            try:
                ftp.cwd(part)
            except ftplib.error_perm:
                if not create:
                    raise
                ftp.mkd(part)
                ftp.cwd(part)

    async def _call(self, fn, *a):
        try:
            return await asyncio.to_thread(fn, *a)
        except ftplib.Error as e:
            raise BackupError(f"FTP error: {e}") from None
        except (socket.timeout, TimeoutError):
            raise BackupError("the FTP server stopped answering (timed out)") from None

    async def ensure_dir(self) -> None:
        await self._call(self._cd_folder, True)

    async def put(self, local: Path, name: str) -> None:
        def go():
            self._cd_folder(True)
            with open(local, "rb") as f:
                self.ftp.storbinary(f"STOR {name}.part", f, blocksize=64 * 1024)
            try:
                self.ftp.delete(name)
            except ftplib.error_perm:
                pass
            self.ftp.rename(f"{name}.part", name)
        await self._call(go)

    async def list(self) -> list[str]:
        def go():
            self._cd_folder(False)
            try:
                return [posixpath.basename(n) for n in self.ftp.nlst()]
            except ftplib.error_perm as e:           # some servers answer 550 for an empty folder
                if str(e).startswith("550"):
                    return []
                raise
        return await self._call(go)

    async def delete(self, name: str) -> None:
        def go():
            self._cd_folder(False)
            self.ftp.delete(name)
        await self._call(go)


# ---------------------------------------------------------------- adapter
class BackupAdapter(Adapter):
    source = "backup"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self._lock = asyncio.Lock()
        self._job: asyncio.Task | None = None
        self.status = "starting"

    # -- state (DATA_DIR/backup_state.json)
    @staticmethod
    def state_path() -> Path:
        return config_mod.DATA_DIR / STATE_NAME

    def load_state(self) -> dict:
        try:
            s = json.loads(self.state_path().read_text())
            return s if isinstance(s, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def save_state(self, s: dict) -> None:
        try:
            config_mod.write_private(self.state_path(), json.dumps(s, indent=2))
        except OSError as e:
            self.log.warning("can't save %s: %s", STATE_NAME, e)

    # -- preferences
    def tz(self):
        name = None
        try:
            from ..settings import effective_config
            name = (effective_config().get("time") or {}).get("timezone")
        except Exception:  # noqa: BLE001
            pass
        for n in (name, os.getenv("TZ"), "UTC"):
            try:
                if n:
                    return ZoneInfo(str(n))
            except (ZoneInfoNotFoundError, ValueError):
                continue
        return ZoneInfo("UTC")

    def _clock(self, dt: datetime) -> str:
        fmt = "12h"
        try:
            from ..settings import effective_config
            fmt = (effective_config().get("time") or {}).get("time_format") or "12h"
        except Exception:  # noqa: BLE001
            pass
        hm = f"{dt:%H:%M}" if fmt == "24h" else f"{dt:%I:%M %p}".lstrip("0")
        today = datetime.now(dt.tzinfo).date()
        if dt.date() == today:
            return hm
        if (dt.date() - today).days == 1:
            return f"tomorrow {hm}"
        if (dt.date() - today).days == -1:
            return f"yesterday {hm}"
        if abs((dt.date() - today).days) < 7:
            return f"{dt:%a} {hm}"
        return f"{dt:%b} {dt.day} {hm}"

    @property
    def schedule(self) -> str:
        s = str(self.cfg.get("schedule") or "daily")
        return s if s in SCHEDULES else "daily"

    @property
    def keep(self) -> int:
        try:
            return max(0, int(float(self.cfg.get("keep") if self.cfg.get("keep") not in (None, "") else 14)))
        except (TypeError, ValueError):
            return 14

    def _secrets(self) -> list[str]:
        return [str(self.cfg.get(k)) for k in ("password", "private_key") if self.cfg.get(k)]

    def _clean(self, e) -> str:
        return redact(str(e) or type(e).__name__, self._secrets())

    def next_run(self, now: datetime | None = None) -> datetime | None:
        now = now or datetime.now(self.tz())
        tod = parse_time_of_day(self.cfg.get("time_of_day") or "03:10")
        st = self.load_state()
        due = next_due(now, self.schedule, tod, st.get("last_run"))
        if due is not None and st.get("last_error") and 0 < int(st.get("fails") or 0) <= MAX_RETRIES and st.get("last_run"):
            retry = datetime.fromtimestamp(st["last_run"] + RETRY_AFTER, now.tzinfo)
            due = min(due, retry)
        return due

    def refresh_status(self) -> None:
        st = self.load_state()
        try:
            nxt = self.next_run()
        except BackupError as e:
            self.status = f"error: {e}"
            return
        tz = self.tz()
        if st.get("last_error") and st.get("last_attempt", 0) >= st.get("last_ok", 0):
            when = self._clock(datetime.fromtimestamp(st["last_attempt"], tz))
            self.status = f"error: backup at {when} failed: {st['last_error']}"
        elif st.get("last_ok"):
            when = self._clock(datetime.fromtimestamp(st["last_ok"], tz))
            self.status = f"ok: last backup {when}, {human_size(st.get('last_size') or 0)}"
        elif nxt is not None:
            self.status = f"ok: no backup yet, next {self._clock(nxt)}"
        else:
            self.status = "ok: no backup yet (manual only)"

    # -- lifecycle
    async def start(self):
        try:
            target_from(self.cfg)
        except BackupError as e:
            self.status = str(e) if str(e).startswith("needs") else f"needs settings: {e}"
            return
        self.refresh_status()
        self.spawn("schedule", self._loop, max_delay=3600)

    async def stop(self):
        if self._job and not self._job.done():
            self._job.cancel()
            await asyncio.gather(self._job, return_exceptions=True)
        await super().stop()

    async def _loop(self):
        first = True
        while True:
            due = self.next_run()
            if due is None:
                await asyncio.sleep(3600)
                continue
            wait = (due - datetime.now(due.tzinfo)).total_seconds()
            if wait <= 0 and first:
                self.log.info("a scheduled backup was missed (due %s): making it up in %ss", due, STARTUP_DELAY)
                await asyncio.sleep(STARTUP_DELAY)
                wait = 0
            first = False
            if self._lock.locked():                         # "Back up now" is running
                await asyncio.sleep(30)
                continue
            if wait > 0:
                await asyncio.sleep(min(wait, 300))         # re-check: settings, clock or time zone may change
                continue
            try:
                await self.run_backup(scheduled=True)
            except BackupError:
                pass                                       # recorded in the state and status
            except Exception as e:  # noqa: BLE001
                self.log.warning("scheduled backup failed: %s", self._clean(e))

    # -- the work
    def _remote(self, t: Target, known: dict):
        return _SSHRemote(t, known) if t.protocol in ("sftp", "scp") else _FTPRemote(t)

    async def run_backup(self, scheduled: bool = False) -> str:
        """Build, upload, prune. Returns the message for the Settings page; raises BackupError."""
        if self._lock.locked():
            raise BackupError("a backup is already running")
        async with self._lock:
            started = time.time()
            now = datetime.now(self.tz())
            self.status = "backing up…"
            work = Path(tempfile.mkdtemp(prefix="haven-backup-"))
            try:
                t = target_from(self.cfg)
                archive, manifest = await asyncio.to_thread(build_archive, work, now,
                                                            bool(self.cfg.get("include_history", True)))
                size = archive.stat().st_size
                known = dict(self.load_state().get("host_keys") or {})
                pruned, prune_err = [], ""
                async with self._remote(t, known) as r:
                    await r.ensure_dir()
                    await r.put(archive, archive.name)
                    try:
                        for old in to_delete(await r.list(), self.keep):
                            await r.delete(old)
                            pruned.append(old)
                    except Exception as e:  # noqa: BLE001 - the backup itself worked
                        prune_err = self._clean(e)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                msg = self._clean(e) if str(e) else type(e).__name__
                st = self.load_state()
                st.update(last_attempt=started, last_error=msg)
                if scheduled:                     # a failed scheduled run is retried (see next_run)
                    st.update(last_run=started, fails=int(st.get("fails") or 0) + 1)
                self.save_state(st)
                self.refresh_status()
                self.log.warning("backup failed: %s", msg)
                raise BackupError(msg) from None
            finally:
                shutil.rmtree(work, ignore_errors=True)
            st = self.load_state()
            st.update(host_keys=known, last_attempt=started, last_run=started, last_ok=time.time(),
                      last_name=archive.name, last_size=size, last_dest=t.url, last_error="", fails=0)
            self.save_state(st)
            self.refresh_status()
            files = len(manifest["files"])
            msg = f"Uploaded {archive.name} ({human_size(size)}, {files} file{'s' if files != 1 else ''}) to {t.url}"
            if pruned:
                msg += f"; removed {len(pruned)} older backup{'s' if len(pruned) != 1 else ''}"
            if prune_err:
                msg += f"; couldn't remove older backups: {prune_err}"
            if t.protocol == "ftp":
                msg += " (FTP isn't encrypted: consider SFTP)"
            self.log.info("backup %s (%s) uploaded to %s", archive.name, human_size(size), t.url)
            return msg + "."

    async def test_connection(self) -> str:
        t = target_from(self.cfg)
        st = self.load_state()
        known = dict(st.get("host_keys") or {})
        probe = f".haven-write-test-{_secrets.token_hex(4)}"
        work = Path(tempfile.mkdtemp(prefix="haven-backup-test-"))
        try:
            (work / probe).write_bytes(b"Haven backup write test. Safe to delete.\n")
            async def go():
                async with self._remote(t, known) as r:
                    await r.ensure_dir()
                    await r.put(work / probe, probe)
                    await r.delete(probe)
                    return r
            try:
                r = await asyncio.wait_for(go(), 90)
            except asyncio.TimeoutError:
                raise BackupError(f"timed out talking to {t.host}:{t.port}") from None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        st = self.load_state()
        st["host_keys"] = known
        self.save_state(st)
        msg = f"Connected to {t.url}: the folder is writable."
        if isinstance(r, _SSHRemote) and r.fingerprint:
            msg += f" Server key {r.fingerprint}" + (" (new, now trusted)." if r.first_use else " (trusted).")
        if t.protocol == "ftp":
            msg += " Note: FTP sends your password and backups unencrypted."
        return msg

    async def action(self, name: str) -> str:
        if name not in ("backup_now", "test"):
            raise ValueError(name)
        try:
            if name == "test":
                return await self.test_connection()
            if self._lock.locked():
                raise BackupError("a backup is already running")
            self._job = asyncio.create_task(self.run_backup())
            self._job.add_done_callback(lambda j: j.cancelled() or j.exception())   # finished after we answered
            done, _ = await asyncio.wait({self._job}, timeout=ACTION_WAIT)
            if not done:
                return "Still uploading… the status line shows the result when it's done."
            return self._job.result()
        except BackupError as e:
            m = str(e)
            raise BackupError(m[:1].upper() + m[1:]) from None
        except Exception as e:  # noqa: BLE001 - never a ValueError (that means "unknown action")
            raise BackupError(self._clean(e)) from None


ADAPTER = BackupAdapter
SETTINGS = {
    "id": "backup", "label": "Backups", "icon": "backup",
    "help": "Copies Haven's configuration (settings, accounts, layout, automations, trusted gateways, config.yaml) "
            "and, if you like, the history database to another computer or NAS over SFTP, SCP, FTPS or FTP, as one "
            "haven-backup-DATE-TIME.tar.gz file. The backup contains your integration passwords and keys, so use a "
            "private folder that only you can read, and prefer SFTP: plain FTP sends everything unencrypted. "
            "Sign-in sessions and tokens are not included (you sign in again after a restore). "
            "Fill in the server, save, then use Test connection. "
            "How to restore: stop Haven, unpack the file, copy the files in config/ into docker/haven/config "
            "and data/history.db into docker/haven/data, then start Haven.",
    "fields": [
        {"key": "protocol", "label": "Send backups with", "type": "select", "default": "sftp",
         "options": list(PROTOCOLS)},
        {"key": "host", "label": "Server (IP address or host name)", "type": "text"},
        {"key": "port", "label": "Port", "type": "number",
         "help": "Empty = the usual port: 22 for SFTP and SCP, 21 for FTP and FTPS."},
        {"key": "username", "label": "Username", "type": "text"},
        {"key": "password", "label": "Password", "type": "password",
         "help": "For a private key with a passphrase, put the passphrase here."},
        {"key": "private_key", "label": "Private key (optional, SFTP and SCP)", "type": "password",
         "multiline": True,
         "help": "Paste the whole key, from -----BEGIN to -----END (OpenSSH or PEM format). "
                 "Leave empty to sign in with the password."},
        {"key": "remote_dir", "label": "Folder on the server", "type": "text", "default": "haven-backups",
         "help": "Created if missing. Without a leading / it is inside the user's home folder, "
                 "e.g. /volume1/backups/haven on a Synology."},
        {"key": "schedule", "label": "Back up automatically", "type": "select", "default": "daily",
         "options": list(SCHEDULES)},
        {"key": "time_of_day", "label": "At (time of day, HH:MM)", "type": "text", "default": "03:10",
         "help": "Daily and weekly backups run at this time (weekly: on Sundays); every 6 hours starts from it. "
                 "Uses the time zone from Date & time."},
        {"key": "keep", "label": "Keep the last (backups)", "type": "number", "default": 14,
         "help": "Older haven-backup files in the folder are deleted. 0 = keep all."},
        {"key": "include_history", "label": "Include the history database (charts)", "type": "bool", "default": True,
         "help": "Can be large. Configuration alone is small."},
        {"key": "host_fingerprint", "label": "SSH host key fingerprint (SFTP, SCP)", "type": "text", "advanced": True,
         "help": "Empty = trust the server's key the first time and refuse it if it changes later. "
                 "Paste a fingerprint (SHA256:…) to require exactly that key, or after the server's key changed "
                 "on purpose."},
        {"key": "verify_tls", "label": "Verify the FTPS certificate", "type": "bool", "default": True,
         "advanced": True, "help": "Turn off for a NAS with a self-signed certificate."},
        {"key": "passive", "label": "FTP passive mode", "type": "bool", "default": True, "advanced": True,
         "help": "Leave on unless your FTP server needs active mode."},
    ],
    "actions": [{"id": "backup_now", "label": "Back up now", "icon": "backup"},
                {"id": "test", "label": "Test connection", "icon": "sync"}],
}
