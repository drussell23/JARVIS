"""Portable cross-repo ripple verification contract (Slice 97 Stage 1).

PREDICTIONS, NOT REQUESTS
=========================

A "ripple" is a cryptographically-signed NOTIFICATION that one repo's
state changed (a contract rule merged, a capability graduated, a
constitutional rule landed).  A consumer repo INDEPENDENTLY VERIFIES the
signature, replay, origin and freshness, then DECIDES what to do.  It
NEVER executes anything the ripple says.  ``verify_ripple`` returns a
*verdict + decoded notification* — nothing more.  A forged or replayed
ripple can never trigger remote code: there is no exec path here.

PORTABILITY (load-bearing)
==========================

jarvis-prime and reactor-core are SEPARATE repos that CANNOT import
``backend.core.ouroboros.*``.  The "independent verification" security
model REQUIRES each repo to verify on its own.  So this file is
STDLIB-ONLY (``hmac``/``hashlib``/``json``/``base64``/``time``/``secrets``/
``enum``/``dataclasses``/``typing``) and is COPYABLE verbatim into a
non-JARVIS repo.  There are NO ``backend.*`` imports.

This is NOT "rewriting the crypto library."  It is a ~stdlib HMAC-verify
contract that 3 repos share by VENDORING (copying).  The wire format is
byte-identical to ``backend/core/ouroboros/aegis/lease.py``:

    <b64url(compact-json-payload)>.<b64url(HMAC-SHA256(K, payload_b64))>

  * compact JSON: ``json.dumps(d, separators=(",", ":"), sort_keys=True)``
  * the HMAC signs the *encoded* payload bytes (defends against any future
    JSON canonicalization drift between sign and verify)
  * constant-time signature compare (``hmac.compare_digest``)

A cross-compat test proves a payload signed by JARVIS (via
``ripple_emitter`` / ``aegis.lease._encode_token``) verifies VERIFIED
under this portable ``verify_ripple``.

NO EXEC PATH: this module contains no ``eval``/``exec``/``subprocess``/
dynamic import.  ``ripple_kind`` and ``intent`` are plain STRINGS that
describe WHAT changed; they are never invoked.
"""
from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Set, Tuple


RIPPLE_SCHEMA_VERSION: str = "cross_repo_ripple.1"


# ---------------------------------------------------------------------------
# Ripple kind — closed taxonomy. STRINGS describing WHAT changed (never code).
# ---------------------------------------------------------------------------


class RippleKind(str, enum.Enum):
    """Closed taxonomy of cross-repo state-change notifications.

    Each value is a plain description of WHAT changed in the source repo.
    A consumer reads the kind to DECIDE what to do; it is never executed.
    """

    CONTRACT_CHANGED = "contract_changed"
    CAPABILITY_GRADUATED = "capability_graduated"
    CONSTITUTIONAL_RULE_MERGED = "constitutional_rule_merged"


# ---------------------------------------------------------------------------
# Verify verdict — closed taxonomy. Failures are SILENT DROPS (never raise).
# ---------------------------------------------------------------------------


