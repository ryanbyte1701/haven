"""Start Haven's web server: plain HTTP and, optionally, HTTPS on a second port.

Environment (all optional):
  HAVEN_PORT          HTTP port inside the container (default 1701). Ecowitt gateways and
                      Prometheus can only use plain HTTP, so this always stays on.
  HAVEN_HTTPS_PORT    HTTPS port (default 1702). Set to 0 to turn HTTPS off.
  HAVEN_TLS_CERT      Certificate (PEM, full chain) and private key (PEM). Leave both unset to
  HAVEN_TLS_KEY       use a self-signed certificate Haven creates in /data/tls on first start.
  HAVEN_TLS_HOSTS     Extra names/IPs for the self-signed certificate, comma separated
                      (e.g. "haven.home,192.168.1.20"). Delete /data/tls to regenerate.
  HAVEN_HTTPS_ONLY    1 = the HTTP port redirects browsers to HTTPS. /data/report (Ecowitt),
                      /metrics and /api/health still answer over HTTP.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import logging
import os
import socket
from pathlib import Path

import uvicorn

log = logging.getLogger("haven.serve")
HTTP_EXEMPT = ("/data/report", "/metrics", "/api/health")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def self_signed(dir_: Path, extra_hosts: str = "") -> tuple[Path, Path]:
    """Create (once) a self-signed certificate valid for this host's names and addresses."""
    cert, key = dir_ / "cert.pem", dir_ / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    names: list[x509.GeneralName] = []
    seen: set[str] = set()

    def add(h: str) -> None:
        h = h.strip()
        if not h or h in seen:
            return
        seen.add(h)
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            names.append(x509.DNSName(h))

    for h in ["localhost", "127.0.0.1", socket.gethostname(), *extra_hosts.split(",")]:
        add(h)
    pk = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Haven"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Haven (self-signed)")])
    now = dt.datetime.now(dt.timezone.utc)
    crt = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
           .public_key(pk.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=825))
           .add_extension(x509.SubjectAlternativeName(names), critical=False)
           .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
           .sign(pk, hashes.SHA256()))
    dir_.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        key.write_bytes(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
        cert.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
    finally:
        os.umask(old)
    log.warning("Created a self-signed HTTPS certificate in %s (browsers will ask you to accept it once)", dir_)
    return cert, key


def https_redirect(app, https_port: int):
    """ASGI wrapper for the HTTP listener: send browsers to HTTPS, keep device/metrics paths."""
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket") and not scope["path"].startswith(HTTP_EXEMPT):
            host = dict(scope["headers"]).get(b"host", b"").decode("latin-1").rsplit(":", 1)[0] or "localhost"
            port = "" if https_port == 443 else f":{https_port}"
            qs = scope.get("query_string", b"").decode("latin-1")
            url = f"https://{host}{port}{scope['path']}" + (f"?{qs}" if qs else "")
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", url.encode("latin-1")), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await app(scope, receive, send)
    return wrapped


async def serve() -> None:
    from app.main import app

    host = os.environ.get("HAVEN_HOST", "0.0.0.0")
    http_port = _env_int("HAVEN_PORT", 1701)
    https_port = _env_int("HAVEN_HTTPS_PORT", 1702)
    common = dict(host=host, proxy_headers=True, log_level=os.environ.get("HAVEN_LOG_LEVEL", "info"))

    servers = []
    if https_port:
        cert, key = os.environ.get("HAVEN_TLS_CERT"), os.environ.get("HAVEN_TLS_KEY")
        if not (cert and key):
            cert, key = self_signed(Path(os.environ.get("HAVEN_DATA", "/data")) / "tls",
                                    os.environ.get("HAVEN_TLS_HOSTS", ""))
        # Only the HTTP server runs the app's startup/shutdown, so adapters start once.
        servers.append(uvicorn.Server(uvicorn.Config(app, port=https_port, ssl_certfile=str(cert),
                                                     ssl_keyfile=str(key), lifespan="off", **common)))
    http_app = https_redirect(app, https_port) if https_port and os.environ.get("HAVEN_HTTPS_ONLY") == "1" else app
    servers.insert(0, uvicorn.Server(uvicorn.Config(http_app, port=http_port, lifespan="on", **common)))
    # One Ctrl-C / docker stop shuts both listeners down.
    import signal
    loop = asyncio.get_running_loop()
    def stop(*_):
        for s in servers:
            s.should_exit = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError):
            pass
    for s in servers:                      # keep uvicorn from replacing our handlers
        s.install_signal_handlers = lambda: None
        if hasattr(s, "capture_signals"):
            import contextlib
            s.capture_signals = contextlib.nullcontext
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(serve())
