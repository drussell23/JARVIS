"""Slice 122 — The Sovereign Cryptographic Key Manager (Ed25519, passphrase-derived).

Provisions the operator's signing authority for the Layer-4 roadmap (Slice 120)
with **zero hardcoding** and a genuine cryptographic air-gap between the operator
and the autonomous loop.

WHY ASYMMETRIC (the root-cause fix)
-----------------------------------
Slice 120 verified roadmaps with symmetric HMAC — but with HMAC, *whoever can
verify can also forge*. If the loop holds the key to verify a signed roadmap, it
can mint its own and self-authorize unattended autonomy. That voids the §1
Zero-Order-Doll invariant. The correct primitive is **asymmetric Ed25519**:

  • The operator holds the PRIVATE key (derived live from a passphrase; never at
    rest). Only the operator can sign.
  • The loop is given ONLY the PUBLIC key (``.jarvis/layer4_operator.pub``). It
    can verify a roadmap but CANNOT forge one — a mathematical guarantee, not a
    policy. This module exposes no path to a private key without the passphrase.

KEY DERIVATION (zero hardcoding, never stored)
----------------------------------------------
The Ed25519 seed is ``scrypt(passphrase, salt)`` (stdlib ``hashlib.scrypt``, no
third-party dep). Only the salt + the public key + a verifier are persisted —
never the passphrase, never the private key, never a plaintext .env secret. The
operator types the passphrase at provision time and at every sign; the loop, run
headless, has no passphrase → cannot derive the private key → cannot sign.

SINGLE-USER HONESTY
-------------------
On a single-user OS there is no kernel-level separation between "the AI process"
and "the operator" — they share the user. The air-gap here is therefore
*cryptographic and procedural*, not OS-enforced: the private key exists only
transiently in an interactive operator session that supplies the passphrase, and
the persisted material (public key) is verify-only. Even if this were defeated,
the Slice-120 **un-signable floor** still holds: no signature suppresses M10 /
recursion-breach / governance / APPROVAL_REQUIRED ops. Defense in depth.

Master switch: ``JARVIS_SOVEREIGN_KEYS_ENABLED`` (default **false**, §33.1).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_SOVEREIGN_KEYS_ENABLED"
_ENV_PUBKEY = "JARVIS_LAYER4_OPERATOR_PUBKEY"        # b64 public key (loop verify)
_ENV_STORE_DIR = "JARVIS_SOVEREIGN_KEY_DIR"

_SALT_NAME = "layer4_key.salt"
_PUB_NAME = "layer4_operator.pub"
_META_NAME = "layer4_key.meta.json"

# scrypt cost — interactive-grade; tune via env only if the operator must.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SEED_LEN = 32


def sovereign_keys_enabled() -> bool:
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def _store_dir() -> Path:
    return Path(os.getenv(_ENV_STORE_DIR, ".jarvis"))


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# ---------------------------------------------------------------------------
# Derivation — passphrase → Ed25519 private key. Never persisted.
# ---------------------------------------------------------------------------
def _derive_seed(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise ValueError("empty passphrase")
    # maxmem must exceed 128*N*r*p bytes (OpenSSL's default 32 MiB cap is exactly
    # the n=2**15 requirement, so set it explicitly).
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SEED_LEN,
        maxmem=128 * _SCRYPT_N * _SCRYPT_R * _SCRYPT_P + 1024 * 1024,
    )


def _private_from_passphrase(passphrase: str, salt: bytes) -> Ed25519PrivateKey:
    seed = _derive_seed(passphrase, salt)
    return Ed25519PrivateKey.from_private_bytes(seed)


def _public_b64(priv: Ed25519PrivateKey) -> str:
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64e(raw)


# ---------------------------------------------------------------------------
# Provisioning + load (operator actions — require the passphrase).
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ProvisionResult:
    public_key_b64: str
    salt_path: str
    pub_path: str


def is_provisioned() -> bool:
    d = _store_dir()
    return (d / _SALT_NAME).exists() and (d / _PUB_NAME).exists()


def provision(passphrase: str, *, overwrite: bool = False) -> ProvisionResult:
    """OPERATOR ACTION: derive the keypair from a passphrase, persist ONLY the
    salt + public key + verifier. Idempotent guard unless ``overwrite``."""
    d = _store_dir()
    d.mkdir(parents=True, exist_ok=True)
    salt_path, pub_path, meta_path = d / _SALT_NAME, d / _PUB_NAME, d / _META_NAME
    if is_provisioned() and not overwrite:
        raise FileExistsError("already provisioned; pass overwrite=True to rotate")

    salt = secrets.token_bytes(16)
    priv = _private_from_passphrase(passphrase, salt)
    pub_b64 = _public_b64(priv)
    # Verifier: a signature over a fixed challenge, checkable with the public key
    # — lets load_private_key confirm the passphrase WITHOUT storing the key.
    verifier = _b64e(priv.sign(b"jarvis-layer4-verifier-v1"))

    salt_path.write_bytes(salt)
    pub_path.write_text(pub_b64, encoding="utf-8")
    meta_path.write_text(json.dumps({"verifier": verifier, "alg": "ed25519",
                                     "kdf": "scrypt", "n": _SCRYPT_N}), encoding="utf-8")
    logger.info("[SovereignKeys] provisioned operator keypair (public only at rest)")
    return ProvisionResult(public_key_b64=pub_b64, salt_path=str(salt_path), pub_path=str(pub_path))


def load_private_key(passphrase: str) -> Ed25519PrivateKey:
    """OPERATOR ACTION: re-derive the private key and confirm the passphrase
    against the stored verifier. Raises on wrong passphrase / not provisioned.
    The returned key exists only in this process's memory for the sign call."""
    d = _store_dir()
    if not is_provisioned():
        raise FileNotFoundError("not provisioned — run provision() first")
    salt = (d / _SALT_NAME).read_bytes()
    priv = _private_from_passphrase(passphrase, salt)
    # Verify the passphrase reproduced the right key.
    meta = json.loads((d / _META_NAME).read_text(encoding="utf-8"))
    pub = priv.public_key()
    try:
        pub.verify(_b64d(meta["verifier"]), b"jarvis-layer4-verifier-v1")
    except InvalidSignature as exc:
        raise ValueError("wrong passphrase (verifier mismatch)") from exc
    return priv


