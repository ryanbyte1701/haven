"""Aqara sign-in: asks Aqara to send a verification code, then trades it for tokens.

Normally done from Settings > Aqara in the web UI. Command-line fallback:
    docker exec -it haven python -m app.aqara_login
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

from .adapters.aqara import AqaraAdapter
from .config import DATA_DIR


class _NullHub:
    devices: dict = {}


def _adapter(cfg: dict) -> AqaraAdapter:
    missing = [k for k in ("app_id", "app_key", "key_id") if not cfg.get(k)]
    if missing:
        raise ValueError(f"Save {', '.join(missing)} first")
    a = AqaraAdapter(_NullHub(), {**cfg, "access_token": None, "refresh_token": None})
    a.access = None
    return a


async def request_code(cfg: dict, account: str) -> None:
    a = _adapter(cfg)
    try:
        await a._call("config.auth.getAuthCode",
                      {"account": account, "accountType": 0, "accessTokenValidity": "1y"},
                      with_token=False, retry=False)
    finally:
        await a.http.aclose()


async def exchange_code(cfg: dict, account: str, code: str) -> None:
    a = _adapter(cfg)
    try:
        res = await a._call("config.auth.getToken", {"authCode": code, "account": account, "accountType": 0},
                            with_token=False, retry=False)
    finally:
        await a.http.aclose()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / "aqara_token.json"
    from .config import write_private
    write_private(p, json.dumps({"access_token": res["accessToken"], "refresh_token": res["refreshToken"],
                                 "refreshed_at": time.time()}))


async def main() -> int:
    from .settings import effective_config
    cfg = effective_config().get("aqara") or {}
    account = input("Aqara account (email or phone used in the Aqara Home app): ").strip()
    await request_code(cfg, account)
    code = input("Enter the verification code Aqara just sent you: ").strip()
    await exchange_code(cfg, account, code)
    print("Saved. Haven picks the tokens up within 30 seconds.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