class VerifyVerdict(str, enum.Enum):
    """Closed verdict set for ripple verification.

    Anything other than ``VERIFIED`` is a SILENT DROP — the consumer takes
    no action and does not raise.  This is the whole security model: a
    compromised/forged/replayed ripple yields a drop verdict, never code
    execution.
    """

    VERIFIED = "verified"
    DROPPED_BAD_SIGNATURE = "dropped_bad_signature"
    DROPPED_MALFORMED = "dropped_malformed"
    DROPPED_REPLAY = "dropped_replay"
    DROPPED_EXPIRED = "dropped_expired"
    DROPPED_WRONG_ORIGIN = "dropped_wrong_origin"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Ripple payload — the signed NOTIFICATION.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RipplePayload:
    """Immutable signed-notification payload.

    Fields are descriptive strings/numbers only — never code/commands.

      * ``ripple_kind`` / ``intent`` — STRINGS describing WHAT changed.
      * ``payload_sha256`` — hex digest of the canonical underlying object
        (so a consumer can correlate without trusting the ripple).
      * ``nonce`` — replay-protection token.
      * ``issued_at_unix`` / ``ttl_s`` — freshness window.
    """

    schema_version: str
    ripple_kind: str
    source_repo: str
    intent: str
    payload_sha256: str
    nonce: str
    issued_at_unix: float
    ttl_s: float

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Deterministic dict (sorted keys via the codec) for signing."""
        return {
            "schema_version": self.schema_version,
            "ripple_kind": self.ripple_kind,
            "source_repo": self.source_repo,
            "intent": self.intent,
            "payload_sha256": self.payload_sha256,
            "nonce": self.nonce,
            "issued_at_unix": self.issued_at_unix,
            "ttl_s": self.ttl_s,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RipplePayload":
        """Lossless reconstruction. Raises on a malformed dict — callers in
        ``verify_ripple`` wrap this so verification never raises."""
        return cls(
            schema_version=str(d["schema_version"]),
            ripple_kind=str(d["ripple_kind"]),
            source_repo=str(d["source_repo"]),
            intent=str(d["intent"]),
            payload_sha256=str(d["payload_sha256"]),
            nonce=str(d["nonce"]),
            issued_at_unix=float(d["issued_at_unix"]),
            ttl_s=float(d["ttl_s"]),
        )


# ---------------------------------------------------------------------------
# Wire codec — IDENTICAL to aegis.lease (vendored, stdlib-only).
# ---------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def _canonical_json(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sign(psk: bytes, payload_b64: str) -> str:
    mac = hmac.new(psk, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(mac)


def sign_ripple(payload: RipplePayload, psk: bytes) -> str:
    """Build the wire token: ``b64url(canonical-json).b64url(HMAC-SHA256)``.

    Pure stdlib HMAC — identical wire format to ``aegis.lease._encode_token``.
    ``psk`` is ``bytes`` (the cross-repo pre-shared key); never module-level.
    """
    payload_b64 = _b64url_encode(_canonical_json(payload.to_canonical_dict()))
    sig_b64 = _sign(psk, payload_b64)
    return f"{payload_b64}.{sig_b64}"


# ---------------------------------------------------------------------------
# Bounded replay-protection helper (optional — caller may inject a set).
# ---------------------------------------------------------------------------


class NonceSeen:
    """Tiny bounded set of seen nonces (drop-oldest). Stdlib-only.

    Mirrors ``aegis.lease.NonceLedger`` semantics in a vendorable form.
    ``register`` returns True if the nonce was newly seen, False if it was
    already present (replay).
    """

    def __init__(self, *, capacity: int = 8192) -> None:
        if capacity < 1:
            raise ValueError("NonceSeen capacity must be >= 1")
        self._capacity = int(capacity)
        self._set: Set[str] = set()
        self._order: list = []

    def __contains__(self, nonce: object) -> bool:
        return nonce in self._set

    def add(self, nonce: str) -> None:
        # Set-like surface so an injected ``set`` is interchangeable.
        self.register(nonce)

    def register(self, nonce: str) -> bool:
        if nonce in self._set:
            return False
        self._set.add(nonce)
        self._order.append(nonce)
        while len(self._order) > self._capacity:
            evicted = self._order.pop(0)
            self._set.discard(evicted)
        return True

    def __len__(self) -> int:
        return len(self._order)


def _mark_seen(seen: Any, nonce: str) -> bool:
    """Register ``nonce`` into a seen-collection. Returns True if newly
    seen (accept), False if already present (replay).

    Accepts either a plain ``set`` (or set-like) or a ``NonceSeen``.
    """
    if seen is None:
        return True
    register = getattr(seen, "register", None)
    if callable(register):
        return bool(register(nonce))
    # Plain set / set-like.
    if nonce in seen:
        return False
    try:
        seen.add(nonce)
    except Exception:
        # Unwritable seen-collection: treat as not-replayed for this call
        # rather than raising (verify_ripple never raises). The caller's
        # own ledger is the durable authority.
        return True
    return True


# ---------------------------------------------------------------------------
# Verification — verdict + decoded NOTIFICATION only. NEVER raises, NEVER execs.
# ---------------------------------------------------------------------------


def verify_ripple(
    token: str,
    psk: bytes,
    *,
    now_unix: float,
    seen_nonces: Optional[Any] = None,
    expected_origins: Optional[Sequence[str]] = None,
) -> Tuple[VerifyVerdict, Optional[RipplePayload]]:
    """Independently verify a ripple token.

    Order of checks (first failure wins):

      1. parse token (``<b64>.<b64>``)            → DROPPED_MALFORMED
      2. HMAC constant-time compare with ``psk``  → DROPPED_BAD_SIGNATURE
      3. origin in ``expected_origins`` (if given)→ DROPPED_WRONG_ORIGIN
      4. ``now_unix`` within [issued_at, issued_at+ttl] → DROPPED_EXPIRED
      5. nonce not already in ``seen_nonces``     → DROPPED_REPLAY
      else                                        → (VERIFIED, payload)

    NEVER raises (any internal error → DROPPED_MALFORMED).  NEVER executes
    anything: returns a verdict + the decoded NOTIFICATION only.  The
    payload's ``intent`` is a plain string that is never invoked.

    ``seen_nonces`` may be a plain ``set``, a set-like object, or a
    ``NonceSeen``.  On VERIFIED the nonce is registered (so a second verify
    of the same token returns DROPPED_REPLAY).  Registration happens LAST,
    only after every other check passes — a malformed/forged/expired/wrong-
    origin ripple never pollutes the replay ledger.
    """
    try:
        # (1) Structural parse.
        if not isinstance(token, str) or token.count(".") != 1:
            return VerifyVerdict.DROPPED_MALFORMED, None
        payload_b64, sig_b64 = token.split(".", 1)
        if not payload_b64 or not sig_b64:
            return VerifyVerdict.DROPPED_MALFORMED, None

        # (2) Signature (constant-time) BEFORE decoding the payload — we
        # never parse fields out of an unauthenticated payload.
        if not isinstance(psk, (bytes, bytearray)):
            return VerifyVerdict.DROPPED_MALFORMED, None
        expected_sig = _sign(bytes(psk), payload_b64)
        if not hmac.compare_digest(expected_sig, sig_b64):
            return VerifyVerdict.DROPPED_BAD_SIGNATURE, None

        # Decode the (now-authenticated) payload.
        try:
            raw = _b64url_decode(payload_b64)
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return VerifyVerdict.DROPPED_MALFORMED, None
        if not isinstance(obj, dict):
            return VerifyVerdict.DROPPED_MALFORMED, None
        try:
            payload = RipplePayload.from_dict(obj)
        except (KeyError, ValueError, TypeError):
            return VerifyVerdict.DROPPED_MALFORMED, None

        # (3) Origin.
        if expected_origins is not None:
            allowed: Iterable[str] = expected_origins
            if payload.source_repo not in set(allowed):
                return VerifyVerdict.DROPPED_WRONG_ORIGIN, None

        # (4) Freshness window [issued_at, issued_at + ttl].
        issued = payload.issued_at_unix
        expires = issued + payload.ttl_s
        if now_unix < issued or now_unix > expires:
            return VerifyVerdict.DROPPED_EXPIRED, None

        # (5) Replay — registered LAST, only on otherwise-valid ripples.
        if not _mark_seen(seen_nonces, payload.nonce):
            return VerifyVerdict.DROPPED_REPLAY, None

        # Verified NOTIFICATION. No execution — verdict + payload only.
        return VerifyVerdict.VERIFIED, payload
    except Exception:
        # Absolute never-raise guarantee: any unexpected internal error is
        # a silent drop, never a raise and never an execution.
        return VerifyVerdict.DROPPED_MALFORMED, None


__all__ = [
    "RIPPLE_SCHEMA_VERSION",
    "RippleKind",
    "VerifyVerdict",
    "RipplePayload",
    "NonceSeen",
    "sign_ripple",
    "verify_ripple",
]