def load_public_key() -> Optional[Ed25519PublicKey]:
    """LOOP-SAFE: load the verify-only public key from env or the pub file.
    Returns None when unavailable → callers fail closed."""
    env = os.getenv(_ENV_PUBKEY)
    raw_b64 = None
    if env:
        raw_b64 = env.strip()
    else:
        p = _store_dir() / _PUB_NAME
        if p.exists():
            raw_b64 = p.read_text(encoding="utf-8").strip()
    if not raw_b64:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(_b64d(raw_b64))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SovereignKeys] public key load failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Roadmap signing / verification (Ed25519 over the canonical body).
# ---------------------------------------------------------------------------
def _canonical_body(body: Dict[str, Any]) -> bytes:
    stripped = {k: v for k, v in body.items() if k not in ("signature", "signature_alg")}
    return json.dumps(stripped, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign_roadmap(body: Dict[str, Any], passphrase: str) -> Dict[str, Any]:
    """OPERATOR ACTION: produce a signed roadmap dict (adds ``signature`` +
    ``signature_alg=ed25519``). Requires the passphrase → the private key."""
    priv = load_private_key(passphrase)
    sig = priv.sign(_canonical_body(body))
    out = dict(body)
    out["signature_alg"] = "ed25519"
    out["signature"] = _b64e(sig)
    return out


def verify_roadmap_signature(body: Dict[str, Any]) -> bool:
    """LOOP-SAFE: verify an ed25519-signed roadmap with the PUBLIC key only.
    Returns False on any defect (no pubkey, wrong alg, bad signature). The loop
    can call this; it has no path to forge a passing signature."""
    if body.get("signature_alg") != "ed25519":
        return False
    sig = body.get("signature")
    if not isinstance(sig, str):
        return False
    pub = load_public_key()
    if pub is None:
        return False
    try:
        pub.verify(_b64d(sig), _canonical_body(body))
        return True
    except Exception:  # noqa: BLE001 - InvalidSignature or any decode defect → reject
        return False


def sign_draft_file(draft_path: str, signed_path: str, passphrase: str) -> str:
    """OPERATOR ACTION: read an unsigned draft YAML, sign it, write the signed
    YAML. Returns the signed path. Raises on wrong passphrase / missing draft."""
    import yaml

    body = yaml.safe_load(Path(draft_path).read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ValueError(f"draft at {draft_path} is not a mapping")
    signed = sign_roadmap(body, passphrase)
    Path(signed_path).parent.mkdir(parents=True, exist_ok=True)
    Path(signed_path).write_text(yaml.safe_dump(signed, sort_keys=True), encoding="utf-8")
    return signed_path


__all__ = [
    "sovereign_keys_enabled",
    "is_provisioned",
    "provision",
    "load_private_key",
    "load_public_key",
    "sign_roadmap",
    "sign_draft_file",
    "verify_roadmap_signature",
    "ProvisionResult",
]


if __name__ == "__main__":  # pragma: no cover - operator entrypoint
    import argparse
    import getpass

    ap = argparse.ArgumentParser(description="Sovereign Layer-4 key manager (Ed25519)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_prov = sub.add_parser("provision", help="derive + persist the operator keypair (public only at rest)")
    p_prov.add_argument("--overwrite", action="store_true", help="rotate an existing key")
    p_sign = sub.add_parser("sign", help="sign a draft roadmap → signed roadmap")
    p_sign.add_argument("--draft", default=".jarvis/roadmap.draft.yaml")
    p_sign.add_argument("--out", default=".jarvis/roadmap.signed.yaml")
    args = ap.parse_args()

    if args.cmd == "provision":
        pw = getpass.getpass("Operator passphrase (never stored): ")
        pw2 = getpass.getpass("Confirm passphrase: ")
        if pw != pw2:
            raise SystemExit("passphrases do not match")
        res = provision(pw, overwrite=args.overwrite)
        print(f"Provisioned. Public key (give to the loop via {_ENV_PUBKEY} or {res.pub_path}):")
        print(f"  {res.public_key_b64}")
    elif args.cmd == "sign":
        pw = getpass.getpass("Operator passphrase: ")
        out = sign_draft_file(args.draft, args.out, pw)
        print(f"Signed roadmap → {out}")
