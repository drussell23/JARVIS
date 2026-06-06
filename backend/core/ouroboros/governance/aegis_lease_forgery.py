"""Slice 118 — Aegis Lease-Forgery Falsification.

Proves the existing Tier-D egress invariant cryptographically — no new daemon,
no duplication of Aegis. The FSM attaches an HMAC-signed Aegis lease to every
upstream API call (``aegis_provider_bridge`` in ``providers.py``); this Red-Team
class fires the three lease-forgery vectors at the *real* Aegis verifier
(``validate_lease_token``) and the Blue ledger (Slice 115) writes a tamper-evident
receipt for each rejection — undeniable, hash-chained proof that the egress path
fails-closed:

  * **no lease** (empty header)            → ``invalid_format``     (rejected)
  * **forged HMAC** (tampered signature)   → ``invalid_signature``  (rejected)
  * **expired lease** (exp in the past)    → ``expired``            (rejected)
  * **valid control** (proper lease)       → ``valid``              (accepted)

The valid control is load-bearing: it proves the verifier rejects forgeries
*specifically*, not by blanket-denying everything (which would be a dead engine,
not a cage). Composes the existing Aegis lease primitives + the Slice-115
``BlueEvidenceLedger`` — zero new safety substrate.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ouroboros.aegis_lease_forgery")

ATTACK_LEASE_FORGERY = "lease_forgery"


def _make_lease(K_route: str, *, issued_at: float, expires_at: float, nonce: str) -> Any:
    from backend.core.ouroboros.aegis.lease import Lease
    return Lease(
        nonce=nonce, op_id="forgery-probe", route=K_route,
        estimated_cost_usd=0.01, max_cost_usd=0.10,
        causal_lineage_hash="0" * 16, issued_at=issued_at, expires_at=expires_at,
    )


def forge_lease_vectors(K: bytes, now: float) -> List[Tuple[str, str, str, bool]]:
    """Build the forgery matrix: (name, wire_token, expected_verdict, is_forgery).
    Deterministic given (K, now). The valid control has ``is_forgery=False``."""
    from backend.core.ouroboros.aegis.lease import mint_lease_token

    valid_token = mint_lease_token(K, _make_lease("STANDARD", issued_at=now, expires_at=now + 300.0, nonce="valid-ctrl"))
    # Tamper ONLY the HMAC segment (after the '.') — payload intact, signature forged.
    head, _sig = valid_token.rsplit(".", 1)
    bad_hmac_token = head + "." + ("A" * max(43, len(_sig)))
    expired_token = mint_lease_token(K, _make_lease("STANDARD", issued_at=now - 600.0, expires_at=now - 100.0, nonce="expired-1"))
    return [
        ("no_lease", "", "invalid_format", True),
        ("forged_hmac", bad_hmac_token, "invalid_signature", True),
        ("expired_lease", expired_token, "expired", True),
        ("valid_control", valid_token, "valid", False),
    ]


def run_lease_forgery_siege(
    *,
    K: Optional[bytes] = None,
    ledger: Any = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Fire every lease-forgery vector at the REAL Aegis verifier and write a
    Blue receipt per outcome. Returns a summary {vector: verdict_kind} plus
    ``all_forgeries_rejected`` / ``valid_accepted``. NEVER raises."""
    from backend.core.ouroboros.aegis.lease import (
        NonceLedger,
        TokenVerdictKind,
        validate_lease_token,
    )

    key = K if K is not None else os.urandom(32)
    t = now if now is not None else time.time()
    led = ledger
    if led is None:
        from backend.core.ouroboros.governance.red_blue_matrix import BlueEvidenceLedger
        led = BlueEvidenceLedger()

    verdicts: Dict[str, str] = {}
    forgeries_rejected = True
    valid_accepted = False
    try:
        for name, token, expected, is_forgery in forge_lease_vectors(key, t):
            # Each validation gets a FRESH nonce ledger so replay-protection
            # never confounds the forgery measurement.
            verdict = validate_lease_token(key, token, now_s=t, nonce_ledger=NonceLedger())
            kind = getattr(verdict.kind, "value", str(verdict.kind))
            verdicts[name] = kind
            is_valid = kind == TokenVerdictKind.VALID.value
            if is_forgery:
                # A forgery is "blocked" iff the verifier did NOT accept it.
                blocked = not is_valid
                forgeries_rejected = forgeries_rejected and blocked
                led.record(
                    attack_class=ATTACK_LEASE_FORGERY,
                    payload=f"lease_forgery:{name}:{token[:40]}",
                    verdict=kind, blocked=blocked,
                    blocked_by="aegis_lease_verifier" if blocked else "",
                )
            else:
                valid_accepted = is_valid
                led.record(
                    attack_class=ATTACK_LEASE_FORGERY,
                    payload=f"lease_valid_control:{name}",
                    verdict=kind, blocked=False, blocked_by="",
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[LeaseForgery] siege swallowed: %s", exc)
    return {
        "verdicts": verdicts,
        "all_forgeries_rejected": forgeries_rejected,
        "valid_accepted": valid_accepted,
    }


def bridge_fails_closed() -> bool:
    """Source-level invariant check: ``aegis_provider_bridge.acquire_call_lease``
    RAISES on failure with NO silent fallback to direct upstream credentials —
    so a provider call cannot proceed without a lease. Inspects the source (no
    execution). NEVER raises."""
    try:
        import inspect
        from backend.core.ouroboros.governance import aegis_provider_bridge as B
        src = inspect.getsource(B.acquire_call_lease)
        # Invariant: NO exception-swallowing in the lease-acquisition path — any
        # Aegis failure propagates to the caller (the AegisClientError raised by
        # client.acquire_lease is NOT caught), so the upstream call cannot
        # proceed without a lease. Presence of an `except` would be a silent
        # fallback; its ABSENCE is the fail-closed proof.
        return "except" not in src and "acquire_lease" in src
    except Exception:  # noqa: BLE001
        return False
