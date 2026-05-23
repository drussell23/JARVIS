"""Lease + SessionToken cryptographic surface.

Per Slice Aegis-1 binding correction #1, the auth lifecycle is **Model B**:

  1. Aegis mints a single-use bootstrap PSK at boot.
  2. JARVIS calls ``POST /session/establish`` with ``Authorization:
     Bearer <BOOTSTRAP_PSK>``. Aegis validates, marks the PSK consumed
     irrevocably, mints a **scoped session token** (HMAC, TTL).
  3. JARVIS calls ``POST /lease/acquire`` with ``Authorization: Bearer
     <SESSION_TOKEN>``. Aegis validates the session token, mints a
     **lease token** describing a single upcoming provider call.
  4. (Slice 2) JARVIS calls a provider endpoint via Aegis with the
     lease token as ``X-JARVIS-Lease``. Aegis redeems the lease once.

Per binding correction #3, the HMAC key K never appears in:

  * ``json.dumps`` output (verdicts/tokens never embed it)
  * ``repr`` / ``str`` / log lines (no formatter ever sees it)
  * HTTP responses (no route handler returns it)
  * Any non-aegis module's namespace (passed only into this module's
    functions from the daemon's per-instance state)

K is a per-instance argument to every signing/validation function in
this module. The daemon constructs K once at boot via
``secrets.token_bytes(32)`` and holds it in its aiohttp ``Application``
state. AST-pinned: no module-level K variable.

Token wire format (lighter JWT, no algorithm flexibility — single
algorithm baked in to avoid the ``alg=none`` family of JWT footguns):

    <base64url(compact-json-payload)>.<base64url(HMAC-SHA256(K, payload_b64))>

Compact JSON: ``json.dumps(d, separators=(",", ":"), sort_keys=True)``
so token bytes are deterministic for the same payload (tests can pin
exact strings if useful).
"""
from __future__ import annotations

import asyncio
import base64
import enum
import hashlib
import hmac
import json
import secrets
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional, Set


LEASE_SCHEMA_VERSION: str = "aegis_lease.1"

# Default session-token TTL: 1 hour. Operators tighten via env at
# daemon boot; the daemon passes the effective value into mint calls.
DEFAULT_SESSION_TOKEN_TTL_S: int = 3600

# Default lease TTL: 5 minutes. Long enough for a provider call to
# complete including extended-thinking budgets; short enough that
# leaked leases self-expire quickly.
DEFAULT_LEASE_TTL_S: int = 300

# Default nonce ledger capacity. Bounded to prevent unbounded memory
# growth under replay-probe load. Drop-oldest eviction. At 60s burst
# of 100 ops/s the LRU comfortably holds 1 minute of nonces.
DEFAULT_NONCE_LEDGER_CAPACITY: int = 8192


# ---------------------------------------------------------------------------
# Verdict taxonomy — frozen closed sets per §33 / §43 discipline.
# ---------------------------------------------------------------------------


class TokenVerdictKind(str, enum.Enum):
    """Closed 5-value taxonomy for any token validation outcome."""

    VALID = "valid"
    INVALID_FORMAT = "invalid_format"
    INVALID_SIGNATURE = "invalid_signature"
    EXPIRED = "expired"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class TokenVerdict:
    """Generic token validation result. Used by both session-token
    and lease-token validation. Carries the decoded payload only on
    ``VALID``; on failure payload is ``None`` (we never leak parsed
    fields from an invalid token)."""

    kind: TokenVerdictKind
    payload: Optional[Dict[str, Any]] = None
    detail: Optional[str] = None  # short human-readable note; never K


