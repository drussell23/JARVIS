"""link_certs — the link's own identity, issued locally and honestly.

WHY THIS EXISTS AT ALL
----------------------
The overlay network (Tailscale) supplies a tunnel. It does not supply an
answer to *which Body is this?* — and that question is load-bearing, because
:func:`link_protocol.plan_resume` will happily serve a replayed session id to
whoever presents it. Anything on the tailnet could resume another Body's
session and receive its verdicts. mTLS is what makes the session id a claim
that has to be proven.

WHAT WENT WRONG WITH THE MATERIAL THAT ALREADY EXISTED
------------------------------------------------------
``.jarvis/brain_mtls/`` holds a working CA — for a different trust domain.
Its leaves are issued to ``jarvis-brain``, it belongs to the J-Prime channel,
and (as of this writing) it had expired weeks earlier on a **seven-day**
validity. Three separate reasons it cannot be reused, and only the third is
fixable by waiting:

* **Wrong names.** A leaf whose SAN is ``jarvis-brain`` cannot authenticate a
  host reached as a tailnet name, and turning off hostname verification to
  make it fit is how an mTLS deployment silently becomes encryption without
  identity.
* **Wrong domain.** One directory shared by two trust domains means rotating
  either silently re-keys the other, and a leaf issued for one peer is
  accepted by the other's verifier.
* **Wrong lifetime.** Seven days is a rotation cadence nobody sustained. A
  link between two machines in two houses cannot require a physical visit
  every week to keep working — the same reason Tailscale key expiry must be
  disabled on the Engine.

So the link gets its own CA, its own directory, and a validity chosen for how
often someone will actually be in the same room as both machines.

DESIGN
------
Generated locally with ``cryptography``; no network, no external PKI, no
shelling out to ``openssl`` with a hand-built config file. A private CA whose
only job is to sign exactly two leaves is the correct shape here — the trust
decision is "these two machines", and that is precisely what a two-leaf CA
encodes.

Nothing is hardcoded. Names, validity, key size and output directory are all
arguments with env-backed defaults, because the peer names are properties of
someone's tailnet and cannot be known here.

Refuses to overwrite existing material unless explicitly told to. A silent
regeneration on a working deployment would revoke the peer's trust from
across the country, and the operator would discover it as a handshake failure
with no obvious cause.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import ipaddress
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.LinkCerts")

LINK_CERTS_SCHEMA_VERSION: str = "link_certs.1"

#: Filenames the transport expects. Kept here so the writer and the reader
#: cannot disagree about what a directory of material looks like.
CA_CERT = "ca.pem"
CA_KEY = "ca-key.pem"
SERVER_CERT = "server-cert.pem"
SERVER_KEY = "server-key.pem"
CLIENT_CERT = "client-cert.pem"
CLIENT_KEY = "client-key.pem"

_LEAF_FILES = (SERVER_CERT, SERVER_KEY, CLIENT_CERT, CLIENT_KEY)
_ALL_FILES = (CA_CERT, CA_KEY) + _LEAF_FILES


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def validity_days() -> int:
    """Leaf lifetime. Default 825 days.

    Not a security compromise — a deployment one. The two machines are in two
    houses, so rotation requires either a visit or a remote procedure someone
    has to remember. 825 is the longest a public CA may issue and is the
    conventional ceiling for a private one; anything shorter re-creates the
    expiry that made the brain material unusable.
    """
    return _env_int("JARVIS_LINK_CERT_DAYS", 825)


def ca_validity_days() -> int:
    """CA lifetime. Must outlive every leaf it signs, or rotation cascades."""
    return _env_int("JARVIS_LINK_CA_DAYS", validity_days() * 2)


def key_bits() -> int:
    """RSA modulus size. Default 3072."""
    return _env_int("JARVIS_LINK_CERT_BITS", 3072, minimum=2048)


def clock_skew_hours() -> int:
    """Backdating on ``notBefore``. Default 24h.

    Directly load-bearing here: §29 exists because these two clocks drift,
    and a certificate whose validity begins "now" is rejected by a peer whose
    clock is a few minutes behind — a handshake failure whose cause looks
    like anything but a clock.
    """
    return _env_int("JARVIS_LINK_CERT_SKEW_HOURS", 24)


@dataclass(frozen=True)
class IssuedMaterial:
    directory: Path
    ca_subject: str
    server_names: Tuple[str, ...]
    client_names: Tuple[str, ...]
    not_after: str
    files: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": LINK_CERTS_SCHEMA_VERSION,
            "directory": str(self.directory),
            "ca_subject": self.ca_subject,
            "server_names": list(self.server_names),
            "client_names": list(self.client_names),
            "not_after": self.not_after,
            "files": list(self.files),
        }


class CertToolUnavailable(RuntimeError):
    """``cryptography`` is not importable. Stated, never worked around."""


def _split_names(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(",", " ").split()]
    else:
        parts = [str(p).strip() for p in raw]
    return tuple(p for p in parts if p)


def _san_entries(names: Sequence[str]) -> List[Any]:
    """Build SANs, routing literals to IPAddress and the rest to DNSName.

    A tailnet address is a ``100.x.y.z`` literal, and putting one in a DNSName
    produces a certificate that verifies against nothing: peers match IP
    literals against ``iPAddress`` SANs only. Getting this wrong yields a
    handshake failure that reads as a trust problem rather than an encoding
    one, so the split is made here rather than left to the caller.
    """
    from cryptography import x509

    out: List[Any] = []
    for name in names:
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            out.append(x509.DNSName(name))
    return out


def _publish(path: Path, data: bytes, *, secret: bool) -> None:
    """Write ``data`` to ``path`` atomically and durably.

    **Why atomic matters here specifically.** A reader of this directory is
    an SSL context being built at connection time, and a half-written PEM is
    not a recoverable partial read — OpenSSL rejects it, and the operator
    sees a trust failure with no hint that the cause was a concurrent writer.
    So every file is written to a temp in the SAME directory (a rename is
    only atomic within a filesystem) and published with
    ``durable_io.atomic_replace``, which already sequences fsync-data →
    rename → fsync-dir. Reusing it means the certificate directory gets the
    same durability guarantee as the transcript log rather than a second,
    weaker one written here.

    Secrets are created with mode 0600 applied at ``open`` rather than
    chmod'ed afterwards: between the two calls the key is world-readable,
    and on a shared machine that window is the whole vulnerability. The temp
    carries the mode too, since a temp file in a readable directory is just
    as exposed as the final name.
    """
    from backend.core.ouroboros.governance.durable_io import atomic_replace

    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    mode = (stat.S_IRUSR | stat.S_IWUSR) if secret else 0o644
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        atomic_replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@contextlib.contextmanager
def _issuance_lock(directory: Path):
    """Serialise concurrent issuance. Fails fast; never waits.

    Two ``--issue-certs`` runs interleaving would produce a CA from one and
    leaves from the other — material that is individually well-formed and
    collectively unverifiable, which is a far worse failure than a refusal.
    An ``O_EXCL`` create is the mutex because it is atomic on every
    filesystem this runs on, including over SMB on the Windows box.

    Fails fast rather than blocking: a second issuer is a mistake, not a
    queue, and telling the operator immediately is more useful than making
    them wait for a lock whose holder may have died. A stale lock names its
    owning pid so the remedy is obvious.
    """
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".issue.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            holder = lock.read_text(encoding="utf-8").strip()
        except OSError:
            holder = "unknown"
        raise FileExistsError(
            f"another issuance is in progress (holder pid {holder}). If no "
            f"such process exists, remove {lock} and retry."
        ) from None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def existing_material(directory: Optional[Path] = None) -> Dict[str, bool]:
    """Which expected files are present. Never raises."""
    base = Path(directory) if directory else _default_dir()
    out: Dict[str, bool] = {}
    for name in _ALL_FILES:
        try:
            out[name] = (base / name).exists()
        except OSError:
            out[name] = False
    return out


def _default_dir() -> Path:
    from backend.core.ouroboros.governance.link_transport import tls_dir
    return tls_dir()


def inspect_material(directory: Optional[Path] = None) -> Dict[str, Any]:
    """Report what is installed and whether it is currently usable.

    Reports expiry as a FACT with days remaining rather than a boolean, so an
    operator sees a rotation coming instead of discovering it as an outage —
    which is exactly how the brain material failed.
    """
    base = Path(directory) if directory else _default_dir()
    report: Dict[str, Any] = {
        "schema_version": LINK_CERTS_SCHEMA_VERSION,
        "directory": str(base), "present": existing_material(base),
        "certificates": {},
    }
    try:
        from cryptography import x509
    except ImportError:
        report["error"] = "cryptography unavailable — cannot inspect"
        return report
    now = _dt.datetime.now(_dt.timezone.utc)
    for name in (CA_CERT, SERVER_CERT, CLIENT_CERT):
        path = base / name
        if not path.exists():
            continue
        try:
            cert = x509.load_pem_x509_certificate(path.read_bytes())
            not_after = cert.not_valid_after_utc
            sans: List[str] = []
            try:
                ext = cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName)
                sans = [str(v) for v in ext.value.get_values_for_type(
                    x509.DNSName)]
                sans += [str(v) for v in ext.value.get_values_for_type(
                    x509.IPAddress)]
            except x509.ExtensionNotFound:
                pass
            report["certificates"][name] = {
                "subject": cert.subject.rfc4514_string(),
                "not_after": not_after.isoformat(),
                "days_remaining": (not_after - now).days,
                "expired": not_after <= now,
                "sans": sans,
            }
        except Exception as exc:  # noqa: BLE001
            report["certificates"][name] = {"error": str(exc)}
    return report


def issue_link_material(
    *,
    directory: Optional[Path] = None,
    server_names: Optional[Sequence[str]] = None,
    client_names: Optional[Sequence[str]] = None,
    force: bool = False,
) -> IssuedMaterial:
    """Create a private CA and the two leaves the link needs.

    ``server_names`` are every name or address the Body will dial the Engine
    by — its tailnet DNS name and its ``100.x`` address are both normal, and
    both belong in the SAN because a client that connects by address verifies
    against the address.

    Refuses to overwrite unless ``force``. Regenerating silently on a live
    deployment revokes the peer's trust from across the country, and the
    operator meets it as an inexplicable handshake failure.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CertToolUnavailable(
            "cryptography is required to issue link material; install it "
            "rather than falling back to a hand-built openssl config"
        ) from exc

    base = Path(directory) if directory else _default_dir()
    srv = _split_names(server_names) or _split_names(
        os.environ.get("JARVIS_LINK_SERVER_NAMES"))
    cli = _split_names(client_names) or _split_names(
        os.environ.get("JARVIS_LINK_CLIENT_NAMES")) or ("jarvis-body",)
    if not srv:
        raise ValueError(
            "server_names is required — the Engine's tailnet name and/or "
            "address. It is a property of your tailnet and cannot be guessed "
            "here; a wrong SAN fails the handshake as a trust error.")

    present = [n for n, ok in existing_material(base).items() if ok]
    if present and not force:
        raise FileExistsError(
            f"{base} already holds {len(present)} file(s): "
            f"{', '.join(sorted(present))}. Re-issuing revokes the peer's "
            f"trust — pass force=True only if you can re-copy the CA to the "
            f"other machine.")

    base.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc)
    not_before = now - _dt.timedelta(hours=clock_skew_hours())

    def _key():
        return rsa.generate_private_key(public_exponent=65537,
                                        key_size=key_bits())

    def _pem_key(key) -> bytes:
        return key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    ca_key = _key()
    ca_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "jarvis-link-ca"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS O+V"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject).issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(now + _dt.timedelta(days=ca_validity_days()))
        # A CA that can sign anything is a CA that can impersonate anything.
        # path_length=0 means it may sign leaves and never another CA.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                       critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False),
            critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    def _leaf(names: Sequence[str], cn: str, server: bool):
        from cryptography.x509.oid import ExtendedKeyUsageOID
        key = _key()
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .issuer_name(ca_subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(now + _dt.timedelta(days=validity_days()))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .add_extension(x509.SubjectAlternativeName(_san_entries(names)),
                           critical=False)
            # Narrow EKU on both sides. A leaf usable as both client and
            # server lets a compromised Body impersonate the Engine to
            # itself, which is exactly the confusion mTLS is bought to
            # prevent.
            .add_extension(
                x509.ExtendedKeyUsage([
                    ExtendedKeyUsageOID.SERVER_AUTH if server
                    else ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_encipherment=True,
                    content_commitment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False),
                critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        return key, cert

    server_key, server_cert = _leaf(srv, srv[0], server=True)
    client_key, client_cert = _leaf(cli, cli[0], server=False)

    pem = serialization.Encoding.PEM
    # Published under the issuance lock so two concurrent runs cannot
    # interleave a CA from one with leaves from the other.
    with _issuance_lock(base):
        _publish(base / CA_CERT, ca_cert.public_bytes(pem), secret=False)
        _publish(base / SERVER_CERT, server_cert.public_bytes(pem),
                 secret=False)
        _publish(base / CLIENT_CERT, client_cert.public_bytes(pem),
                 secret=False)
        _publish(base / CA_KEY, _pem_key(ca_key), secret=True)
        _publish(base / SERVER_KEY, _pem_key(server_key), secret=True)
        _publish(base / CLIENT_KEY, _pem_key(client_key), secret=True)

    logger.info(
        "[LinkCerts] issued link material in %s — server SAN %s, valid %dd",
        base, ", ".join(srv), validity_days())
    return IssuedMaterial(
        directory=base, ca_subject=ca_subject.rfc4514_string(),
        server_names=srv, client_names=cli,
        not_after=(now + _dt.timedelta(days=validity_days())).isoformat(),
        files=_ALL_FILES,
    )


def files_to_copy_to_peer() -> Tuple[str, ...]:
    """What the Body needs from the Engine's issuing run.

    The CA's PRIVATE key is deliberately absent: it stays on the machine that
    issued, and copying it would give each end the power to mint identities
    for the other. The Body needs the CA certificate to verify the Engine, and
    its own leaf — nothing more.
    """
    return (CA_CERT, CLIENT_CERT, CLIENT_KEY)
