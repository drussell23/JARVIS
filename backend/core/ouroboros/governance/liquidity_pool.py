"""Dynamic Liquidity Pool — keep background autonomy alive during a DW-RT outage.

The SPECULATIVE/BACKGROUND lanes are DW-only by cost design (Manifesto §5). When
DW realtime is ENTIRELY entitlement-denied (all RT models 403 → the DW
``DIRECT_STREAMING`` surface reads ``AUTH_FAILED``), those lanes STARVE: every op
takes ``DoublewordInfraError`` → per-op breaker ``terminal_quota`` → Claude
fallback BLOCKED by ``CostContractViolation`` (BG/SPEC may not cascade to Claude)
→ ``EXHAUSTION`` at GENERATE, never reaching APPLY (observed bt-2026-07-18-091549).

A *static* "allow Claude for background" escape valve would violate the cost
contract. This pool is the ADAPTIVE alternative: when DW-RT is entirely denied
AND the session budget is HEALTHY (a configurable fraction still unspent), it
issues a bounded, instantly-revocable **Liquidity Lease** that elevates ONE
speculative/background op to STANDARD (Claude-capable) — up to a session
micro-budget ceiling. The instant DW-RT recovers OR the budget tightens, no new
lease issues (the gate re-evaluates on every classification, so "revoke" is
simply the next evaluation declining).

Design:
  * **DRY** — reuses ``SessionBudgetAuthority`` (money), ``provider_availability``
    (DW-RT health via the ``DIRECT_STREAMING`` surface verdict), and the
    ``ProviderRoute`` ``.value`` strings. No new tracking system.
  * **Cycle-free** — takes/returns plain strings (route ``.value``, ``op_id``); the
    ``ProviderRoute.STANDARD`` substitution stays in ``urgency_router``, so this
    module never imports the router.
  * **Fail-soft** — any fault (or unregistered budget authority) DENIES elevation,
    i.e. falls back to the legacy DW-only behavior. NEVER raises into routing.
  * **Bounded** — the reservation is conservative + monotonic per session; once
    the micro-ceiling is reached, no further leases issue this session.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

LIQUIDITY_POOL_SCHEMA_VERSION = "liquidity_pool.v1"

# Only the DW-only, cost-optimized lanes are elevation-eligible.
_ELEVATION_ELIGIBLE = ("speculative", "background")
_FALSY = ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Env knobs — clamped, hot-revertable, read at call time.
# ---------------------------------------------------------------------------


def liquidity_pool_enabled() -> bool:
    """``JARVIS_LIQUIDITY_POOL_ENABLED`` (default ON). Off → no elevation ever
    (byte-identical legacy DW-only routing). NEVER raises."""
    return os.environ.get(
        "JARVIS_LIQUIDITY_POOL_ENABLED", "true",
    ).strip().lower() not in _FALSY


def min_budget_ratio() -> float:
    """``JARVIS_LIQUIDITY_MIN_BUDGET_RATIO`` (default 0.5) — the minimum fraction
    of session budget that must remain UNSPENT for a lease to issue. Clamped
    [0.0, 1.0]. NEVER raises."""
    try:
        v = float(os.environ.get("JARVIS_LIQUIDITY_MIN_BUDGET_RATIO", "0.5"))
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return 0.5


def micro_ceiling_usd() -> float:
    """``JARVIS_LIQUIDITY_MICRO_CEILING_USD`` (default 0.50) — the SESSION
    cumulative ceiling on lease-elevated spend. Once reached, no further leases
    issue this session. Clamped >= 0. NEVER raises."""
    try:
        return max(0.0, float(
            os.environ.get("JARVIS_LIQUIDITY_MICRO_CEILING_USD", "0.50"),
        ))
    except (TypeError, ValueError):
        return 0.50


def per_op_estimate_usd() -> float:
    """``JARVIS_LIQUIDITY_PER_OP_ESTIMATE_USD`` (default 0.05) — the conservative
    amount reserved against the micro-ceiling at each lease grant (a STANDARD
    Claude-op cost estimate). The reservation is monotonic per session so the
    ceiling behaves as a hard session cap even without post-hoc reconciliation.
    Clamped >= 0. NEVER raises."""
    try:
        return max(0.0, float(
            os.environ.get("JARVIS_LIQUIDITY_PER_OP_ESTIMATE_USD", "0.05"),
        ))
    except (TypeError, ValueError):
        return 0.05


# ---------------------------------------------------------------------------
# Session lease state (module singleton — one pool per process/session).
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_leased_ops: Set[str] = set()
_reserved_usd: float = 0.0


# ---------------------------------------------------------------------------
# The two reused signals (both sync + fail-soft).
# ---------------------------------------------------------------------------


def _dw_rt_denied() -> bool:
    """True iff DW realtime is ENTIRELY entitlement-denied this session — the
    ``DIRECT_STREAMING`` surface reads ``AUTH_FAILED`` (all RT models 403). Reuses
    ``provider_availability.collect_provider_availability`` (sub-ms, cached ledger
    read). Fail-soft → False (no elevation) on any fault. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.provider_availability import (  # noqa: PLC0415
            collect_provider_availability,
        )
        snap = collect_provider_availability()
        return (not getattr(snap, "dw_healthy", True)) and (
            str(getattr(snap, "dw_reason", "")) == "auth_failed"
        )
    except Exception:  # noqa: BLE001
        return False


