"""Start Haven's web server: plain HTTP and, optionally, HTTPS on a second port.

Environment (all optional):
  HAVEN_PORT          HTTP port inside the container (default 1701). Ecowitt gateways and
                      Prometheus can only use plain HTTP, so this always stays on.
  HAVEN_HTTPS_PORT    HTTPS port (default 1702). Set to 0 to turn HTTPS off.
  HAVEN_TLS_CERT      Certificate (PEM, full chain) and private key (PEM). Leave both unset to
  HAVEN_TLS_KEY       use a self-signed certificate Haven creates in /data/tls on first start.
  HAVEN_TLS_HOSTS     Extra names/IPs for the self-signed certificate, comma separated
                      (e.g. "haven.home,192.168.1.20"). Delete /data/tls to regenerate.
  HAVEN_HTTPS_PUBLIC_PORT  Port browsers should be sent to when the NAS maps HTTPS to another
                      number (e.g. "1802:1702" -> 1802). Default: HAVEN_HTTPS_PORT.
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
        try:
            from cryptography import x509
            c = x509.load_pem_x509_certificate(cert.read_bytes())
            left = c.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
            if left > dt.timedelta(days=30):
                return cert, key
            log.warning("The self-signed HTTPS certificate expires in %d days: making a new one", left.days)
        except Exception as e:  # noqa: BLE001  (unreadable/corrupt: make a new one)
            log.warning("Can't read %s (%s): making a new certificate", cert, e)
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


def _redirect_host(raw: bytes) -> str:
    """Hostname from a Host header, IPv6-aware; anything that isn't a plain host becomes 'localhost'."""
    h = raw.decode("latin-1").strip()
    if h.startswith("["):                               # [v6] or [v6]:port
        end = h.find("]")
        name = h[1:end] if end > 0 else ""
        try:
            ipaddress.IPv6Address(name)
            return f"[{name}]"
        except ValueError:
            return "localhost"
    name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if name and len(name) <= 253 and all(c.isalnum() or c in "-." for c in name):
        return name
    return "localhost"


def https_redirect(app, https_port: int):
    """ASGI wrapper for the HTTP listener: send browsers to HTTPS, keep device/metrics paths."""
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket") and not scope["path"].startswith(HTTP_EXEMPT):
            host = _redirect_host(dict(scope["headers"]).get(b"host", b""))
            port = "" if https_port == 443 else f":{https_port}"
            raw = scope.get("raw_path") or scope["path"].encode("utf-8")
            path = raw.decode("latin-1") if raw.startswith(b"/") and not any(c < 0x21 or c > 0x7e for c in raw) else "/"
            qs = scope.get("query_string", b"")
            qs = qs.decode("latin-1") if not any(c < 0x21 or c > 0x7e for c in qs) else ""
            url = f"https://{host}{port}{path}" + (f"?{qs}" if qs else "")
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", url.encode("latin-1")), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await app(scope, receive, send)
    return wrapped


def _tls_files():
    """(cert, key) to use, or None (logged) when HTTPS can't start; never stops the HTTP side."""
    cert, key = os.environ.get("HAVEN_TLS_CERT"), os.environ.get("HAVEN_TLS_KEY")
    if bool(cert) != bool(key):
        log.error("HTTPS is off: set both HAVEN_TLS_CERT and HAVEN_TLS_KEY (only one is set)")
        return None
    if cert and key:
        for f in (cert, key):
            try:
                with open(f, "rb"):
                    pass
            except OSError as e:
                log.error("HTTPS is off: can't read %s (%s). Check the file is mounted and readable "
                          "by the container user.", f, e.strerror)
                return None
        return cert, key
    data = Path(os.environ.get("HAVEN_DATA", "/data"))
    try:
        return self_signed(data / "tls", os.environ.get("HAVEN_TLS_HOSTS", ""))
    except OSError as e:
        log.error("HTTPS is off: can't create the certificate in %s (%s). The data folder must be writable "
                  "by the container user (Synology: the user: line in docker-compose.yml, usually 1026:100).",
                  data / "tls", e.strerror or e)
        return None


async def serve() -> None:
    from app.main import app

    host = os.environ.get("HAVEN_HOST", "0.0.0.0")  # noqa: S104 - a server for the LAN: listening on every interface is the point
    http_port = _env_int("HAVEN_PORT", 1701)
    https_port = _env_int("HAVEN_HTTPS_PORT", 1702)
    common = dict(host=host, proxy_headers=True, log_level=os.environ.get("HAVEN_LOG_LEVEL", "info"),
                  limit_concurrency=_env_int("HAVEN_MAX_CONNECTIONS", 200))

    servers = []
    tls = _tls_files() if https_port else None
    if https_port and not tls:
        https_port = 0                                    # problem already logged: run HTTP only
    if tls:
        # Only the HTTP server runs the app's startup/shutdown, so adapters start once.
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            fp = x509.load_pem_x509_certificate(Path(tls[0]).read_bytes()).fingerprint(hashes.SHA256()).hex(":").upper()  # noqa: ASYNC240 - once, at startup
            log.info("HTTPS on port %d. Certificate SHA-256 fingerprint: %s (compare it with what your browser "
                        "shows before accepting a self-signed certificate)", https_port, fp)
        except Exception:  # noqa: BLE001
            pass
        servers.append(uvicorn.Server(uvicorn.Config(app, port=https_port, ssl_certfile=str(tls[0]),
                                                     ssl_keyfile=str(tls[1]), lifespan="off", **common)))
    public = _env_int("HAVEN_HTTPS_PUBLIC_PORT", https_port)
    http_app = https_redirect(app, public) if https_port and os.environ.get("HAVEN_HTTPS_ONLY") == "1" else app
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
    async def https_after_startup(s):
        # don't take HTTPS requests before the app (adapters, settings) has finished starting
        while not servers[0].started and not servers[0].should_exit:
            await asyncio.sleep(0.1)
        if not servers[0].should_exit:
            await s.serve()
    await asyncio.gather(servers[0].serve(), *(https_after_startup(s) for s in servers[1:]))


if __name__ == "__main__":
    asyncio.run(serve())
