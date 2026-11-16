"""HTTP endpoints for sign-in, the signed-in user's account and (admins) user management.

Who may call what is decided by the middleware in main.py using auth.allowed(); the endpoints here
only add the checks that depend on the request body or on the caller being a real (non-guest) user.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import auth
from . import security as sec

router = APIRouter()
guest_limiter = sec.RateLimiter(rate=0.2, burst=10)       # viewer sessions per address
_sems: dict = {}


def _hashing() -> asyncio.Semaphore:
    """scrypt is CPU-heavy: at most two at a time (one semaphore per event loop)."""
    loop = asyncio.get_running_loop()
    return _sems.setdefault(id(loop), asyncio.Semaphore(2))


def _m():
    from . import main      # late import: main includes this router
    return main


def _principal(request: Request) -> dict:
    p = getattr(request.state, "principal", None)
    if not p:
        raise HTTPException(401, "unauthorized")
    return p


def _session(user: str, role: str, guest: bool = False) -> dict:
    return {"token": auth.store.new_session(user, role, guest), "user": user, "role": role, "guest": guest}


class Credentials(BaseModel):
    username: str = ""
    password: str = ""


class Setup(Credentials):
    code: str = ""
    guest_view: bool = False


class PasswordChange(BaseModel):
    current: str = ""
    new: str = ""


class NewUser(BaseModel):
    name: str = ""
    password: str = ""
    role: str = "user"


class UserPatch(BaseModel):
    role: str | None = None
    password: str | None = None


class Toggle(BaseModel):
    enabled: bool


# ---------------- public ----------------
@router.get("/api/auth/state")
async def auth_state(request: Request):
    lan = auth.is_private_ip(_m()._client_ip(request))
    return {"setup_needed": not auth.store.has_users, "guest_view": auth.store.guest_view and lan}


@router.post("/api/auth/setup")
async def auth_setup(body: Setup, request: Request):
    """First run without DASH_TOKEN: create the admin account. Only while there are no users, only from
    the LAN, and only with the one-time setup code from the container log."""
    if auth.store.has_users:
        raise HTTPException(409, "Haven is already set up. Sign in instead.")
    m = _m()
    ip = m._client_ip(request)
    if not auth.is_private_ip(ip):
        raise HTTPException(403, "Create the admin account from a device on your home network.")
    if m.auth_limiter.flooding(ip):
        raise HTTPException(429, "Too many wrong codes. Try again shortly.")
    if not auth.store.check_setup_code(body.code):
        await m.auth_limiter.punish(ip)
        raise HTTPException(400, "That setup code isn't right. Find it in the container log "
                                 "(Container Manager → haven → Log); it changes when Haven restarts.")
    m.auth_limiter.ok(ip)
    try:
        name = auth.store.finish_setup(body.username, body.password, body.guest_view)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except PermissionError:
        raise HTTPException(409, "Haven is already set up. Sign in instead.")
    return _session(name, "admin")


@router.post("/api/auth/login")
async def auth_login(body: Credentials, request: Request):
    m = _m()
    ip = m._client_ip(request)
    if m.auth_limiter.flooding(ip):          # only while many wrong guesses from here are waiting at once
        return JSONResponse({"detail": "Too many wrong passwords. Try again shortly."}, status_code=429,
                            headers={"Retry-After": "5"})
    name = (body.username or "").strip().lower()[:64]
    async with _hashing():
        role = await asyncio.to_thread(auth.store.verify, name, body.password[:1024])
    if not role:
        await m.auth_limiter.punish(ip)       # checked first, so the right password is never locked out
        raise HTTPException(401, "Wrong username or password")
    m.auth_limiter.ok(ip)
    return _session(name, role)


@router.post("/api/auth/guest")
async def auth_guest(request: Request):
    ip = _m()._client_ip(request)
    if not auth.store.guest_view:
        raise HTTPException(403, "Viewing without signing in is turned off. Sign in instead.")
    if not auth.is_private_ip(ip):
        raise HTTPException(403, "Viewing without signing in only works on your home network. Sign in instead.")
    if not guest_limiter.allow(ip):
        raise HTTPException(429, "slow down")
    return _session(auth.GUEST_USER, "monitor", guest=True)


# ---------------- signed in ----------------
@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    auth.store.revoke(_m()._request_token(request))
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request):
    p = _principal(request)
    return {"user": p["user"], "role": p["role"], "role_label": auth.ROLE_LABEL[p["role"]], "guest": p["guest"],
            "dash_token": bool(p.get("dash_token"))}


@router.post("/api/me/password")
async def change_my_password(body: PasswordChange, request: Request):
    p = _principal(request)
    if p["guest"] or p.get("dash_token") or p["user"] not in auth.store.users:
        raise HTTPException(400, "Sign in with your own account to change its password.")
    m = _m()
    ip = m._client_ip(request)
    async with _hashing():
        ok = await asyncio.to_thread(auth.store.verify, p["user"], body.current[:1024])
    if not ok:
        await m.auth_limiter.punish(ip)
        raise HTTPException(400, "Your current password isn't right")
    if body.new == body.current:
        raise HTTPException(400, "Pick a password different from the current one")
    try:
        auth.store.set_password(p["user"], body.new)      # signs out every device...
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _session(p["user"], auth.store.users[p["user"]]["role"])   # ...except this one


# ---------------- admin: users & access ----------------
def _users_view() -> dict:
    return {"users": auth.store.list_users(), "guest_view": auth.store.guest_view, "roles": auth.ROLE_LABEL}


@router.get("/api/users")
async def list_users():
    return _users_view()


@router.post("/api/users")
async def add_user(body: NewUser):
    try:
        auth.store.create_user(body.name, body.password, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _users_view()


@router.patch("/api/users/{name}")
async def patch_user(name: str, body: UserPatch):
    if name not in auth.store.users:
        raise HTTPException(404, "No such user")
    try:
        if body.password is not None:
            auth.store.set_password(name, body.password)
        if body.role is not None:
            auth.store.set_role(name, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _users_view()


@router.delete("/api/users/{name}")
async def delete_user(name: str):
    if name not in auth.store.users:
        raise HTTPException(404, "No such user")
    try:
        auth.store.delete_user(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _users_view()


@router.put("/api/auth/guest_view")
async def set_guest_view(body: Toggle):
    auth.store.set_guest_view(body.enabled)
    return _users_view()
