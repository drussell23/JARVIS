"""Unit tests for scripts/gen_brain_mtls.py (Stage-1 Brain-VM mTLS chain gen).

NO network, NO GCP, NO openssl subprocess. Every assertion is a native
``cryptography`` / ``ssl`` check against the 3-cert chain the generator emits:

  (a) all 5 PEMs land and parse via ``x509.load_pem_x509_certificate``;
  (b) server + client leaves are SIGNED BY the CA (signature chains to the CA
      public key);
  (c) server carries ``serverAuth`` EKU + a DNSName SAN and NO literal-IP
      IPAddress SAN; client carries ``clientAuth`` EKU;
  (d) THE LOAD PROOF: the generated files load cleanly through the UNCHANGED
      Stage-0 ``build_server_ssl_context`` / ``build_client_ssl_context`` and
      yield real ``ssl.SSLContext`` with ``verify_mode == CERT_REQUIRED``;
  (e) a real in-memory mutual handshake over a loopback socket SUCCEEDS with the
      generated chain and a CERTLESS client is REJECTED (mirrors Stage-0
      ``test_two_process_loopback.py::test_mtls_handshake_succeeds_and_is_required``);
  (f) key files are 0600.

The handshake test binds a loopback socket -> run with the sandbox disabled.
"""
from __future__ import annotations

import importlib.util
import ipaddress
import os
import socket
import ssl
import stat
import sys
import threading
from typing import Any, Dict

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.oid import ExtensionOID, ExtendedKeyUsageOID

from backend.core.ouroboros.governance.transport.transport_config import TransportConfig
from backend.core.ouroboros.governance.transport.transport_security import (
    build_server_ssl_context,
    build_client_ssl_context,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_GEN_PATH = os.path.join(_REPO_ROOT, "scripts", "gen_brain_mtls.py")


def _load_gen() -> Any:
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    spec = importlib.util.spec_from_file_location("gen_brain_mtls", _GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _load_cert(path: str) -> x509.Certificate:
    with open(path, "rb") as fh:
        return x509.load_pem_x509_certificate(fh.read())


def _tls_cfg(**over) -> TransportConfig:
    base = dict(
        host="127.0.0.1", port=0, path="/ws/trinity-bus", heartbeat_s=0.0,
        reconnect_base_s=0.05, reconnect_max_s=0.5, reconnect_jitter=0.0,
        queue_maxsize=256, history_maxlen=1024, degrade_after_missed_hb=2,
        tls_enabled=True, tls_cert=None, tls_key=None, tls_ca=None,
        tls_ephemeral=False, source_id="brain",
    )
    base.update(over)
    return TransportConfig(**base)


@pytest.fixture()
def chain(tmp_path) -> Dict[str, str]:
    gen = _load_gen()
    return gen.generate_brain_mtls_chain(
        out_dir=str(tmp_path / "mtls"),
        server_identity="jarvis-brain-vm",
        validity_days=7,
    )


# --- (a) all 5 PEMs land + parse -----------------------------------------
def test_all_five_pems_generated_and_parse(chain):
    for key in ("ca", "server_cert", "server_key", "client_cert", "client_key"):
        assert key in chain, "missing return key %s" % key
        assert os.path.exists(chain[key]), "missing file for %s" % key
    for key in ("ca", "server_cert", "client_cert"):
        cert = _load_cert(chain[key])
        assert isinstance(cert, x509.Certificate)


# --- (b) leaves signed by the CA -----------------------------------------
def test_server_and_client_signed_by_ca(chain):
    ca = _load_cert(chain["ca"])
    ca_pub = ca.public_key()
    for leaf_key in ("server_cert", "client_cert"):
        leaf = _load_cert(chain[leaf_key])
        assert leaf.issuer == ca.subject
        # Verifies the leaf signature chains to the CA public key. Raises on
        # mismatch -> a failed chain fails the test.
        ca_pub.verify(
            leaf.signature,
            leaf.tbs_certificate_bytes,
            padding.PKCS1v15(),
            leaf.signature_hash_algorithm,
        )


# --- (c) EKUs + DNSName SAN, no literal-IP SAN ---------------------------
def test_server_eku_and_dns_san_no_ip(chain):
    server = _load_cert(chain["server_cert"])
    eku = server.extensions.get_extension_for_oid(
        ExtensionOID.EXTENDED_KEY_USAGE
    ).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    san = server.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
    ).value
    dns_names = san.get_values_for_type(x509.DNSName)
    assert dns_names, "server SAN must carry at least one DNSName"
    # Architectural purity: NO literal-IP SAN. IPAddress SANs must be empty,
    # and no DNSName may itself be an IP literal.
    ip_sans = san.get_values_for_type(x509.IPAddress)
    assert ip_sans == [], "server SAN must not contain a literal IPAddress"
    for name in dns_names:
        with pytest.raises(ValueError):
            ipaddress.ip_address(name)


def test_client_eku_client_auth(chain):
    client = _load_cert(chain["client_cert"])
    eku = client.extensions.get_extension_for_oid(
        ExtensionOID.EXTENDED_KEY_USAGE
    ).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku


def test_ca_is_a_ca(chain):
    ca = _load_cert(chain["ca"])
    bc = ca.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value
    assert bc.ca is True


# --- (d) THE LOAD PROOF via the UNCHANGED Stage-0 builders ----------------
def test_load_proof_through_unchanged_builders(chain):
    server_cfg = _tls_cfg(
        tls_cert=chain["server_cert"], tls_key=chain["server_key"], tls_ca=chain["ca"],
    )
    client_cfg = _tls_cfg(
        tls_cert=chain["client_cert"], tls_key=chain["client_key"], tls_ca=chain["ca"],
    )
    sctx = build_server_ssl_context(server_cfg)
    cctx = build_client_ssl_context(client_cfg)
    assert isinstance(sctx, ssl.SSLContext)
    assert isinstance(cctx, ssl.SSLContext)
    assert sctx.verify_mode == ssl.CERT_REQUIRED
    assert cctx.verify_mode == ssl.CERT_REQUIRED


# --- (e) real loopback mutual handshake + certless reject ----------------
def _server_thread(listener: socket.socket, sctx: ssl.SSLContext, out: dict):
    try:
        conn, _ = listener.accept()
        conn.settimeout(5.0)
        try:
            tls = sctx.wrap_socket(conn, server_side=True)
            out["peercert"] = tls.getpeercert()
            try:
                tls.close()
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001 - reject path is a pass
            out["server_error"] = exc
    except Exception as exc:  # noqa: BLE001
        out["accept_error"] = exc


def test_mutual_handshake_succeeds_and_certless_rejected(chain):
    server_cfg = _tls_cfg(
        tls_cert=chain["server_cert"], tls_key=chain["server_key"], tls_ca=chain["ca"],
    )
    client_cfg = _tls_cfg(
        tls_cert=chain["client_cert"], tls_key=chain["client_key"], tls_ca=chain["ca"],
    )
    sctx = build_server_ssl_context(server_cfg)
    cctx = build_client_ssl_context(client_cfg)

    # The stable DNS identity the SERVER cert is bound to (no IP). The client
    # verifies THIS name against the SAN -- proving identity-by-DNS, not by IP.
    server = _load_cert(chain["server_cert"])
    san = server.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
    ).value
    identity = san.get_values_for_type(x509.DNSName)[0]

    # (e.1) mTLS mutual handshake succeeds.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    out: dict = {}
    t = threading.Thread(target=_server_thread, args=(listener, sctx, out), daemon=True)
    t.start()
    raw = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    tls = cctx.wrap_socket(raw, server_hostname=identity)
    try:
        assert tls.cipher() is not None
    finally:
        try:
            tls.close()
        except OSError:
            pass
        t.join(timeout=5.0)
    assert "server_error" not in out, "handshake failed: %r" % out.get("server_error")
    # Server saw the client's cert (mutual auth actually happened).
    assert out.get("peercert"), "server did not receive a client cert"
    listener.close()

    # (e.2) a CERTLESS client is REJECTED (server CERT_REQUIRED).
    listener2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener2.bind(("127.0.0.1", 0))
    listener2.listen(1)
    port2 = listener2.getsockname()[1]
    out2: dict = {}
    t2 = threading.Thread(target=_server_thread, args=(listener2, sctx, out2), daemon=True)
    t2.start()
    bare = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    bare.check_hostname = False
    bare.verify_mode = ssl.CERT_NONE  # accept server cert, present NO client cert
    raw2 = socket.create_connection(("127.0.0.1", port2), timeout=5.0)
    with pytest.raises((ssl.SSLError, OSError)):
        cless = bare.wrap_socket(raw2, server_hostname=identity)
        # If the handshake somehow returns, force I/O so the reject surfaces.
        cless.send(b"x")
        cless.recv(1)
    t2.join(timeout=5.0)
    assert "server_error" in out2, "certless client was NOT rejected by the server"
    listener2.close()


