"""Start Haven as your NAS user, after making sure it can write its folders.

The compose files start the container as root with only CHOWN, DAC_OVERRIDE, FOWNER, SETUID and SETGID (plus
NET_ADMIN and NET_RAW in the high-availability file). Within the first milliseconds this launcher:
  1. picks the user: HAVEN_UID/HAVEN_GID, else the owner of the data folder (the one you created in File
     Station) if that isn't root, else the config folder's owner, else 1026:100 (the first DSM admin, group "users");
  2. creates /data and /config if needed and gives everything in them to that user, so a folder Docker
     created as root, or files left by another user ID, can't stop Haven from saving;
  3. switches to that user, keeping NET_ADMIN + NET_RAW only if the container was given them (high
     availability: as permitted, effective, inheritable and ambient capabilities, which survive exec), and
     drops everything else for good, CHOWN, SETUID and SETGID included;
  4. execs `python -m app.serve` (or the command given after `--`).
Started as a normal user (plain `docker run`), it just execs the server. If a step fails it refuses to run
as root.

    python -m app.privdrop [-- command ...]
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys

CAP_SETGID, CAP_SETUID, CAP_NET_ADMIN, CAP_NET_RAW = 6, 7, 12, 13
KEEP = (CAP_NET_ADMIN, CAP_NET_RAW)        # kept only if the container was given them (HA compose file)
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


def permitted() -> int:
    """This process's permitted capability set (from /proc)."""
    try:
        for line in open("/proc/self/status"):
            if line.startswith("CapPrm:"):
                return int(line.split()[1], 16)
    except OSError:
        pass
    return 0


def own_tree(root: str, uid: int, gid: int) -> int:
    """chown everything under root that isn't uid:gid already (symlinks are not followed). Returns how many."""
    n = 0
    if not os.path.isdir(root):
        return 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [dirpath, *(os.path.join(dirpath, f) for f in dirnames + filenames)]:
            try:
                st = os.lstat(name)
                if (st.st_uid, st.st_gid) != (uid, gid):
                    os.lchown(name, uid, gid)
                    n += 1
            except OSError as e:
                print(f"app.privdrop: can't take ownership of {name}: {e}", file=sys.stderr, flush=True)
    return n


def pick_ids(data_dir: str, *more: str) -> tuple[int, int]:
    """HAVEN_UID/HAVEN_GID, else the owner of the data folder (the folder you created in File Station), else the
    owner of the next folder given (the config folder), else 1026:100 (the first DSM admin, group users)."""
    if os.environ.get("HAVEN_UID"):
        return int(os.environ["HAVEN_UID"]), int(os.environ.get("HAVEN_GID") or 100)
    for d in (data_dir, *more):
        try:
            st = os.stat(d)
            if st.st_uid != 0:
                return st.st_uid, st.st_gid
        except OSError:
            pass
    return 1026, int(os.environ.get("HAVEN_GID") or 100)


def pick_groups(gid: int, *folders: str) -> list[int]:
    """Supplementary groups: HAVEN_GROUPS (comma-separated, e.g. "101" for Synology's administrators group: copy
    the numbers from `id`), plus the group of each folder. Synology shares grant access through ACL rules on
    groups such as administrators, so the primary group alone isn't always enough."""
    out = [gid]
    for g in (os.environ.get("HAVEN_GROUPS") or "").replace(" ", ",").split(","):
        if g.strip().isdigit() and int(g) != 0:
            out.append(int(g))
    for d in folders:
        try:
            g = os.stat(d).st_gid
            if g != 0:
                out.append(g)
        except OSError:
            pass
    return list(dict.fromkeys(out))


def writable(d: str) -> str:
    """'' if this process can create, write and delete a file in d (what SQLite needs), else the error."""
    probe = os.path.join(d, f".haven-write-test-{os.getpid()}")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        return ""
    except OSError as e:
        return e.strerror or str(e)


def conf_folder() -> str:
    from .config import CONFIG_PATH
    return os.environ.get("HAVEN_CONF_DIR") or str(CONFIG_PATH.parent)


def prepare(uid: int, gid: int) -> None:
    from .config import DATA_DIR
    for d in dict.fromkeys([str(DATA_DIR), conf_folder()]):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print(f"app.privdrop: can't create {d}: {e}", file=sys.stderr, flush=True)
            continue
        n = own_tree(d, uid, gid)      # no chmod: on Synology it can replace the share's ACL rules
        if n:
            print(f"app.privdrop: gave {n} file(s) in {d} to {uid}:{gid}", file=sys.stderr, flush=True)


def drop(uid: int, gid: int, keep=KEEP, groups: list[int] | None = None) -> None:
    libc = _libc()
    _check(libc.prctl(PR_SET_KEEPCAPS, 1, 0, 0, 0), "prctl(KEEPCAPS)")
    os.setgroups(groups or [gid])
    os.setgid(gid)
    os.setuid(uid)                          # with KEEPCAPS the permitted set survives; effective is cleared
    hdr = _Header(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_Data * 2)()
    m = mask(keep)
    data[0].effective = data[0].permitted = data[0].inheritable = m
    _check(libc.capset(ctypes.byref(hdr), data), "capset")          # also drops SETUID/SETGID for good
    for c in keep:
        _check(libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE, c, 0, 0), f"ambient cap {c}")
    _check(libc.prctl(PR_SET_KEEPCAPS, 0, 0, 0, 0), "prctl(KEEPCAPS off)")


def main(argv: list[str]) -> None:
    cmd = argv[argv.index("--") + 1:] if "--" in argv else []
    cmd = cmd or [sys.executable, "-m", "app.serve"]
    if os.getuid() == 0:
        from .config import DATA_DIR
        uid, gid = pick_ids(str(DATA_DIR), conf_folder())
        if uid == 0:
            sys.exit("app.privdrop: HAVEN_UID must not be 0 (Haven doesn't run as root)")
        prepare(uid, gid)
        groups = pick_groups(gid, str(DATA_DIR), conf_folder())
        have = permitted()
        keep = tuple(c for c in KEEP if have & (1 << c))
        try:
            drop(uid, gid, keep, groups)
        except OSError as e:
            sys.exit(f"app.privdrop: can't switch to {uid}:{gid} ({e}). The compose file needs cap_add: CHOWN, "
                     "DAC_OVERRIDE, FOWNER, SETUID, SETGID and user: \"0:0\". Refusing to run as root.")
        extra = " with NET_ADMIN and NET_RAW" if keep else ""
        print(f"app.privdrop: running as {uid}:{gid} (groups {','.join(map(str, groups))}){extra}",
              file=sys.stderr, flush=True)
        for d in dict.fromkeys([str(DATA_DIR), conf_folder()]):
            err = writable(d)
            if err:
                print(f"app.privdrop: WARNING: user {uid} can't write {d} ({err}). On Synology the shared folder's "
                      "permission rules (ACL) decide, not only the owner: run Haven as your own DSM user. Over SSH run "
                      "`id`, then set HAVEN_UID, HAVEN_GID and HAVEN_GROUPS in the compose file to those numbers "
                      "(e.g. uid=1024 gid=100 groups=100,101 -> HAVEN_UID 1024, HAVEN_GID 100, HAVEN_GROUPS 101).",
                      file=sys.stderr, flush=True)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
