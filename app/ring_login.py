"""Ring sign-in from the command line (normally done from Settings > Ring in the web UI).
    docker exec -it haven python -m app.ring_login
"""
import asyncio
import getpass
import sys

from .adapters.ring import login


async def main() -> int:
    email = input("Ring email: ").strip()
    pw = getpass.getpass("Ring password: ")
    res = await login(email, pw)
    if res.get("need_code"):
        res = await login(email, pw, input("Verification code: ").strip())
    print("Signed in. Haven picks it up within 10 seconds." if res.get("ok") else res)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
