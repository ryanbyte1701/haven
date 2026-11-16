import asyncio
from cryptography import x509
from app.serve import self_signed, https_redirect


def test_self_signed_has_extra_names(tmp_path):
    cert, key = self_signed(tmp_path / "tls", "haven.home,192.168.1.20")
    c = x509.load_pem_x509_certificate(cert.read_bytes())
    san = str(c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
    assert "haven.home" in san and "192.168.1.20" in san and "localhost" in san
    assert (key.stat().st_mode & 0o777) == 0o600
    assert self_signed(tmp_path / "tls") == (cert, key)          # reused, not regenerated


def _call(app, path, host=b"nas:1701"):
    sent, called = [], []
    async def inner(scope, receive, send): called.append(scope["path"])
    async def send(m): sent.append(m)
    scope = {"type": "http", "path": path, "query_string": b"a=1", "headers": [(b"host", host)]}
    asyncio.run(https_redirect(inner, 1702)(scope, None, send))
    return sent, called


def test_redirect_and_exemptions():
    sent, called = _call(None, "/settings")
    assert sent[0]["status"] == 308 and dict(sent[0]["headers"])[b"location"] == b"https://nas:1702/settings?a=1"
    for p in ("/data/report", "/metrics", "/api/health"):
        sent, called = _call(None, p)
        assert called == [p] and not sent
