"""Cost Contract Runtime Assertion — Layer 2 of the §26.6 reinforcement.

Closes the Static Pricing Blindspot triage's structural-cost gap. The
contract (post-Phase-12, refined from PRD §26.6 simplification):

  * **SPEC route**: NO Claude cascade, ever. No exceptions.
  * **BG route**: Claude cascade is permitted ONLY when
    ``is_read_only=True`` (Manifesto §5 Nervous System Reflex —
    survival supersedes cost optimization for read-only ops because
    no mutation can happen).
  * **STANDARD / COMPLEX / IMMEDIATE**: Claude is the intended brain;
    no contract restriction.

Invariant:
  * ``provider_tier == "claude"`` AND ``provider_route in
    {"background", "speculative"}`` AND NOT ``is_read_only`` →
    raise ``CostContractViolation``.

Three independent layers compose:
  * Layer 1 (AST) — `meta/shipped_code_invariants.py` seeds
    `cost_contract_bg_spec_no_unguarded_cascade` +
    `providers_cost_contract_assertion_wired` validators (boot + APPLY).
  * **Layer 2 (this module)** — runtime fatal-exception barrier at the
    `ClaudeProvider.generate` entry point.
  * Layer 3 (claim) — `verification.default_claims` seeds the
    `cost.bg_op_used_claude_must_be_false` per-op postmortem claim.

A future model patch would have to weaken ALL THREE layers
simultaneously to break the contract. The Order-2 manifest cage
(Pass B Slice 6.3 — `meta/order2_review_queue.py`) prevents
unauthorized weakening of Layer 1; this module prevents runtime
violation of the contract even if an unmerged candidate slipped past.

Architecture
------------

  * ``CostContractViolation`` — Exception class. Inherits ``Exception``
    (not ``RuntimeError``) to make ``except Exception`` catches that
    mean to swallow normal errors NOT swallow this one accidentally.
  * ``assert_provider_route_compatible(...)`` — pure-function gate.
    Master-flag-gated; never raises when off; raises
    CostContractViolation when on AND contract is violated.
  * ``cost_contract_runtime_assert_enabled()`` — master flag with
    asymmetric env semantics (default true).

Master flag
-----------

``JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED`` (default ``true``).
When off, ``assert_provider_route_compatible`` short-circuits to a
no-op — useful for the existing Qwen-397B benchmark workflow that
explicitly disables the cost-contract gate via
``JARVIS_DISABLE_CLAUDE_FALLBACK_ROUTES`` (legacy isolation override).

Hot-revert
----------

Single env knob — ``export JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED=false``
returns the system to pre-§26.6 behavior. Layer 1 (AST) and Layer 3
(claim) remain active.

Authority invariants (AST-pinned by tests):
  * No imports of orchestrator / phase_runner / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian.
  * Pure stdlib (logging, os) only.
  * NEVER raises for any input that is not a contract violation.
  * Read-only — never modifies its inputs.
  * The CostContractViolation exception class is the ONLY symbol that
    raises out of this module's public surface.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


COST_CONTRACT_ASSERTION_SCHEMA_VERSION: str = "cost_contract_assertion.1"


# ---------------------------------------------------------------------------
# Routes that never cascade to Claude (with documented exception)
# ---------------------------------------------------------------------------
#
# These are the routes the cost contract gates. STANDARD / COMPLEX /
# IMMEDIATE are deliberately NOT in this set — they're the routes
# Claude is intended for.

BG_ROUTE: str = "background"
SPEC_ROUTE: str = "speculative"

# Routes the contract gates. Tuple-of-strings, not a frozenset, so
# the AST validator (Layer 1) can pattern-match against the symbol.
COST_GATED_ROUTES: tuple = (BG_ROUTE, SPEC_ROUTE)


# Provider tier classifier — keyed on string, not enum, so this module
# stays unaware of provider implementations. The `ClaudeProvider` is
# tagged externally (provider_tier="claude") at the dispatch site.
CLAUDE_TIER: str = "claude"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def cost_contract_runtime_assert_enabled() -> bool:
    """``JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED`` (default ``true``).

    Asymmetric env semantics — empty/whitespace = unset marker =
    graduated default; explicit false-class hot-reverts."""
    raw = os.environ.get(
        "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Exception class
# ---------------------------------------------------------------------------


class CostContractViolation(Exception):
    """Raised when a BG/SPEC op attempts to dispatch to Claude in
    violation of the cost contract.

    Inherits Exception directly (not RuntimeError) so that defensive
    ``except Exception`` blocks that mean to swallow recoverable
    errors don't accidentally swallow this. Callers that legitimately
    catch this MUST do so explicitly via
    ``except CostContractViolation``.

    The orchestrator's policy is to:
      1. Catch this exception at the op boundary.
      2. Terminate the op with ``failure_class="cost_contract_violation"``.
      3. Write a ``must_hold_failed`` postmortem record (Layer 3 claim
         catches the empirical signal).
      4. Refuse further work on that op (no retry — the contract is
         not retry-correctable; the op was misrouted upstream).
    """

    def __init__(
        self,
        *,
        op_id: str,
        provider_route: str,
        provider_tier: str,
        is_read_only: bool,
        provider_name: str = "",
        detail: str = "",
    ) -> None:
        self.op_id = op_id
        self.provider_route = provider_route
        self.provider_tier = provider_tier
        self.is_read_only = is_read_only
        self.provider_name = provider_name
        self.detail = detail
        msg = (
            f"CostContractViolation: op={op_id!r} "
            f"route={provider_route!r} provider_tier={provider_tier!r} "
            f"is_read_only={is_read_only} provider={provider_name!r}. "
            f"BG/SPEC route attempted Claude cascade outside the "
            f"read-only Nervous System Reflex (Manifesto §5). "
            f"Cost contract per project_bg_spec_sealed.md + PRD §26.6."
        )
        if detail:
            msg = f"{msg} detail={detail!r}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Public assertion gate
# ---------------------------------------------------------------------------


def _normalize_route(route: Any) -> str:
    """Best-effort string normalization. NEVER raises."""
    try:
        if route is None:
            return ""
        return str(route).strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _normalize_tier(tier: Any) -> str:
    """Best-effort string normalization. NEVER raises."""
    try:
        if tier is None:
            return ""
        return str(tier).strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _normalize_bool(value: Any) -> bool:
    """Best-effort bool coercion. Defensive: unrecognized → False
    (so an op with corrupted is_read_only metadata does NOT
    accidentally pass through the gate as read-only). NEVER raises."""
    try:
        if value is True:
            return True
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if isinstance(value, (int, float)):
            return bool(value)
        return False
    except Exception:  # noqa: BLE001
        return False


def assert_provider_route_compatible(
    *,
    op_id: str,
    provider_route: Any,
    provider_tier: Any,
    is_read_only: Any,
    provider_name: str = "",
    detail: str = "",
) -> None:
    """Cost contract structural assertion — the §26.6.2 runtime gate.

    Raises ``CostContractViolation`` iff:
      * Master flag is on (``cost_contract_runtime_assert_enabled``)
      * provider_tier is "claude"
      * provider_route is in COST_GATED_ROUTES (background or
        speculative)
      * AND NOT (provider_route == "background" AND is_read_only=True)

    Otherwise returns None.

    Parameters
    ----------
    op_id:
        Op identifier. Used for diagnostic messaging only.
    provider_route:
        The op's provider_route ("background" / "speculative" /
        "standard" / "complex" / "immediate"). Case-insensitive.
    provider_tier:
        The dispatching provider's tier ("claude" / "doubleword" /
        "prime"). Case-insensitive.
    is_read_only:
        Whether the op is read-only. Manifesto §5 Nervous System
        Reflex — read-only BG ops MAY cascade because no mutation
        can occur. Coerced to bool defensively (unrecognized → False
        so corrupted metadata fails closed).
    provider_name:
        Diagnostic name (e.g., "claude-api"). Optional.
    detail:
        Free-form diagnostic context (e.g., the calling site).
        Optional.

    Raises
    ------
    CostContractViolation:
        When the contract is violated AND the master flag is on.

    Master-off behavior:
        Returns None silently. No log emission (avoids noise during
        legacy isolation override + benchmark workflows).
    """
    if not cost_contract_runtime_assert_enabled():
        return

    tier_norm = _normalize_tier(provider_tier)
    if tier_norm != CLAUDE_TIER:
        return  # only Claude triggers the contract gate

    route_norm = _normalize_route(provider_route)
    if route_norm not in COST_GATED_ROUTES:
        return  # only BG/SPEC are cost-gated

    read_only_norm = _normalize_bool(is_read_only)

    # Nervous System Reflex (Manifesto §5) — only BG, NOT SPEC.
    if route_norm == BG_ROUTE and read_only_norm:
        return

    # Contract violated — raise loud.
    logger.error(
        "[CostContract] VIOLATION op=%s route=%s tier=%s "
        "is_read_only=%s provider=%s detail=%s",
        op_id, route_norm, tier_norm, read_only_norm,
        provider_name, detail,
    )
    raise CostContractViolation(
        op_id=str(op_id) if op_id else "<unknown>",
        provider_route=route_norm,
        provider_tier=tier_norm,
        is_read_only=read_only_norm,
        provider_name=provider_name,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Convenience: classify an op's contract status without raising
# ---------------------------------------------------------------------------


def classify_route_compatibility(
    *,
    provider_route: Any,
    provider_tier: Any,
    is_read_only: Any,
) -> str:
    """Pure classification helper. Returns one of:
      * ``"ok"`` — no contract restriction applies
      * ``"reflex_allowed"`` — BG + read-only Claude cascade
        (Manifesto §5)
      * ``"violation"`` — contract violated
      * ``"non_claude"`` — provider isn't Claude, contract n/a

    Used by Layer 3 (Property Oracle claim) to decide pass/fail
    without raising. NEVER raises."""
    tier_norm = _normalize_tier(provider_tier)
    if tier_norm != CLAUDE_TIER:
        return "non_claude"
    route_norm = _normalize_route(provider_route)
    if route_norm not in COST_GATED_ROUTES:
        return "ok"
    read_only_norm = _normalize_bool(is_read_only)
    if route_norm == BG_ROUTE and read_only_norm:
        return "reflex_allowed"
    return "violation"


__all__ = [
    "BG_ROUTE",
    "CLAUDE_TIER",
    "COST_CONTRACT_ASSERTION_SCHEMA_VERSION",
    "COST_GATED_ROUTES",
    "CostContractViolation",
    "SPEC_ROUTE",
    "assert_provider_route_compatible",
    "classify_route_compatibility",
    "cost_contract_runtime_assert_enabled",
]