def _unspent_budget_ratio() -> float:
    """Fraction of session budget still UNSPENT (remaining / total). Reuses
    ``SessionBudgetAuthority``. Fail-soft → 1.0 (treat as full budget) when no
    authority is registered — matches the codebase idiom. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.session_budget_authority import (  # noqa: PLC0415
            get_session_remaining_usd,
            get_session_total_cap_usd,
        )
        remaining = get_session_remaining_usd()
        total = get_session_total_cap_usd()
        if remaining is None or total is None or total <= 0.0:
            return 1.0
        return max(0.0, min(1.0, float(remaining) / float(total)))
    except Exception:  # noqa: BLE001
        return 1.0


# ---------------------------------------------------------------------------
# The lease decision — the middleware evaluator.
# ---------------------------------------------------------------------------


def should_elevate(*, route_value: str, op_id: str = "") -> Optional[str]:
    """Decide whether a Liquidity Lease elevates a SPECULATIVE/BACKGROUND op to
    STANDARD. Returns a short cause tag (``"dw_rt_denied"``) to embed in the route
    reason, or ``None`` for no elevation.

    The 3-way truth table (all other cases deny):

      * route not SPECULATIVE/BACKGROUND, or pool disabled  → None
      * DW-RT HEALTHY                                         → None (deny)
      * DW-RT denied but budget TIGHT (< min ratio)          → None (deny)
      * DW-RT denied AND budget ABUNDANT AND ceiling headroom → grant

    Per-op dedup: a re-classified op keeps its lease without re-charging the
    ceiling. The reservation is conservative + monotonic (a hard session
    micro-cap). NEVER raises (fault → None = legacy DW-only)."""
    global _reserved_usd
    try:
        if not liquidity_pool_enabled():
            return None
        rv = str(route_value or "").strip().lower()
        if rv not in _ELEVATION_ELIGIBLE:
            return None
        if not _dw_rt_denied():
            return None                       # (a) DW-RT healthy → deny
        ratio = _unspent_budget_ratio()
        if ratio < min_budget_ratio():
            return None                       # (b) budget tight → deny
        oid = str(op_id or "").strip()
        est = per_op_estimate_usd()
        with _lock:
            if oid and oid in _leased_ops:
                return "dw_rt_denied"         # already leased (re-classify) — no re-charge
            if _reserved_usd + est > micro_ceiling_usd():
                return None                   # micro-ceiling exhausted → deny
            if oid:
                _leased_ops.add(oid)
            _reserved_usd += est
            reserved_now = _reserved_usd
        logger.warning(
            "[LiquidityPool] lease GRANTED op=%s %s→standard (dw_rt_denied, "
            "budget_unspent=%.2f, reserved=$%.2f/$%.2f ceiling)",
            oid or "?", rv, ratio, reserved_now, micro_ceiling_usd(),
        )
        return "dw_rt_denied"
    except Exception as exc:  # noqa: BLE001 — routing must never break
        logger.debug("[LiquidityPool] should_elevate degraded: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Observability + test hooks.
# ---------------------------------------------------------------------------


def stats() -> Dict[str, Any]:
    """Read-only snapshot for the observability surface. NEVER raises."""
    try:
        with _lock:
            reserved = _reserved_usd
            leased = len(_leased_ops)
        return {
            "enabled": liquidity_pool_enabled(),
            "dw_rt_denied": _dw_rt_denied(),
            "budget_unspent_ratio": round(_unspent_budget_ratio(), 3),
            "min_budget_ratio": min_budget_ratio(),
            "micro_ceiling_usd": micro_ceiling_usd(),
            "reserved_usd": round(reserved, 4),
            "leased_ops": leased,
            "schema_version": LIQUIDITY_POOL_SCHEMA_VERSION,
        }
    except Exception:  # noqa: BLE001
        return {"schema_version": LIQUIDITY_POOL_SCHEMA_VERSION}


def _reset_for_tests() -> None:
    """Test helper — clear the session lease state. Production MUST NOT call.
    NEVER raises."""
    global _reserved_usd
    try:
        with _lock:
            _leased_ops.clear()
            _reserved_usd = 0.0
    except Exception:  # noqa: BLE001
        _reserved_usd = 0.0
