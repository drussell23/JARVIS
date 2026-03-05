"""UMF Contract Gate -- validates incoming UMF messages.

Every message entering the local UMF fabric passes through this gate.
The gate **never** silently drops; it always returns a structured
``ValidationResult`` indicating acceptance or a specific rejection reason.

Validation steps (in order):
1. Schema version -- must be in the accepted set.
2. TTL expiry -- ``is_expired()`` checks ``observed_at + ttl > now``.
3. Routing deadline -- if set and in the past, reject.
4. Capability hash -- optional contract compatibility guard.
5. HMAC signature -- optional cryptographic authentication.

Design rules
------------
* Stdlib only (no third-party imports).  The managed_mode HMAC import is
  attempted lazily and falls back gracefully.
* Pure functions -- no global mutable state.
* Deterministic: same inputs always produce the same result.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import FrozenSet, Optional

from backend.core.umf.types import RejectReason, UMF_SCHEMA_VERSION, UmfMessage


# ── Accepted schema versions ──────────────────────────────────────────

_ACCEPTED_SCHEMAS: FrozenSet[str] = frozenset({UMF_SCHEMA_VERSION})


# ── Validation result ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a contract-gate validation check.

    ``accepted`` is True when the message passes all checks.
    ``reject_reason`` is None when accepted, otherwise one of the
    ``RejectReason`` string values.
    """

    accepted: bool
    message_id: str
    reject_reason: Optional[str] = None


# ── Public API ────────────────────────────────────────────────────────


def validate_message(
    msg: UmfMessage,
    *,
    expected_capability_hash: Optional[str] = None,
    hmac_secret: Optional[str] = None,
    session_id: Optional[str] = None,
    accepted_schemas: Optional[FrozenSet[str]] = None,
) -> ValidationResult:
    """Validate an incoming UMF message against the contract gate.

    Parameters
    ----------
    msg:
        The UMF message to validate.
    expected_capability_hash:
        If provided **and** the message carries a capability hash,
        the two must match.  If None, capability check is skipped.
    hmac_secret:
        If provided **and** the message carries a signature, the
        signature is verified using this secret.  If None, signature
        check is skipped.
    session_id:
        Session identifier passed to the HMAC verifier.
    accepted_schemas:
        Override the default accepted schema set.  Useful for tests
        or during rolling upgrades that need N-2 support.

    Returns
    -------
    ValidationResult
        Always populated -- never raises on invalid messages.
    """
    schemas = accepted_schemas if accepted_schemas is not None else _ACCEPTED_SCHEMAS

    # 1. Schema version
    if msg.schema_version not in schemas:
        return ValidationResult(
            accepted=False,
            message_id=msg.message_id,
            reject_reason=RejectReason.schema_mismatch.value,
        )

    # 2. TTL expiry
    if msg.is_expired():
        return ValidationResult(
            accepted=False,
            message_id=msg.message_id,
            reject_reason=RejectReason.ttl_expired.value,
        )

    # 3. Routing deadline
    if msg.routing_deadline_unix_ms > 0:
        now_ms = int(time.time() * 1000)
        if now_ms > msg.routing_deadline_unix_ms:
            return ValidationResult(
                accepted=False,
                message_id=msg.message_id,
                reject_reason=RejectReason.deadline_expired.value,
            )

    # 4. Capability hash
    if (
        expected_capability_hash is not None
        and msg.contract_capability_hash
        and msg.contract_capability_hash != expected_capability_hash
    ):
        return ValidationResult(
            accepted=False,
            message_id=msg.message_id,
            reject_reason=RejectReason.capability_mismatch.value,
        )

    # 5. HMAC signature
    if msg.signature_value and hmac_secret is not None:
        if not _verify_signature(msg, hmac_secret, session_id or ""):
            return ValidationResult(
                accepted=False,
                message_id=msg.message_id,
                reject_reason=RejectReason.sig_invalid.value,
            )

    # All checks passed
    return ValidationResult(
        accepted=True,
        message_id=msg.message_id,
    )


# ── Internal helpers ──────────────────────────────────────────────────


def _verify_signature(msg: UmfMessage, secret: str, session_id: str) -> bool:
    """Verify the HMAC signature on a UMF message.

    Attempts to import ``verify_hmac_auth`` from ``backend.core.managed_mode``.
    If the import fails (e.g. in a stripped-down deployment), returns False
    to deny the message safely.
    """
    try:
        from backend.core.managed_mode import verify_hmac_auth
    except ImportError:
        return False

    return verify_hmac_auth(
        header=msg.signature_value,
        session_id=session_id,
        secret=secret,
    )
