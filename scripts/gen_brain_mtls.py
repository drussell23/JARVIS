#!/usr/bin/env python3
"""gen_brain_mtls.py -- Stage-1 Brain-VM mTLS cert-chain generator.

Emits a proper CA-signed 3-cert chain for the cross-host Brain<->Mac mTLS
WebSocket boundary that Stage-1 Task-5 ignition (``scripts/ignite_brain_vm.py``)
brings up:

    ca.pem            self-signed root (CA=True, keyCertSign+cRLSign)
    server-cert.pem   SERVER leaf (serverAuth EKU) -> injected into the Brain VM
                      via instance metadata by the ignition driver
    server-key.pem    SERVER private key            (0600)
    client-cert.pem   CLIENT leaf (clientAuth EKU) -> stays on THIS Mac
    client-key.pem    CLIENT private key            (0600)

These files are consumed UNCHANGED by the Stage-0
``transport_security.build_server_ssl_context`` / ``build_client_ssl_context``
loaders (they read ``JARVIS_BRAIN_WS_TLS_CERT``/``_KEY``/``_CA`` from the env and
enforce ``CERT_REQUIRED`` mutual auth). We feed those loaders -- we do NOT
reimplement TLS context construction.

Root-Cause / Mandate 1: native ``cryptography`` primitives ONLY. No ``subprocess``,
no ``openssl`` shell.

Architectural purity / Mandate 2: the SERVER cert SAN is NEVER a literal IP. The
Brain's external IP is ephemeral (per-session GCE node + /32 firewall), so the
cert is bound to a STABLE DNS-style RUNTIME IDENTITY instead:

  * a canonical ``jarvis-brain`` DNSName is always present -- the client verifies
    THIS name (``server_hostname="jarvis-brain"``) against the SAN, so a changing
    external IP never has to appear in the cert;
  * when ``server_identity`` is provided it is added as a DNSName, and -- if the
    GCE zone + project are resolvable -- the GCE metadata-internal DNS pattern
    ``<instance>.<zone>.c.<project>.internal`` is added as an additional SAN.

Authentication is by CA-trust (shared ``ca.pem`` + ``CERT_REQUIRED``); the
network-identity boundary is the ephemeral /32 firewall. No IP in any cert.

Bulletproof / Mandate 4: private keys are written 0600; the generator refuses to
silently overwrite an existing chain without ``--force``; it never emits a cert
with a literal-IP SAN (an IP-literal ``server_identity`` is rejected up front).
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import os
import sys
import tempfile
from typing import Dict, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# The canonical, stable identity the Brain server is always reachable AS. A
# dynamic external IP is authenticated by CA-trust, not by being in the cert;
# the client uses this name as its server_hostname over the CA-trust path.
CANONICAL_BRAIN_IDENTITY = "jarvis-brain"

_FILENAMES = {
    "ca": "ca.pem",
    "server_cert": "server-cert.pem",
    "server_key": "server-key.pem",
    "client_cert": "client-cert.pem",
    "client_key": "client-key.pem",
}

_KEY_SIZE = 2048
_PUBLIC_EXPONENT = 65537


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _new_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(
        public_exponent=_PUBLIC_EXPONENT, key_size=_KEY_SIZE
    )


def _server_dns_names(
    server_identity: str,
    *,
    gce_zone: Optional[str],
    gce_project: Optional[str],
) -> List[str]:
    """Build the ordered, de-duplicated DNSName SAN list for the server leaf.

    Never contains a literal IP -- an IP-literal identity is rejected by the
    caller before we get here.
    """
    names: List[str] = [CANONICAL_BRAIN_IDENTITY]
    ident = (server_identity or "").strip()
    if ident and ident != CANONICAL_BRAIN_IDENTITY:
        names.append(ident)
        # GCE metadata-internal DNS: <instance>.<zone>.c.<project>.internal --
        # only when both coordinates resolve AND the identity is a bare instance
        # name (not already a FQDN we should not decorate).
        if gce_zone and gce_project and "." not in ident:
            names.append("%s.%s.c.%s.internal" % (ident, gce_zone, gce_project))
    # De-dupe, preserve order.
    seen: Dict[str, None] = {}
    for n in names:
        seen.setdefault(n, None)
    return list(seen.keys())


def _build_ca(ca_cn: str, not_before, not_after):
    ca_key = _new_rsa_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ca_cn)])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return ca_key, ca_cert


def _sign_leaf(
    *,
    ca_key,
    ca_cert,
    subject_cn: str,
    san: x509.SubjectAlternativeName,
    eku_oid,
    not_before,
    not_after,
):
    """Generate a leaf key, build a CSR, and have the CA sign it into a cert."""
    leaf_key = _new_rsa_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    # CSR (Mandate: "key + CSR signed by the CA").
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .add_extension(san, critical=False)
        .sign(leaf_key, hashes.SHA256())
    )
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(san, critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([eku_oid]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return leaf_key, leaf_cert


def _write_cert(path: str, cert: x509.Certificate) -> None:
    with open(path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(path, 0o644)


def _write_key(path: str, key: rsa.RSAPrivateKey) -> None:
    # Create with 0600 up front (umask-independent), then re-assert perms.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )
    finally:
        os.chmod(path, 0o600)


def generate_brain_mtls_chain(
    *,
    out_dir: str,
    ca_cn: str = "jarvis-brain-ca",
    server_identity: str = "",
    client_cn: str = "jarvis-brain-client",
    validity_days: int = 7,
    gce_zone: Optional[str] = None,
    gce_project: Optional[str] = None,
    force: bool = False,
) -> Dict[str, str]:
    """Generate a CA + SERVER leaf + CLIENT leaf PEM chain under ``out_dir``.

    Returns a dict of the 5 written paths keyed
    ``ca``/``server_cert``/``server_key``/``client_cert``/``client_key``.

    Raises ``ValueError`` if ``server_identity`` is a literal IP (Mandate 2/4:
    no IP ever lands in a SAN). Raises ``FileExistsError`` if any target file
    already exists and ``force`` is False (Mandate 4: no silent overwrite).
    """
    ident = (server_identity or "").strip()
    if ident and _is_ip_literal(ident):
        raise ValueError(
            "server_identity must be a DNS-style identity, not a literal IP "
            "(got %r) -- the cert binds to a stable name, never the ephemeral IP"
            % ident
        )
    if validity_days <= 0:
        raise ValueError("validity_days must be positive (got %r)" % validity_days)

    gce_zone = gce_zone if gce_zone is not None else _resolve_env("JARVIS_BRAIN_GCE_ZONE")
    gce_project = (
        gce_project if gce_project is not None
        else _resolve_env("JARVIS_BRAIN_GCE_PROJECT")
    )

    os.makedirs(out_dir, exist_ok=True)
    paths = {k: os.path.join(out_dir, v) for k, v in _FILENAMES.items()}
    if not force:
        existing = [p for p in paths.values() if os.path.exists(p)]
        if existing:
            raise FileExistsError(
                "refusing to overwrite existing mTLS material without force=True: "
                + ", ".join(sorted(existing))
            )

    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(minutes=1)
    not_after = now + datetime.timedelta(days=validity_days)

    ca_key, ca_cert = _build_ca(ca_cn, not_before, not_after)

    # SERVER leaf: stable-DNS SAN, serverAuth. Subject CN mirrors the primary
    # DNS identity so tooling that reads CN still sees a name, never an IP.
    server_dns = _server_dns_names(ident, gce_zone=gce_zone, gce_project=gce_project)
    server_san = x509.SubjectAlternativeName([x509.DNSName(n) for n in server_dns])
    server_key, server_cert = _sign_leaf(
        ca_key=ca_key,
        ca_cert=ca_cert,
        subject_cn=server_dns[0],
        san=server_san,
        eku_oid=ExtendedKeyUsageOID.SERVER_AUTH,
        not_before=not_before,
        not_after=not_after,
    )

    # CLIENT leaf: SAN/CN == client_cn, clientAuth.
    client_cn = (client_cn or "jarvis-brain-client").strip()
    if _is_ip_literal(client_cn):
        raise ValueError("client_cn must not be a literal IP (got %r)" % client_cn)
    client_san = x509.SubjectAlternativeName([x509.DNSName(client_cn)])
    client_key, client_cert = _sign_leaf(
        ca_key=ca_key,
        ca_cert=ca_cert,
        subject_cn=client_cn,
        san=client_san,
        eku_oid=ExtendedKeyUsageOID.CLIENT_AUTH,
        not_before=not_before,
        not_after=not_after,
    )

    _write_cert(paths["ca"], ca_cert)
    _write_cert(paths["server_cert"], server_cert)
    _write_key(paths["server_key"], server_key)
    _write_cert(paths["client_cert"], client_cert)
    _write_key(paths["client_key"], client_key)

    return paths


def _resolve_env(key: str) -> Optional[str]:
    raw = os.environ.get(key)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _default_out_dir() -> str:
    env = _resolve_env("JARVIS_BRAIN_MTLS_DIR")
    if env:
        return env
    return tempfile.mkdtemp(prefix="jarvis-brain-mtls-")


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "")
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return default


def _print_env_block(paths: Dict[str, str]) -> None:
    """Print the operator/ignition env-export block.

    Client-side vars (``JARVIS_BRAIN_WS_TLS_*``) point at the MAC's material and
    are what ``build_client_ssl_context`` reads. The SERVER material paths are
    surfaced separately -- the ignition driver injects server-cert/key into the
    Brain VM's instance metadata.
    """
    lines = [
        "# --- JARVIS Brain mTLS chain (source this) ---",
        "# Client side (this Mac): consumed by build_client_ssl_context",
        "export JARVIS_BRAIN_WS_TLS_CA=%s" % paths["ca"],
        "export JARVIS_BRAIN_WS_TLS_CERT=%s" % paths["client_cert"],
        "export JARVIS_BRAIN_WS_TLS_KEY=%s" % paths["client_key"],
        "export JARVIS_BRAIN_WS_TLS_ENABLED=true",
        "export JARVIS_BRAIN_WS_TLS_EPHEMERAL=false",
        "# Server side (injected into the Brain VM metadata by the ignition driver)",
        "export JARVIS_BRAIN_SERVER_TLS_CA=%s" % paths["ca"],
        "export JARVIS_BRAIN_SERVER_TLS_CERT=%s" % paths["server_cert"],
        "export JARVIS_BRAIN_SERVER_TLS_KEY=%s" % paths["server_key"],
        "# ---------------------------------------------",
    ]
    print("\n".join(lines))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the Brain-VM mTLS CA + server + client cert chain "
        "(native cryptography, no openssl).",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="output dir for the 5 PEMs (default: env JARVIS_BRAIN_MTLS_DIR, "
        "else a per-run tmp dir)",
    )
    parser.add_argument(
        "--server-identity",
        default=_resolve_env("JARVIS_BRAIN_VM_NODE_NAME") or "",
        help="stable DNS-style SERVER identity for the SAN (a GCE instance name "
        "/ jarvis-brain DNSName). NEVER an IP. Empty => canonical 'jarvis-brain'.",
    )
    parser.add_argument(
        "--client-cn",
        default="jarvis-brain-client",
        help="client leaf CN/SAN (default jarvis-brain-client)",
    )
    parser.add_argument(
        "--ca-cn", default="jarvis-brain-ca", help="CA common name",
    )
    parser.add_argument(
        "--validity-days",
        type=int,
        default=_env_int("JARVIS_BRAIN_MTLS_VALIDITY_DAYS", 7),
        help="cert validity window in days (env JARVIS_BRAIN_MTLS_VALIDITY_DAYS, "
        "default 7)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing chain in --out-dir (default: refuse)",
    )
    args = parser.parse_args(argv)

    out_dir = args.out_dir or _default_out_dir()
    try:
        paths = generate_brain_mtls_chain(
            out_dir=out_dir,
            ca_cn=args.ca_cn,
            server_identity=args.server_identity,
            client_cn=args.client_cn,
            validity_days=args.validity_days,
            force=args.force,
        )
    except FileExistsError as exc:
        sys.stderr.write("error: %s\n(use --force to overwrite)\n" % exc)
        return 2
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    sys.stderr.write("wrote mTLS chain to %s\n" % out_dir)
    _print_env_block(paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
