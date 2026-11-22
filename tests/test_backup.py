"""Backups (app/adapters/backup.py): archive contents, schedule, retention, and real uploads to in-process
SFTP / SCP (asyncssh) and FTP / FTPS (pyftpdlib, test-only: pip install pyftpdlib pyopenssl) servers."""
import asyncio
import datetime as dt
import json
import socket
import sqlite3
import tarfile
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

asyncssh = pytest.importorskip("asyncssh")

from app import config as config_mod                     # noqa: E402
from app.adapters import backup as bk                    # noqa: E402

from conftest import ADMIN                               # noqa: E402

PW = "s3cret-backup-pass"


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def haven_dirs(tmp_path, monkeypatch):
    """A config folder and a data folder like a real install, with files that must and mustn't be backed up."""
    conf, data = tmp_path / "config", tmp_path / "data"
    conf.mkdir()
    data.mkdir()
    monkeypatch.setattr(config_mod, "CONF_DIR", conf)
    monkeypatch.setattr(config_mod, "DATA_DIR", data)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", conf / "config.yaml")
    (conf / "config.yaml").write_text("history:\n  backend: sqlite\n")
    for name in config_mod.CONFIG_FILES:
        (conf / name).write_text(json.dumps({"file": name}))
    (conf / "sessions.json").write_text("{}")            # never in a backup, wherever it is
    (data / "sessions.json").write_text("{}")
    (data / "aqara_token.json").write_text('{"token": "x"}')
    (data / "ring_token.cache").write_text("tok")
    db = sqlite3.connect(data / "history.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE samples (device TEXT, cap TEXT, ts REAL, v REAL, binary INTEGER DEFAULT 0)")
    db.executemany("INSERT INTO samples VALUES (?,?,?,?,0)", [("d", "temperature", i, i / 10) for i in range(500)])
    db.commit()
    db.close()
    return conf, data


def _members(archive: Path) -> dict[str, bytes]:
    with tarfile.open(archive) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