# --- (f) key perms 0600 ---------------------------------------------------
def test_key_files_are_0600(chain):
    for key in ("server_key", "client_key"):
        mode = stat.S_IMODE(os.stat(chain[key]).st_mode)
        assert mode == 0o600, "%s perms are %o, expected 600" % (key, mode)


# --- extra: never a literal-IP SAN even when identity is empty -----------
def test_default_identity_is_dns_no_ip(tmp_path):
    gen = _load_gen()
    chain = gen.generate_brain_mtls_chain(out_dir=str(tmp_path / "d"), server_identity="")
    server = _load_cert(chain["server_cert"])
    san = server.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
    ).value
    assert san.get_values_for_type(x509.IPAddress) == []
    assert "jarvis-brain" in san.get_values_for_type(x509.DNSName)


# --- extra: idempotent-safe (no silent overwrite without --force) --------
def test_refuses_overwrite_without_force(tmp_path):
    gen = _load_gen()
    out = str(tmp_path / "once")
    gen.generate_brain_mtls_chain(out_dir=out, server_identity="jarvis-brain-vm")
    with pytest.raises(FileExistsError):
        gen.generate_brain_mtls_chain(out_dir=out, server_identity="jarvis-brain-vm")
    # force=True overwrites cleanly.
    again = gen.generate_brain_mtls_chain(
        out_dir=out, server_identity="jarvis-brain-vm", force=True,
    )
    assert os.path.exists(again["ca"])


# --- extra: an IP-literal server_identity is refused ---------------------
def test_ip_literal_identity_refused(tmp_path):
    gen = _load_gen()
    with pytest.raises(ValueError):
        gen.generate_brain_mtls_chain(
            out_dir=str(tmp_path / "ip"), server_identity="10.128.0.7",
        )
