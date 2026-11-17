"""app.privdrop: pick the NAS user, give it the config and data folders, drop root for good."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import privdrop as P

ROOT = os.getuid() == 0
needs_root = pytest.mark.skipif(not ROOT, reason="needs root (the container starts as root)")
REPO = Path(__file__).resolve().parent.parent


# ---------------- pick_ids ----------------
def test_pick_ids_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HAVEN_UID", "1030")
    monkeypatch.setenv("HAVEN_GID", "101")
    assert P.pick_ids(str(tmp_path)) == (1030, 101)
    monkeypatch.delenv("HAVEN_GID")
    assert P.pick_ids(str(tmp_path)) == (1030, 100)          # group "users" by default


@needs_root
def test_pick_ids_from_folder_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("HAVEN_UID", raising=False)
    monkeypatch.delenv("HAVEN_GID", raising=False)
    data, conf = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    conf.mkdir()
    os.chown(data, 1027, 101)
    assert P.pick_ids(str(data), str(conf)) == (1027, 101)
    os.chown(data, 0, 0)                                      # Docker created data/ as root...
    os.chown(conf, 1031, 100)                                 # ...but the config folder is the user's
    assert P.pick_ids(str(data), str(conf)) == (1031, 100)
    os.chown(conf, 0, 0)
    assert P.pick_ids(str(data), str(conf)) == (1026, 100)    # both root: the first DSM admin
    assert P.pick_ids(str(tmp_path / "missing")) == (1026, 100)


# ---------------- own_tree ----------------
@needs_root
def test_own_tree_chowns_everything_but_follows_no_links(tmp_path):
    root = tmp_path / "data"
    (root / "tls").mkdir(parents=True)
    (root / "history.db").write_text("x")
    (root / "tls" / "key.pem").write_text("k")
    outside = tmp_path / "outside.txt"
    outside.write_text("not ours")
    os.symlink(outside, root / "link")
    n = P.own_tree(str(root), 1026, 100)
    assert n == 5                                             # data, tls, history.db, link (itself), key.pem
    for p in (root, root / "tls", root / "history.db", root / "tls" / "key.pem"):
        st = os.stat(p)
        assert (st.st_uid, st.st_gid) == (1026, 100), p
    assert os.lstat(root / "link").st_uid == 1026
    assert os.stat(outside).st_uid == 0                       # the link target is untouched
    assert P.own_tree(str(root), 1026, 100) == 0              # nothing left to change
    assert P.own_tree(str(tmp_path / "missing"), 1026, 100) == 0


# ---------------- prepare ----------------
@needs_root
def test_prepare_creates_and_gives_both_folders(tmp_path, monkeypatch):
    import app.config as C
    data, conf = tmp_path / "data", tmp_path / "config"
    conf.mkdir()
    (conf / "settings.json").write_text("{}")
    monkeypatch.setattr(C, "DATA_DIR", data)
    monkeypatch.setattr(C, "CONFIG_PATH", conf / "config.yaml")
    monkeypatch.delenv("HAVEN_CONF_DIR", raising=False)
    P.prepare(1026, 100)
    for d in (data, conf):
        st = os.stat(d)
        assert d.is_dir() and (st.st_uid, st.st_gid) == (1026, 100) and st.st_mode & 0o777 == 0o700
    assert os.stat(conf / "settings.json").st_uid == 1026


def test_prepare_survives_an_unusable_folder(tmp_path, monkeypatch, capsys):
    import app.config as C
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(C, "DATA_DIR", blocker / "data")          # can't be created (parent is a file)
    monkeypatch.setenv("HAVEN_CONF_DIR", str(blocker / "config"))
    P.prepare(os.getuid(), os.getgid())                            # reports, doesn't raise
    assert "can't create" in capsys.readouterr().err


# ---------------- drop ----------------
@needs_root
@pytest.mark.parametrize("keep", [(), P.KEEP])
def test_drop_switches_user_and_keeps_only_net_caps(keep):
    if keep and (P.permitted() & P.mask(P.KEEP)) != P.mask(P.KEEP):
        pytest.skip("this runner doesn't have NET_ADMIN/NET_RAW")
    code = ("import os; from app import privdrop as P\n"
            f"P.drop(1026, 100, {tuple(keep)!r})\n"
            "def caps(f):\n"
            "    return next(int(l.split()[1], 16) for l in open('/proc/self/status') if l.startswith(f + ':'))\n"
            "msg = f\"{os.getuid()} {os.geteuid()} {os.getgid()} {os.getgroups()} {caps('CapEff')} {caps('CapPrm')} {caps('CapAmb')}\"\n"
            "try:\n    os.setuid(0)\n    msg += ' regained-root'\nexcept PermissionError:\n    pass\n"
            "print(msg)\n")
    res = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=30)
    out = res.stdout.strip() if res.returncode == 0 else f"error {res.stderr}"
    assert not out.startswith("error"), out
    uid, euid, gid, groups, eff, prm, amb = out.split()[:7]
    assert (uid, euid, gid) == ("1026", "1026", "100"), out
    assert groups == "[100]" and "regained-root" not in out
    assert int(eff) == int(prm) == int(amb) == P.mask(keep), out


@needs_root
def test_main_runs_the_command_as_the_folder_owner(tmp_path):
    data, conf = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    conf.mkdir()
    os.chown(data, 1032, 100)
    (conf / "users.json").write_text("{}")                          # left by root: must become the user's
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HAVEN_DATA": str(data),
           "HAVEN_CONFIG": str(conf / "config.yaml"), "HOME": "/tmp"}
    out = subprocess.run([sys.executable, "-m", "app.privdrop", "--", sys.executable, "-c",
                          "import os; print(os.getuid(), os.getgid())"],
                         cwd=REPO, env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["1032", "100"]
    assert "running as 1032:100" in out.stderr
    assert os.stat(conf / "users.json").st_uid == 1032


@needs_root
def test_main_refuses_uid_0(tmp_path):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HAVEN_DATA": str(tmp_path), "HAVEN_UID": "0",
           "HAVEN_CONFIG": str(tmp_path / "config.yaml"), "HOME": "/tmp"}
    out = subprocess.run([sys.executable, "-m", "app.privdrop", "--", "true"], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode != 0 and "must not be 0" in out.stderr


def test_not_root_just_execs(tmp_path):
    """Started as a normal user (plain docker run, USER 1000:1000), privdrop only runs the command."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HAVEN_DATA": str(tmp_path), "HOME": "/tmp"}
    code = "import os, runpy, sys; os.setgid(100) if os.getuid() == 0 else None; " \
           "os.setuid(1026) if os.getuid() == 0 else None; sys.argv = ['x', '--', 'id', '-u']; " \
           "runpy.run_module('app.privdrop', run_name='__main__')"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ("1026" if ROOT else str(os.getuid())) and "running as" not in out.stderr