class SSHServer:
    """asyncssh SFTP + SCP (+ a shell for SCP's ls/rm/mkdir) server on 127.0.0.1, in its own thread."""

    def __init__(self, root: Path):
        self.root = root
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.port = 0
        self.server = None
        self.client_pub: str | None = None
        self.passwords_seen: list[str] = []
        self.host_key = None
        self.start(asyncssh.generate_private_key("ssh-ed25519"))

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(20)

    def start(self, host_key):
        self.host_key = host_key
        outer = self

        class Srv(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                outer.passwords_seen.append(password)
                return username == "backup" and password == PW

            def public_key_auth_supported(self):
                return outer.client_pub is not None

            def validate_public_key(self, username, key):
                return username == "backup" and key.export_public_key().decode().split()[1] == outer.client_pub

        async def shell(process):
            p = await asyncio.create_subprocess_shell(process.command or "true", cwd=outer.root,
                                                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await p.communicate()
            process.stdout.write(out)
            process.stderr.write(err)
            process.exit(p.returncode)

        async def listen():
            return await asyncssh.listen(
                "127.0.0.1", self.port, server_host_keys=[host_key], server_factory=Srv,
                sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(outer.root)),
                allow_scp=True, process_factory=shell, encoding=None)
        self.server = self._call(listen())
        self.port = self.server.sockets[0].getsockname()[1]

    def restart_with_new_key(self):
        self.server.close()
        self._call(self.server.wait_closed())
        self.start(asyncssh.generate_private_key("ssh-ed25519"))

    @property
    def fingerprint(self) -> str:
        return self.host_key.get_fingerprint("sha256")

    def stop(self):
        self.server.close()
        self._call(self.server.wait_closed())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def ssh_server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    s = SSHServer(root)
    yield s
    s.stop()


def _cfg(server, protocol="sftp", **kw):
    return {"enabled": True, "protocol": protocol, "host": "127.0.0.1", "port": server.port, "username": "backup",
            "password": PW, "remote_dir": "haven-backups", "schedule": "manual", "keep": 14,
            "include_history": True, **kw}


# ---------------------------------------------------------------- archive
def test_archive_contents_and_live_history_copy(haven_dirs, tmp_path):
    conf, data = haven_dirs
    stop = threading.Event()
    written = []

    def writer():                                       # the history store keeps writing during the backup
        db = sqlite3.connect(data / "history.db", timeout=30)
        i = 0
        while not stop.is_set():
            db.execute("INSERT INTO samples VALUES ('w', 'power', ?, ?, 0)", (time.time(), i))
            db.commit()
            written.append(i)
            i += 1
        db.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        while len(written) < 20:
            time.sleep(0.01)
        work = tmp_path / "work"
        work.mkdir()
        now = dt.datetime(2026, 11, 16, 16, 52, tzinfo=ZoneInfo("America/Phoenix"))
        archive, manifest = bk.build_archive(work, now, include_history=True)
    finally:
        stop.set()
        t.join()
    assert archive.name == "haven-backup-2026-11-16-1652.tar.gz"
    assert archive.stat().st_mode & 0o077 == 0
    files = _members(archive)
    assert set(files) == {"manifest.json", "data/history.db", "config/config.yaml",
                          *(f"config/{n}" for n in config_mod.CONFIG_FILES)}
    assert not any("session" in n or "token" in n for n in files)
    assert json.loads(files["config/settings.json"]) == {"file": "settings.json"}
    man = json.loads(files["manifest.json"])
    assert man["history_included"] and man["created"].startswith("2026-11-16T16:52")
    assert {f["name"] for f in man["files"]} == set(files) - {"manifest.json"}
    copy = tmp_path / "restored.db"
    copy.write_bytes(files["data/history.db"])
    db = sqlite3.connect(copy)
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"     # one self-contained file
    assert db.execute("SELECT COUNT(*) FROM samples WHERE device='d'").fetchone()[0] == 500
    assert db.execute("SELECT COUNT(*) FROM samples WHERE device='w'").fetchone()[0] >= 1
    db.close()


def test_archive_without_history(haven_dirs, tmp_path):
    archive, man = bk.build_archive(tmp_path, dt.datetime.now(dt.timezone.utc), include_history=False)
    assert "data/history.db" not in _members(archive) and not man["history_included"]


# ---------------------------------------------------------------- retention & schedule
def test_retention_only_touches_haven_backups():
    names = [f"haven-backup-2026-11-{d:02d}-0310.tar.gz" for d in range(1, 11)] + [
        "notes.txt", "haven-backup-2026-11-01-0310.tar.gz.part", "haven-backup-latest.tar.gz"]
    gone = bk.to_delete(names, 3)
    assert gone == [f"haven-backup-2026-11-{d:02d}-0310.tar.gz" for d in range(1, 8)]
    assert bk.to_delete(names, 0) == [] and bk.to_delete(names, 10) == []


def test_next_due():
    tz = ZoneInfo("America/New_York")
    at = dt.time(3, 10)
    now = dt.datetime(2026, 11, 16, 12, 0, tzinfo=tz)          # a Monday
    assert bk.next_due(now, "manual", at, None) is None
    assert bk.next_due(now, "daily", at, None) == dt.datetime(2026, 11, 17, 3, 10, tzinfo=tz)
    # ran this morning -> tomorrow; last ran yesterday -> this morning's slot is overdue (due in the past)
    assert bk.next_due(now, "daily", at, now.replace(hour=3, minute=11).timestamp()).day == 17
    overdue = bk.next_due(now, "daily", at, (now - dt.timedelta(days=1)).timestamp())
    assert overdue == dt.datetime(2026, 11, 16, 3, 10, tzinfo=tz) and overdue < now
    assert bk.next_due(now, "every_6h", at, now.replace(hour=9, minute=11).timestamp()) == now.replace(hour=15, minute=10)
    wk = bk.next_due(now, "weekly", at, now.timestamp())
    assert wk == dt.datetime(2026, 11, 22, 3, 10, tzinfo=tz) and wk.weekday() == 6
    # across the DST change (Nov 1, 2026): still 03:10 local
    before = dt.datetime(2026, 10, 31, 12, 0, tzinfo=tz)
    nxt = bk.next_due(before, "daily", at, before.timestamp())
    assert (nxt.hour, nxt.minute, nxt.day) == (3, 10, 1)
    assert bk.parse_time_of_day("3:10 pm") == dt.time(15, 10) and bk.parse_time_of_day("0310") is not None
    with pytest.raises(bk.BackupError):
        bk.parse_time_of_day("25:00")


def test_failed_scheduled_backup_retries_hourly(haven_dirs):
    a = bk.BackupAdapter(None, {"schedule": "weekly", "host": "x"})
    # a fixed Wednesday noon: far from the weekly 03:10 Sunday slot (run near the slot, the slot itself comes first)
    when = dt.datetime(2026, 11, 18, 12, 0, tzinfo=a.tz())
    now = when.timestamp()
    a.save_state({"last_run": now - 60, "last_attempt": now - 60, "last_error": "boom", "fails": 1})
    assert abs(a.next_run(when).timestamp() - (now - 60 + 3600)) < 2
    a.save_state({"last_run": now - 60, "last_attempt": now - 60, "last_error": "boom", "fails": 4})
    assert a.next_run(when).timestamp() - now > 3600             # gave up: waits for the next slot


def test_key_without_line_breaks_is_repaired():
    key = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
    flat = key.replace("\n", "")
    assert asyncssh.import_private_key(bk.normalize_key(flat))


# ---------------------------------------------------------------- SFTP / SCP
@pytest.mark.parametrize("protocol", ["sftp", "scp"])
def test_ssh_backup_retention_and_host_key(protocol, haven_dirs, ssh_server):
    remote = ssh_server.root / "haven-backups"
    remote.mkdir()
    for d in (1, 2, 3):
        (remote / f"haven-backup-2020-01-{d:02d}-0310.tar.gz").write_bytes(b"old")
    (remote / "keep-me.txt").write_text("not ours")
    a = bk.BackupAdapter(None, _cfg(ssh_server, protocol, keep=2))

    msg = asyncio.run(a.test_connection())
    assert "writable" in msg and ssh_server.fingerprint in msg and "now trusted" in msg
    assert a.load_state()["host_keys"][f"127.0.0.1:{ssh_server.port}"] == ssh_server.fingerprint
    assert not [p for p in remote.iterdir() if p.name.startswith(".haven-write-test")]

    msg = asyncio.run(a.run_backup())
    assert msg.startswith("Uploaded haven-backup-") and f"{protocol}://127.0.0.1:{ssh_server.port}/haven-backups" in msg
    names = sorted(p.name for p in remote.iterdir())
    new = [n for n in names if n.startswith("haven-backup-20") and not n.startswith("haven-backup-2020")]
    assert len(new) == 1 and names == sorted(["haven-backup-2020-01-03-0310.tar.gz", new[0], "keep-me.txt"])
    files = _members(remote / new[0])
    assert "config/users.json" in files and "data/history.db" in files
    st = a.load_state()
    assert st["last_name"] == new[0] and st["last_error"] == "" and a.status.startswith("ok: last backup")

    # the server's key changes: refused before signing in, with a message that says how to accept it
    ssh_server.restart_with_new_key()
    assert a.cfg["port"] == ssh_server.port                     # same address, different key
    seen = len(ssh_server.passwords_seen)
    with pytest.raises(bk.BackupError) as e:
        asyncio.run(a.run_backup())
    assert "changed" in str(e.value) and ssh_server.fingerprint in str(e.value) and "Advanced" in str(e.value)
    assert len(ssh_server.passwords_seen) == seen                 # the password was never sent
    assert a.status.startswith("error:") and PW not in a.status
    # pasting the new fingerprint accepts it (and remembers it)
    a.cfg["host_fingerprint"] = ssh_server.fingerprint
    assert "Uploaded" in asyncio.run(a.run_backup())
    a.cfg["host_fingerprint"] = ""
    assert "(trusted)" in asyncio.run(a.test_connection())


def test_sftp_private_key_and_wrong_password(haven_dirs, ssh_server):
    key = asyncssh.generate_private_key("ssh-ed25519")
    ssh_server.client_pub = key.export_public_key().decode().split()[1]
    a = bk.BackupAdapter(None, _cfg(ssh_server, password="", private_key=key.export_private_key().decode(),
                                    remote_dir="/deep/er/folder"))
    assert "writable" in asyncio.run(a.test_connection())
    assert (ssh_server.root / "deep/er/folder").is_dir()
    b = bk.BackupAdapter(None, _cfg(ssh_server, password="wrong-password-123"))
    with pytest.raises(bk.BackupError) as e:
        asyncio.run(b.test_connection())
    assert "denied" in str(e.value)


def test_connection_refused_is_clear(haven_dirs):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    a = bk.BackupAdapter(None, {"protocol": "sftp", "host": "127.0.0.1", "port": port, "username": "u",
                                "password": PW, "schedule": "manual"})
    with pytest.raises(bk.BackupError) as e:
        asyncio.run(a.run_backup())
    assert "refused" in str(e.value)
    assert "refused" in a.status and a.load_state()["last_error"]


# ---------------------------------------------------------------- FTP / FTPS
def _self_signed(path: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=2))
            .sign(key, hashes.SHA256()))
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM))


