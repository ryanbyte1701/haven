import os
import tempfile

import pytest

# Never touch the real /data: every module reads HAVEN_DATA at import time, so set it before any app import.
os.environ["HAVEN_DATA"] = tempfile.mkdtemp(prefix="haven-test-data-")

# The API needs a signed-in user. Tests act as admin through the DASH_TOKEN bearer (still accepted for
# scripts and Prometheus): use the `ADMIN` headers or `TestClient(app, headers=ADMIN)`.
os.environ.setdefault("DASH_TOKEN", "test-admin-password")
ADMIN = {"authorization": f"Bearer {os.environ['DASH_TOKEN']}"}


@pytest.fixture(autouse=True)
def _auth_store(tmp_path_factory, monkeypatch):
    """Every test gets its own empty users/sessions folder (never the real /data)."""
    from app import auth
    d = tmp_path_factory.mktemp("auth")
    monkeypatch.setattr(auth, "store", auth.AuthStore(d))
    yield auth.store


@pytest.fixture
def admin_headers():
    return dict(ADMIN)
