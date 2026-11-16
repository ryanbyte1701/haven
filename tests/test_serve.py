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


def test_redirect_hosts_and_paths():
    def loc(host, raw=b"/x", qs=b""):
        sent = []
        async def send(m): sent.append(m)
        scope = {"type": "http", "path": raw.decode("latin-1"), "raw_path": raw, "query_string": qs, "headers": [(b"host", host)]}
        asyncio.run(https_redirect(None, 1802)(scope, None, send))
        return dict(sent[0]["headers"])[b"location"].decode()
    assert loc(b"[::1]") == "https://[::1]:1802/x"
    assert loc(b"[fe80::1]:1701") == "https://[fe80::1]:1802/x"
    assert loc(b"nas.local:1701", b"/a%20b") == "https://nas.local:1802/a%20b"
    assert loc(b"a@evil.com") == "https://localhost:1802/x"
    assert loc(b"nas", b"/x\r\nSet-Cookie:1") == "https://nas:1802/"


def test_tls_unwritable_data_dir_does_not_crash(tmp_path, monkeypatch):
    from app import serve
    ro = tmp_path / "ro"; ro.mkdir(); ro.chmod(0o500)
    monkeypatch.setenv("HAVEN_DATA", str(ro))
    monkeypatch.delenv("HAVEN_TLS_CERT", raising=False); monkeypatch.delenv("HAVEN_TLS_KEY", raising=False)
    import os
    if os.geteuid() == 0:
        monkeypatch.setattr(serve, "self_signed", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    assert serve._tls_files() is None
    monkeypatch.setenv("HAVEN_TLS_CERT", "/nonexistent.pem")
    assert serve._tls_files() is None                        # only one set