# ---------------------------------------------------------------------------
# SessionToken + Lease payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionToken:
    """Scoped session-token payload. Issued by /session/establish.

    Equality + hash are content-based (frozen). §33.5 lossless roundtrip
    via to_dict/from_dict.
    """

    jti: str          # token id; nonce for the session-token ledger
    issued_at: float
    expires_at: float
    schema_version: str = LEASE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "session",
            "jti": self.jti,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "v": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionToken":
        if d.get("kind") != "session":
            raise ValueError(f"payload kind != session: {d.get('kind')!r}")
        return cls(
            jti=str(d["jti"]),
            issued_at=float(d["iat"]),
            expires_at=float(d["exp"]),
            schema_version=str(d.get("v", LEASE_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class Lease:
    """Lease payload — describes one upcoming provider call.

    ``causal_lineage_hash`` is a stub for Arc #4 (Causal-Lineage Depth-N
    stamp). Slice 1 accepts opaque str; Arc #4 will validate it against
    the inherited hash chain.
    """

    nonce: str
    op_id: str
    route: str                     # IMMEDIATE | STANDARD | COMPLEX | BACKGROUND | SPECULATIVE
    estimated_cost_usd: float
    max_cost_usd: float
    causal_lineage_hash: str
    issued_at: float
    expires_at: float
    schema_version: str = LEASE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "lease",
            "nonce": self.nonce,
            "op_id": self.op_id,
            "route": self.route,
            "est_usd": self.estimated_cost_usd,
            "max_usd": self.max_cost_usd,
            "lineage": self.causal_lineage_hash,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "v": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Lease":
        if d.get("kind") != "lease":
            raise ValueError(f"payload kind != lease: {d.get('kind')!r}")
        return cls(
            nonce=str(d["nonce"]),
            op_id=str(d["op_id"]),
            route=str(d["route"]),
            estimated_cost_usd=float(d["est_usd"]),
            max_cost_usd=float(d["max_usd"]),
            causal_lineage_hash=str(d["lineage"]),
            issued_at=float(d["iat"]),
            expires_at=float(d["exp"]),
            schema_version=str(d.get("v", LEASE_SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Token wire codec (lighter JWT-shape).
# ---------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # Re-pad to multiple of 4 — urlsafe_b64decode requires it.
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def _canonical_json(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sign(K: bytes, payload_b64: str) -> str:
    """Compute HMAC-SHA256(K, payload_b64).encode and base64-url-encode.

    Signs the encoded payload bytes rather than the raw JSON — defends
    against any future JSON canonicalization drift between mint and
    validate.
    """
    mac = hmac.new(K, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(mac)


def _encode_token(K: bytes, payload: Dict[str, Any]) -> str:
    payload_b64 = _b64url_encode(_canonical_json(payload))
    sig_b64 = _sign(K, payload_b64)
    return f"{payload_b64}.{sig_b64}"


def _decode_token(K: bytes, token: str) -> TokenVerdict:
    """Decode + verify a token. Returns a verdict; never raises.

    Failure modes (closed):
      * INVALID_FORMAT — not <b64>.<b64>, base64 decode fails, JSON
        not a dict
      * INVALID_SIGNATURE — HMAC mismatch (constant-time compare)
      * (caller checks EXPIRED + REPLAYED — we just decode here)
    """
    if not isinstance(token, str) or token.count(".") != 1:
        return TokenVerdict(TokenVerdictKind.INVALID_FORMAT, detail="missing dot")

    payload_b64, sig_b64 = token.split(".", 1)
    if not payload_b64 or not sig_b64:
        return TokenVerdict(TokenVerdictKind.INVALID_FORMAT, detail="empty segment")

    expected_sig = _sign(K, payload_b64)
    if not hmac.compare_digest(expected_sig, sig_b64):
        return TokenVerdict(TokenVerdictKind.INVALID_SIGNATURE)

    try:
        payload_bytes = _b64url_decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return TokenVerdict(TokenVerdictKind.INVALID_FORMAT, detail="payload decode")

    if not isinstance(payload, dict):
        return TokenVerdict(TokenVerdictKind.INVALID_FORMAT, detail="payload not dict")

    return TokenVerdict(TokenVerdictKind.VALID, payload=payload)


# ---------------------------------------------------------------------------
# SessionToken minting + validation
# ---------------------------------------------------------------------------


def mint_session_token(
    K: bytes,
    *,
    now_s: float,
    ttl_s: int = DEFAULT_SESSION_TOKEN_TTL_S,
) -> tuple[str, SessionToken]:
    """Mint a fresh session token signed by ``K``.

    Returns ``(wire_token, payload)`` — caller (the daemon) stores
    ``payload.jti`` in its active-sessions ledger and returns the wire
    string to the client.
    """
    jti = secrets.token_urlsafe(16)
    payload = SessionToken(
        jti=jti,
        issued_at=now_s,
        expires_at=now_s + float(ttl_s),
    )
    wire = _encode_token(K, payload.to_dict())
    return wire, payload


def validate_session_token(
    K: bytes,
    token: str,
    *,
    now_s: float,
    active_jti: Set[str],
) -> TokenVerdict:
    """Validate a session token. Returns a verdict; never raises.

    ``active_jti`` is the daemon's set of currently-valid session ids.
    A token whose ``jti`` is not in the set is treated as REPLAYED
    (the daemon revoked it; e.g., session expired and was reaped).
    """
    verdict = _decode_token(K, token)
    if verdict.kind is not TokenVerdictKind.VALID:
        return verdict
    assert verdict.payload is not None  # for type checker; VALID guarantees it

    try:
        st = SessionToken.from_dict(verdict.payload)
    except (KeyError, ValueError, TypeError) as exc:
        return TokenVerdict(TokenVerdictKind.INVALID_FORMAT, detail=str(exc))

    if now_s >= st.expires_at:
        return TokenVerdict(TokenVerdictKind.EXPIRED, detail=f"exp={st.expires_at}")

    if st.jti not in active_jti:
        return TokenVerdict(TokenVerdictKind.REPLAYED, detail="jti not active")

    return TokenVerdict(TokenVerdictKind.VALID, payload=verdict.payload)


# ---------------------------------------------------------------------------
# Lease minting + validation
# ---------------------------------------------------------------------------


def mint_lease_token(K: bytes, lease: Lease) -> str:
    """Encode a fully-formed ``Lease`` as a wire token signed by K."""
    return _encode_token(K, lease.to_dict())


def validate_lease_token(
    K: bytes,
    token: str,
    *,
    now_s: float,
    nonce_ledger: "NonceLedger",
) -> TokenVerdict:
    """Validate a lease token. Returns a verdict; never raises.

    Replay protection: nonces are registered into the ledger on the
    FIRST successful validation. A second validation of the same lease
    is rejected as REPLAYED.

    Note: ``validate_lease_token`` records the nonce as redeemed. This
    is the policy boundary for "a lease is used exactly once."
    """
    verdict = _decode_token(K, token)
    if verdict.kind is not TokenVerdictKind.VALID:
        return verdict
    assert verdict.payload is not None

    try:
        lease = Lease.from_dict(verdict.payload)
    except (KeyError, ValueError, TypeError) as exc:
        return TokenVerdict(TokenVerdictKind.INVALID_FORMAT, detail=str(exc))

    if now_s >= lease.expires_at:
        return TokenVerdict(TokenVerdictKind.EXPIRED, detail=f"exp={lease.expires_at}")

    # Single-redeem invariant.
    if not nonce_ledger.try_register(lease.nonce):
        return TokenVerdict(TokenVerdictKind.REPLAYED, detail="nonce already redeemed")

    return TokenVerdict(TokenVerdictKind.VALID, payload=verdict.payload)


# ---------------------------------------------------------------------------
# Bounded nonce ledger
# ---------------------------------------------------------------------------


class NonceLedger:
    """Bounded FIFO set of redeemed nonces. Async-safe.

    Drop-oldest eviction when capacity is exceeded — older nonces
    "expire" by ejection rather than wall-clock. Safe because lease
    TTL is short (5 minutes default) and capacity (8192) sustains
    ~27 redemptions/sec for the entire 5-minute window without ever
    evicting a still-live lease.

    Capacity is env-tunable via daemon construction; do not import env
    here (single-seam discipline — daemon reads env, passes value in).
    """

    def __init__(self, *, capacity: int = DEFAULT_NONCE_LEDGER_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"NonceLedger capacity must be >= 1, got {capacity}")
        self._capacity: int = int(capacity)
        self._set: Set[str] = set()
        self._fifo: Deque[str] = deque()
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def size(self) -> int:
        # Read-only; no lock needed for a single int read on CPython.
        return len(self._fifo)

    def try_register_sync(self, nonce: str) -> bool:
        """Sync variant for non-async callers (tests). Returns True if
        the nonce was newly registered, False if it was already present
        (replay)."""
        if nonce in self._set:
            return False
        self._set.add(nonce)
        self._fifo.append(nonce)
        while len(self._fifo) > self._capacity:
            evicted = self._fifo.popleft()
            self._set.discard(evicted)
        return True

    def try_register(self, nonce: str) -> bool:
        """Async-API alias. NonceLedger does not actually need the
        lock for try_register because set+deque CPython operations
        are GIL-atomic at this granularity, and the daemon serializes
        validation through a single event loop. Lock retained for
        future multi-task callers and audit clarity."""
        # Synchronous body is fine; provided for symmetry with future
        # I/O-bearing operations. Validate functions call this from
        # within request handlers (event loop), so no contention.
        return self.try_register_sync(nonce)

    def contains(self, nonce: str) -> bool:
        return nonce in self._set


__all__ = [
    "DEFAULT_LEASE_TTL_S",
    "DEFAULT_NONCE_LEDGER_CAPACITY",
    "DEFAULT_SESSION_TOKEN_TTL_S",
    "LEASE_SCHEMA_VERSION",
    "Lease",
    "NonceLedger",
    "SessionToken",
    "TokenVerdict",
    "TokenVerdictKind",
    "mint_lease_token",
    "mint_session_token",
    "validate_lease_token",
    "validate_session_token",
]