@pytest.fixture
def ftp_server(tmp_path, request):
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import ThreadedFTPServer
    tls = getattr(request, "param", False)
    root = tmp_path / "ftproot"
    root.mkdir()
    auth = DummyAuthorizer()
    auth.add_user("backup", PW, str(root), perm="elradfmwMT")
    if tls:
        pytest.importorskip("OpenSSL")
        from pyftpdlib.handlers import TLS_FTPHandler
        pem = tmp_path / "ftps.pem"
        _self_signed(pem)
        handler = type("H", (TLS_FTPHandler,), {"certfile": str(pem), "tls_control_required": True,
                                               "tls_data_required": True})
    else:
        handler = type("H", (FTPHandler,), {})
    handler.authorizer = auth
    server = ThreadedFTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    t.start()
    server.root, server.port = root, server.address[1]
    yield server
    server.close_all()


@pytest.mark.parametrize("ftp_server", [False, True], indirect=True, ids=["ftp", "ftps"])
def test_ftp_backup_and_retention(haven_dirs, ftp_server):
    proto = "ftps" if ftp_server.handler.__mro__[1].__name__ == "TLS_FTPHandler" else "ftp"
    remote = ftp_server.root / "nas" / "haven"
    remote.mkdir(parents=True)
    for d in (1, 2, 3, 4):
        (remote / f"haven-backup-2020-01-{d:02d}-0310.tar.gz").write_bytes(b"old")
    cfg = {"protocol": proto, "host": "127.0.0.1", "port": ftp_server.port, "username": "backup", "password": PW,
           "remote_dir": "nas/haven", "keep": 3, "schedule": "manual", "include_history": False,
           "verify_tls": False}
    a = bk.BackupAdapter(None, cfg)
    assert "writable" in asyncio.run(a.test_connection())
    msg = asyncio.run(a.run_backup())
    assert "Uploaded" in msg and "removed 2 older backups" in msg
    assert ("not encrypted" in msg or "isn't encrypted" in msg) == (proto == "ftp")
    names = sorted(p.name for p in remote.iterdir())
    assert len(names) == 3 and names[:2] == ["haven-backup-2020-01-03-0310.tar.gz", "haven-backup-2020-01-04-0310.tar.gz"]
    assert "config/settings.json" in _members(remote / names[2])
    bad = bk.BackupAdapter(None, {**cfg, "password": "nope-nope"})
    with pytest.raises(bk.BackupError) as e:
        asyncio.run(bad.test_connection())
    assert "denied" in str(e.value)
    if proto == "ftps":                                           # self-signed: refused while verification is on
        strict = bk.BackupAdapter(None, {**cfg, "verify_tls": True})
        with pytest.raises(bk.BackupError) as e:
            asyncio.run(strict.test_connection())
        assert "certificate" in str(e.value)


