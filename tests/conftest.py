import os
import tempfile

import pytest

# Never touch the real /data: every module reads HAVEN_DATA at import time, so set it before any app import.
os.environ["HAVEN_DATA"] = tempfile.mkdtemp(prefix="haven-test-data-")

# The API needs a signed-in user. Every test gets its own auth store with an admin account "admin"
# (password ADMIN_PASSWORD) and a ready session ADMIN_TOKEN: use the `ADMIN` headers,
# `TestClient(app, headers=ADMIN)` or the admin_headers fixture.
ADMIN_PASSWORD = "test-admin-password"
ADMIN_TOKEN = "test-admin-session-token-0123456789abcdef"
ADMIN = {"authorization": f"Bearer {ADMIN_TOKEN}"}


def add_admin(store, name: str = "admin", password: str = ADMIN_PASSWORD, token: str = ADMIN_TOKEN) -> dict:
    """Create an admin (not must_change) with a known session token in `store`; returns its headers."""
    import time
    from app import auth
    if name not in store.users:
        store.create_user(name, password, "admin")
    now = time.time()
    store.sessions[auth._digest(token)] = {"user": name, "role": "admin", "guest": False, "created": now, "last_seen": now}
    store._save_sessions()
    return {"authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _auth_store(tmp_path_factory, monkeypatch):
    """Every test gets its own users/sessions folder (never the real /data) with the test admin in it."""
    from app import auth
    d = tmp_path_factory.mktemp("auth")
    monkeypatch.setattr(auth, "store", auth.AuthStore(d))
    add_admin(auth.store)
    yield auth.store


@pytest.fixture
def admin_headers():
    return dict(ADMIN)
