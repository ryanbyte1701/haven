"""Start Haven as a normal user that keeps exactly two capabilities: NET_ADMIN and NET_RAW.

Used by deploy/synology/docker-compose.ha.yml (high availability). Docker only gives `cap_add`
capabilities to root; a container started with `user: "1026:100"` gets none of them, and
`no-new-privileges` (which we keep) also blocks file capabilities (`setcap` on /usr/sbin/ip). So the HA
container starts as root with only NET_ADMIN, NET_RAW, SETUID and SETGID, and this launcher, within the
first milliseconds:
  1. switches to HAVEN_UID:HAVEN_GID (default 1026:100, the owner of the data folder),
  2. keeps NET_ADMIN + NET_RAW as permitted, effective, inheritable and *ambient* capabilities (ambient
     ones survive exec, so the Python server and the `ip` command it runs have them), and drops
     everything else for good, SETUID and SETGID included,
  3. execs `python -m app.serve` (or the command given after `--`).
If any step fails it refuses to start rather than run as root.

    python -m app.privdrop [-- command ...]
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys

CAP_SETGID, CAP_SETUID, CAP_NET_ADMIN, CAP_NET_RAW = 6, 7, 12, 13
KEEP = (CAP_NET_ADMIN, CAP_NET_RAW)
PR_SET_KEEPCAPS, PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE = 8, 47, 2
LINUX_CAPABILITY_VERSION_3 = 0x20080522


class _Header(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _Data(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]


def mask(caps) -> int:
    m = 0
    for c in caps:
        m |= 1 << c
    return m


def _libc():
    libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    return libc


def _check(rc: int, what: str) -> None:
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"{what}: {os.strerror(e)}")


def drop(uid: int, gid: int) -> None:
    libc = _libc()
    _check(libc.prctl(PR_SET_KEEPCAPS, 1, 0, 0, 0), "prctl(KEEPCAPS)")
    os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)                          # with KEEPCAPS the permitted set survives; effective is cleared
    hdr = _Header(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_Data * 2)()
    m = mask(KEEP)
    data[0].effective = data[0].permitted = data[0].inheritable = m
    _check(libc.capset(ctypes.byref(hdr), data), "capset")          # also drops SETUID/SETGID for good
    for c in KEEP:
        _check(libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE, c, 0, 0), f"ambient cap {c}")
    _check(libc.prctl(PR_SET_KEEPCAPS, 0, 0, 0, 0), "prctl(KEEPCAPS off)")


def main(argv: list[str]) -> None:
    cmd = argv[argv.index("--") + 1:] if "--" in argv else []
    cmd = cmd or [sys.executable, "-m", "app.serve"]
    if os.getuid() == 0:
        uid, gid = int(os.environ.get("HAVEN_UID", "1026")), int(os.environ.get("HAVEN_GID", "100"))
        if uid == 0:
            sys.exit("app.privdrop: HAVEN_UID must not be 0 (Haven doesn't run as root)")
        try:
            drop(uid, gid)
        except OSError as e:
            sys.exit(f"app.privdrop: can't switch to {uid}:{gid} keeping NET_ADMIN/NET_RAW ({e}). The HA compose "
                     "file needs cap_add: NET_ADMIN, NET_RAW, SETUID, SETGID and user: \"0:0\". Refusing to run as root.")
        print(f"app.privdrop: running as {uid}:{gid} with NET_ADMIN and NET_RAW only", file=sys.stderr, flush=True)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
