# backend/core/ouroboros/governance/transport/transport_security.py
from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
import tempfile
from typing import Optional, Tuple

from backend.core.ouroboros.governance.transport.transport_config import TransportConfig


class EphemeralMaterialUnavailable(RuntimeError):
    """Raised when ephemeral self-signed material is requested but the
    `cryptography` package is not installed. Loopback-test-only path."""


def generate_ephemeral_material() -> Tuple[str, str]:
    """Generate an in-memory self-signed cert/key pair for loopback
    tests. Returns (cert_path, key_path) under a fresh temp dir.

    NOT for production -- production resolves cert/key/CA from
    TransportConfig paths (which come from env / the golden-image
    metadata pattern in Stage 1).
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise EphemeralMaterialUnavailable(str(exc)) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    tmpdir = tempfile.mkdtemp(prefix="jarvis-ws-tls-")
    cert_path = os.path.join(tmpdir, "cert.pem")
    key_path = os.path.join(tmpdir, "key.pem")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    return cert_path, key_path


def _resolve_material(cfg: TransportConfig) -> Tuple[str, str, Optional[str]]:
    """Return (cert_path, key_path, ca_path). Ephemeral when requested;
    otherwise the env-resolved paths from cfg."""
    if cfg.tls_ephemeral:
        cert, key = generate_ephemeral_material()
        return cert, key, cfg.tls_ca
    if not cfg.tls_cert or not cfg.tls_key:
        raise EphemeralMaterialUnavailable(
            "tls_enabled but no cert/key resolved from env "
            "(JARVIS_BRAIN_WS_TLS_CERT / _KEY) and tls_ephemeral is false"
        )
    return cfg.tls_cert, cfg.tls_key, cfg.tls_ca


def build_server_ssl_context(cfg: TransportConfig) -> Optional[ssl.SSLContext]:
    if not cfg.tls_enabled:
        return None
    cert, key, ca = _resolve_material(cfg)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS
    if ca:
        ctx.load_verify_locations(cafile=ca)
    elif cfg.tls_ephemeral:
        # Loopback: trust our own ephemeral cert as the peer CA so the
        # two ends of a self-signed handshake verify each other.
        ctx.load_verify_locations(cafile=cert)
    return ctx


def build_client_ssl_context(cfg: TransportConfig) -> Optional[ssl.SSLContext]:
    if not cfg.tls_enabled:
        return None
    cert, key, ca = _resolve_material(cfg)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    if ca:
        ctx.load_verify_locations(cafile=ca)
        ctx.check_hostname = True
    elif cfg.tls_ephemeral:
        ctx.load_verify_locations(cafile=cert)
        ctx.check_hostname = False  # self-signed loopback
    return ctx
