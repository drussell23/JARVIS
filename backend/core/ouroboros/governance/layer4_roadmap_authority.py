"""Slice 120 — The Sovereign Layer-4 Roadmap Authority (BOUNDED).

The system transitions from a per-PR reviewer to a *long-term executor*: it
ingests an operator-signed multi-month roadmap (``.jarvis/roadmap.signed.yaml``),
cryptographically verifies the Zero-Order Doll's (the operator's) signature, and
— for the **safe, explicitly-authorized scopes only** — may run *unattended*,
allocating its own budget and CPU cycles to execute those goals. This is the
bridge that lets the 12-18 month evidence clock run without a human in the loop
for every Green op.

WHAT THIS IS NOT — the load-bearing §1 safety boundary
------------------------------------------------------
The operator's brief said "fully suppresses *all* APPROVAL_REQUIRED prompts."
Taken literally that would *remove the Zero-Order Doll* — a single signature (or
a stale 12-month-old one, or a forged one) could switch off the human gate on
the very operations the recursion bound exists to protect. We refuse to build
that. Instead this module enforces an **un-signable floor**:

    A signed roadmap MAY suppress approval for the SAFE, scoped work it
    explicitly authorizes. It can NEVER suppress approval for:
      • SAFETY-tier / BLOCKED operations,
      • Order-2 RSI (M10 — cognitive self-modification),
      • any op whose self-modification chain would exceed the recursion bound,
      • any op that touches the governance / cage substrate.
    Those ALWAYS escalate to live human review, regardless of signature.

So this is *delegated autonomy within a signed boundary* — never the removal of
the boundary. ``may_suppress_approval`` is the single chokepoint; every safety
class short-circuits it to ``False`` before the scope/signature is even read.

Crypto: composed, not reinvented — the exact HMAC-SHA256 token codec from
``aegis/lease.py`` (``_encode_token`` / ``_decode_token`` / ``TokenVerdictKind``).
Budget bounds: composed from ``recursion_depth_gate`` (the autonomous Slice-104
cap) and a hard budget ceiling — the roadmap can only *tighten*, never raise a
bound past the safety maximum.

Master switch: ``JARVIS_LAYER4_ROADMAP_ENABLED`` (default **false**, §33.1).
When off, ``unattended_mode_authorized`` is always ``False`` → the system stays
in per-PR human-review mode (the legacy, safe default).
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Compose the Aegis HMAC-SHA256 token codec — same construction, no reinvention.
from backend.core.ouroboros.aegis.lease import (  # noqa: E402
    TokenVerdictKind,
    _canonical_json,
    _decode_token,
    _encode_token,
)

# ---------------------------------------------------------------------------
# Master switch (§33.1 — default FALSE).
# ---------------------------------------------------------------------------
_ENV_MASTER = "JARVIS_LAYER4_ROADMAP_ENABLED"
_ENV_OPERATOR_KEY = "JARVIS_LAYER4_OPERATOR_KEY"          # hex-encoded HMAC key
_ENV_OPERATOR_KEYFILE = "JARVIS_LAYER4_OPERATOR_KEYFILE"  # path to raw key bytes
_ENV_HARD_MAX_BUDGET = "JARVIS_LAYER4_HARD_MAX_BUDGET_USD"

_DEFAULT_HARD_MAX_BUDGET_USD = 50.0


def layer4_enabled() -> bool:
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def _hard_max_budget_usd() -> float:
    try:
        return max(0.0, float(os.getenv(_ENV_HARD_MAX_BUDGET, _DEFAULT_HARD_MAX_BUDGET_USD)))
    except (TypeError, ValueError):
        return _DEFAULT_HARD_MAX_BUDGET_USD


def _hard_max_recursion_depth() -> int:
    """The autonomous Slice-104 recursion cap — the roadmap cannot exceed it."""
    try:
        from backend.core.ouroboros.governance.recursion_depth_gate import max_recursion_depth

        return int(max_recursion_depth())
    except Exception:  # pragma: no cover - defensive; gate always present in prod
        return 3


def _operator_key() -> Optional[bytes]:
    """The operator's signing key — the Zero-Order Doll's private authority.

    Returns ``None`` when no key is configured → every verification fails
    CLOSED (``MISSING``), so the system never runs unattended without the
    operator having provisioned a key.
    """
    keyfile = os.getenv(_ENV_OPERATOR_KEYFILE)
    if keyfile:
        try:
            with open(keyfile, "rb") as fh:
                raw = fh.read().strip()
            if raw:
                return raw
        except OSError as exc:
            logger.warning("[Layer4] operator keyfile unreadable (%s) — fail-closed", exc)
            return None
    hexkey = os.getenv(_ENV_OPERATOR_KEY)
    if hexkey:
        try:
            return bytes.fromhex(hexkey.strip())
        except ValueError:
            logger.warning("[Layer4] operator key not valid hex — fail-closed")
            return None
    return None


# ---------------------------------------------------------------------------
# Verdict + authorization records.
# ---------------------------------------------------------------------------
class RoadmapVerdictKind(str, enum.Enum):
    """Mirrors ``aegis.lease.TokenVerdictKind`` — fail-closed taxonomy."""

    VALID = "valid"
    MISSING = "missing"                  # no roadmap or no operator key
    INVALID_FORMAT = "invalid_format"    # unparseable / no signature field
    INVALID_SIGNATURE = "invalid_signature"
    EXPIRED = "expired"
    TAMPERED = "tampered"                # body hash != signed hash


# The SAFETY classes that NO signature can suppress (the un-signable floor).
# Risk-tier names are compared as upper-case strings to stay decoupled from the
# RiskTier enum's import surface.
_SAFETY_RISK_TIERS = frozenset({"APPROVAL_REQUIRED", "BLOCKED"})


@dataclasses.dataclass(frozen=True)
class RoadmapAuthorization:
    kind: RoadmapVerdictKind
    scopes: frozenset = dataclasses.field(default_factory=frozenset)
    max_budget_usd: float = 0.0
    max_recursion_depth: int = 0
    expires_at: int = 0
    detail: str = ""

    @property
    def is_valid(self) -> bool:
        return self.kind is RoadmapVerdictKind.VALID


_INVALID = RoadmapAuthorization(kind=RoadmapVerdictKind.MISSING)


# ---------------------------------------------------------------------------
# Phase 1 — cryptographic roadmap reader (fail-closed).
# ---------------------------------------------------------------------------
def sign_roadmap_body(body: Dict[str, Any], operator_key: bytes) -> str:
    """Operator-side helper: produce the detached signature token for a body.

    The signed artifact is ``{...body..., "signature": <token>}``. The token's
    payload binds the SHA-256 of the canonical body, so any tamper to the body
    invalidates the signature.
    """
    body_hash = hashlib.sha256(_canonical_json(_strip_signature(body))).hexdigest()
    payload: Dict[str, Any] = {"body_sha256": body_hash}
    exp = body.get("expires_at")
    if isinstance(exp, int):
        payload["exp"] = exp
    return _encode_token(operator_key, payload)


def _strip_signature(body: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in body.items() if k != "signature"}


def verify_signed_roadmap(body: Optional[Dict[str, Any]], *, now: int) -> RoadmapAuthorization:
    """Verify the operator's signature on a parsed roadmap body. Fail-closed.

    Any defect — missing key, missing/garbled signature, bad HMAC, hash
    mismatch, expiry — returns a non-VALID authorization, which downstream
    collapses unattended mode back to per-PR human review.
    """
    if not isinstance(body, dict) or not body:
        return dataclasses.replace(_INVALID, kind=RoadmapVerdictKind.INVALID_FORMAT, detail="empty/non-dict body")

    key = _operator_key()
    if key is None:
        return dataclasses.replace(_INVALID, kind=RoadmapVerdictKind.MISSING, detail="no operator key configured")

    token = body.get("signature")
    if not isinstance(token, str) or not token:
        return dataclasses.replace(_INVALID, kind=RoadmapVerdictKind.INVALID_FORMAT, detail="missing signature field")

    verdict = _decode_token(key, token)
    if verdict.kind is TokenVerdictKind.INVALID_SIGNATURE:
        return dataclasses.replace(_INVALID, kind=RoadmapVerdictKind.INVALID_SIGNATURE)
    if verdict.kind is not TokenVerdictKind.VALID or not isinstance(verdict.payload, dict):
        return dataclasses.replace(_INVALID, kind=RoadmapVerdictKind.INVALID_FORMAT, detail=f"token={verdict.kind.value}")

    # Tamper check: recompute the body hash and constant-compare.
    recomputed = hashlib.sha256(_canonical_json(_strip_signature(body))).hexdigest()
    signed_hash = verdict.payload.get("body_sha256")
    if not isinstance(signed_hash, str) or not _consteq(recomputed, signed_hash):
        return dataclasses.replace(_INVALID, kind=RoadmapVerdictKind.TAMPERED, detail="body_sha256 mismatch")

    # Expiry: the budget/evidence window. Past expiry → revoke unattended mode.
    expires_at = body.get("expires_at")
    if isinstance(expires_at, int) and now >= expires_at:
        return dataclasses.replace(
            _INVALID, kind=RoadmapVerdictKind.EXPIRED, expires_at=expires_at,
            detail=f"now={now} >= exp={expires_at}",
        )

    scopes = body.get("authorized_scopes") or []
    return RoadmapAuthorization(
        kind=RoadmapVerdictKind.VALID,
        scopes=frozenset(str(s) for s in scopes if isinstance(s, (str,))),
        max_budget_usd=_coerce_float(body.get("max_budget_usd"), 0.0),
        max_recursion_depth=_coerce_int(body.get("max_recursion_depth"), 0),
        expires_at=int(expires_at) if isinstance(expires_at, int) else 0,
        detail="ok",
    )


def _consteq(a: str, b: str) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(a, b)


def _coerce_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Phase 2 — dynamic budget & scope, bounded by hard safety ceilings.
# The roadmap can only TIGHTEN; it can never raise a bound past the safety max.
# ---------------------------------------------------------------------------
def effective_budget_usd(auth: RoadmapAuthorization) -> float:
    """Authorized budget, clamped to the hard ceiling. Invalid → 0."""
    if not auth.is_valid:
        return 0.0
    return min(auth.max_budget_usd, _hard_max_budget_usd())


def effective_recursion_depth(auth: RoadmapAuthorization) -> int:
    """Authorized recursion depth, clamped to the autonomous Slice-104 cap.

    A roadmap asking for depth 99 is silently clamped to the un-bypassable
    ``recursion_depth_gate`` maximum — the signature cannot buy more recursion
    than the mathematical safety bound allows.
    """
    hard = _hard_max_recursion_depth()
    if not auth.is_valid:
        return 0
    return max(0, min(auth.max_recursion_depth, hard))


# ---------------------------------------------------------------------------
# The un-signable floor — the §1 chokepoint.
# ---------------------------------------------------------------------------
def is_safety_operation(
    *,
    risk_tier: Optional[str] = None,
    is_order2_rsi: bool = False,
    recursion_exceeded: bool = False,
    touches_governance: bool = False,
) -> bool:
    """An op whose human gate NO signature may suppress.

    True for SAFETY/BLOCKED risk tiers, Order-2 (M10) cognitive self-mod, any op
    that would exceed the recursion bound, and any op touching the governance /
    cage substrate. These ALWAYS escalate to a live operator decision.
    """
    if is_order2_rsi or recursion_exceeded or touches_governance:
        return True
    if risk_tier is not None and str(risk_tier).strip().upper() in _SAFETY_RISK_TIERS:
        return True
    return False


def may_suppress_approval(
    auth: RoadmapAuthorization,
    *,
    op_scope: str,
    risk_tier: Optional[str] = None,
    is_order2_rsi: bool = False,
    recursion_exceeded: bool = False,
    touches_governance: bool = False,
) -> bool:
    """The SINGLE chokepoint deciding whether unattended auto-approval applies.

    Returns ``True`` (approval may be suppressed → the op runs unattended) ONLY
    when ALL hold:
      • the Layer-4 master is enabled,
      • the roadmap signature is VALID and unexpired,
      • the op is NOT a safety operation (the un-signable floor), AND
      • the op's scope was explicitly authorized in the signed roadmap.

    Safety operations short-circuit to ``False`` *before* the scope is read —
    so even a perfectly-valid roadmap cannot auto-approve an Order-2 RSI, a
    recursion-bound breach, or a governance-substrate change.
    """
    # Un-signable floor first — checked before signature/scope so it can never
    # be reasoned around.
    if is_safety_operation(
        risk_tier=risk_tier,
        is_order2_rsi=is_order2_rsi,
        recursion_exceeded=recursion_exceeded,
        touches_governance=touches_governance,
    ):
        return False
    if not layer4_enabled():
        return False
    if not auth.is_valid:
        return False
    return op_scope in auth.scopes


def unattended_mode_authorized(auth: RoadmapAuthorization) -> bool:
    """Whether the system may run unattended at all (the evidence-clock gate)."""
    return layer4_enabled() and auth.is_valid


_ENV_ROADMAP_PATH = "JARVIS_LAYER4_ROADMAP_PATH"
_DEFAULT_ROADMAP_PATH = ".jarvis/roadmap.signed.yaml"


def load_and_verify_roadmap(*, now: int) -> RoadmapAuthorization:
    """Read + verify ``.jarvis/roadmap.signed.yaml``. Always fail-closed.

    Any defect — file absent, YAML unparseable, signature bad — returns a
    non-VALID authorization. Never raises (a malformed roadmap must degrade the
    system to per-PR review, never crash the loop).
    """
    path = os.getenv(_ENV_ROADMAP_PATH, _DEFAULT_ROADMAP_PATH)
    try:
        if not os.path.exists(path):
            return dataclasses.replace(_INVALID, kind=RoadmapVerdictKind.MISSING, detail=f"no file at {path}")
        import yaml  # local import — only on the unattended path

        with open(path, "r", encoding="utf-8") as fh:
            body = yaml.safe_load(fh)
        return verify_signed_roadmap(body, now=now)
    except Exception as exc:  # noqa: BLE001 - fail-closed is the contract
        logger.warning("[Layer4] roadmap load failed (%s) — fail-closed", exc)
        return dataclasses.replace(_INVALID, kind=RoadmapVerdictKind.INVALID_FORMAT, detail=str(exc))


def degrade_reason(auth: RoadmapAuthorization) -> str:
    """Human-facing reason the system fell back to per-PR review."""
    if not layer4_enabled():
        return "layer4 master disabled — per-PR human review"
    if auth.is_valid:
        return "unattended authorized"
    return f"roadmap {auth.kind.value} ({auth.detail}) — degraded to per-PR human review"


__all__ = [
    "RoadmapVerdictKind",
    "RoadmapAuthorization",
    "layer4_enabled",
    "sign_roadmap_body",
    "verify_signed_roadmap",
    "effective_budget_usd",
    "effective_recursion_depth",
    "is_safety_operation",
    "may_suppress_approval",
    "unattended_mode_authorized",
    "load_and_verify_roadmap",
    "degrade_reason",
]
