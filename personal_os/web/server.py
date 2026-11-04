"""Running the dashboard inside the daemon: the laptop listener on 127.0.0.1, and the phone
listener (HTTPS, self-signed certificate made on this laptop) only when you've turned it on."""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
import uvicorn

from personal_os.web.app import WebState, create_app


class QuietServer(uvicorn.Server):
    """The daemon owns Ctrl+C and shutdown; uvicorn must not install its own signal handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def lan_addresses() -> list[str]:
    """This laptop's private IPv4 addresses (Wi-Fi, Ethernet), most likely first."""
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        stats = psutil.net_if_stats().get(name)
        if stats is not None and not stats.isup:
            continue
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = ipaddress.ip_address(a.address)
            if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                out.append(
                    (0 if name.lower().startswith(("wi-fi", "wlan", "ethernet")) else 1, a.address)
                )
    return [ip for _, ip in sorted(out)]


def ensure_certificate(tls_dir: Path, addresses: list[str]) -> tuple[Path, Path]:
    """A self-signed certificate for this laptop's LAN addresses; made again if they change."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_path, key_path = tls_dir / "cert.pem", tls_dir / "key.pem"
    wanted = sorted({*addresses, "127.0.0.1"})
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = sorted(str(ip) for ip in san.get_values_for_type(x509.IPAddress))
        if have == wanted and cert.not_valid_after_utc > datetime.now(UTC) + timedelta(days=30):
            return cert_path, key_path
    tls_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Personal OS (this laptop)")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(a)) for a in wanted]
                + [x509.DNSName(socket.gethostname())]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def local_server(state: WebState) -> QuietServer:
    cfg = state.config.api
    config = uvicorn.Config(
        create_app(state),
        host=cfg.host,
        port=cfg.port,
        log_level="warning",
        log_config=None,  # the daemon has no console (pythonw): use its own logging
        access_log=False,
        lifespan="off",
    )
    return QuietServer(config)


def lan_server(state: WebState) -> QuietServer | None:
    lan = state.config.api.lan
    addresses = state.lan_addresses()
    if not lan.enabled or not addresses:
        return None
    cert, key = ensure_certificate(state.config.data_dir / "tls", addresses)
    config = uvicorn.Config(
        create_app(state, lan=True),
        host=addresses[0],
        port=lan.port,
        log_level="warning",
        log_config=None,  # the daemon has no console (pythonw): use its own logging
        access_log=False,
        lifespan="off",
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
    )
    return QuietServer(config)