# ---------------------------------------------------------------- settings & API
def test_settings_section_and_secret_binding(haven_dirs, monkeypatch):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", haven_dirs[0] / "settings.json")
    settings.SETTINGS_FILE.write_text("{}")
    sec = settings.SCHEMA_BY_ID["backup"]
    assert {a["id"] for a in sec["actions"]} == {"backup_now", "test"} and sec["icon"] == "backup"
    settings.save_section("backup", True, {"protocol": "sftp", "host": "nas.local", "username": "u",
                                           "password": PW, "private_key": "KEYDATA-123"})
    view = next(s for s in settings.public_view(settings.effective_config(), {}) if s["id"] == "backup")
    assert view["values"]["password"] == settings.MASK and view["values"]["private_key"] == settings.MASK
    settings.save_section("backup", True, {"protocol": "sftp", "host": "nas.local", "password": settings.MASK})
    assert settings.effective_config()["backup"]["password"] == PW
    # switching to plain FTP (or another host) must not reuse the stored secrets
    settings.save_section("backup", True, {"protocol": "ftp", "host": "nas.local", "password": settings.MASK,
                                           "private_key": settings.MASK})
    cfg = settings.effective_config()["backup"]
    assert cfg["password"] == "" and cfg["private_key"] == ""


def test_action_endpoint(haven_dirs, ssh_server):
    from fastapi.testclient import TestClient
    from app import main
    a = bk.BackupAdapter(main.hub, _cfg(ssh_server))
    main.hub.adapters["backup"] = a
    try:
        c = TestClient(main.app)
        assert c.post("/api/settings/backup/action/test").status_code == 401      # admins only
        r = c.post("/api/settings/backup/action/test", headers=ADMIN)
        assert r.status_code == 200 and "writable" in r.json()["message"]
        r = c.post("/api/settings/backup/action/backup_now", headers=ADMIN)
        assert r.status_code == 200 and r.json()["message"].startswith("Uploaded haven-backup-")
        assert len(list((ssh_server.root / "haven-backups").glob("haven-backup-*.tar.gz"))) == 1
        assert c.post("/api/settings/backup/action/nope", headers=ADMIN).status_code == 404
        a.cfg["password"] = "wrong-" + PW
        r = c.post("/api/settings/backup/action/backup_now", headers=ADMIN)
        assert r.status_code == 400 and "denied" in r.json()["detail"] and PW not in r.json()["detail"]
        assert not main.hub.devices or not any(d.startswith("backup:") for d in main.hub.devices)
    finally:
        main.hub.adapters.pop("backup", None)
