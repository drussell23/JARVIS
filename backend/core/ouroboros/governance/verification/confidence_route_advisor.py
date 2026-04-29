"""Priority 1 Slice 4 — Confidence-aware advisory route advisor.

Pure-data advisor. Given (current_route, rolling-confidence stats,
posture) produces an OPTIONAL ``RouteProposal`` for the operator
surface — emitted as an SSE event via ``confidence_observability``.

The proposal is **always advisory**. It NEVER mutates ``ctx.route``
directly; it NEVER drives an automatic dispatch change. Operators
review proposals in the IDE stream + ``/postmortems`` distribution
view; any route change crosses the existing operator-approval-bound
surfaces.

Cost contract preservation (load-bearing)
-----------------------------------------

The advisor cannot propose any route change that would escalate a
BG/SPEC op to a higher-cost provider. Concretely:

  * ``current_route in {"background", "speculative"}`` AND
    ``proposed_route in {"standard", "complex", "immediate"}`` →
    ``CostContractViolation`` raised at the structural guard.

This is enforced at three composing layers:

  1. **AST pin** (``test_authority_no_bg_spec_escalation``) verifies
     the structural guard exists in ``_propose_route_change`` and
     raises ``CostContractViolation`` rather than returning the
     proposal.
  2. **Runtime guard** (``_propose_route_change`` body) raises
     ``CostContractViolation`` from
     ``cost_contract_assertion`` — same exception class the §26.6
     dispatcher gate uses, so existing ``except`` blocks catch it.
  3. **§26.6 Layer 2** in ``providers.py`` — even if a malformed
     proposal somehow reached a dispatch site, the ClaudeProvider
     boundary refuses BG/SPEC routes that aren't read-only.

Decision math (per scope doc)
-----------------------------

  * BG + recurring low-confidence → propose SPECULATIVE
    (further cost demote)
  * COMPLEX + recurring high-confidence → propose STANDARD
    (lighter cascade)
  * STANDARD + recurring high-confidence → propose BACKGROUND
    (cost demote — DW only is sufficient)
  * Other cells → no proposal (return ``None``)

"Recurring" is parameterized by:
  * ``JARVIS_CONFIDENCE_ROUTE_HISTORY_K`` (default 8) — number of
    op outcomes to look at
  * ``JARVIS_CONFIDENCE_ROUTE_LOW_FRACTION`` (default 0.5) —
    fraction of low-confidence ops needed to trigger a low-conf
    proposal
  * ``JARVIS_CONFIDENCE_ROUTE_HIGH_FRACTION`` (default 0.7) —
    fraction of high-confidence ops needed to trigger a high-conf
    proposal

Master flag
-----------

``JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED`` (default ``false``).
Independent from ``JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED`` (an
operator can enable observability without enabling the advisor,
or vice versa). Both default false in Slice 4.

Authority invariants (AST-pinned by tests)
------------------------------------------

  * No imports of orchestrator / phase_runners / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router (cost-contract isolation).
  * Pure stdlib + verification.* family (own slice's
    confidence_observability + the §26.6
    cost_contract_assertion for the exception class).
  * NEVER raises out of the public dispatcher EXCEPT
    ``CostContractViolation`` from the structural guard — and
    that is intentional: a violation is fatal, not recoverable.
  * ``_propose_route_change`` body MUST contain the cost-contract
    guard literal pattern (AST-pinned).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Sequence

from backend.core.ouroboros.governance.cost_contract_assertion import (
    COST_GATED_ROUTES,
    CostContractViolation,
)

logger = logging.getLogger(__name__)


CONFIDENCE_ROUTE_ADVISOR_SCHEMA_VERSION: str = "confidence_route_advisor.1"


# Routes the advisor knows about — duplicated as strings here rather
# than imported from urgency_router (which is a forbidden import per
# authority isolation). String comparison via lowercased equality.
_ROUTE_BACKGROUND: str = "background"
_ROUTE_SPECULATIVE: str = "speculative"
_ROUTE_STANDARD: str = "standard"
_ROUTE_COMPLEX: str = "complex"
_ROUTE_IMMEDIATE: str = "immediate"

# Routes considered "higher cost" than BG/SPEC. If the advisor were
# ever asked to propose any of these starting from BG/SPEC, that's
# a cost-contract violation.
_HIGHER_COST_ROUTES: frozenset = frozenset({
    _ROUTE_STANDARD, _ROUTE_COMPLEX, _ROUTE_IMMEDIATE,
})


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def confidence_route_routing_enabled() -> bool:
    """``JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED`` (default ``true`` —
    graduated in Priority 1 Slice 5).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy disables.
    Re-read at call time so monkeypatch + live toggle work.

    Cost contract preservation: even with this flag on, the
    advisor's structural guard (``_propose_route_change``) raises
    ``CostContractViolation`` on any BG/SPEC → higher-cost
    proposal. §26.6 four-layer defense-in-depth (AST invariant +
    runtime CostContractViolation in providers.py + Property
    Oracle claim + this advisor's AST-pinned guard) ensures
    cost contract holds regardless of route-advisor state.

    Hot-revert: ``export JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED=false``
    short-circuits ``propose_route_change`` to ``None`` always."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5 — was false in Slice 4)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Knobs (FlagRegistry-typed; values clamped defensively)
# ---------------------------------------------------------------------------


_DEFAULT_HISTORY_K: int = 8
_DEFAULT_LOW_FRACTION: float = 0.5
_DEFAULT_HIGH_FRACTION: float = 0.7


def confidence_route_history_k() -> int:
    """``JARVIS_CONFIDENCE_ROUTE_HISTORY_K`` (default 8). Number of
    recent op confidence outcomes to consult. Floored at 2 (a
    single observation isn't recurring).

    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_ROUTE_HISTORY_K", "",
    ).strip()
    if not raw:
        return _DEFAULT_HISTORY_K
    try:
        v = int(raw)
        return max(2, v)
    except (TypeError, ValueError):
        return _DEFAULT_HISTORY_K


def confidence_route_low_fraction() -> float:
    """``JARVIS_CONFIDENCE_ROUTE_LOW_FRACTION`` (default 0.5).
    Fraction of low-confidence ops in the history window required
    to trigger a low-conf proposal. Floored at 0.0; capped at 1.0.

    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_ROUTE_LOW_FRACTION", "",
    ).strip()
    if not raw:
        return _DEFAULT_LOW_FRACTION
    try:
        v = float(raw)
        if v != v:  # NaN
            return _DEFAULT_LOW_FRACTION
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return _DEFAULT_LOW_FRACTION


def confidence_route_high_fraction() -> float:
    """``JARVIS_CONFIDENCE_ROUTE_HIGH_FRACTION`` (default 0.7).
    Fraction of high-confidence ops required to trigger a high-conf
    proposal. Floored at low_fraction (so high ≥ low always).
    Capped at 1.0.

    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_CONFIDENCE_ROUTE_HIGH_FRACTION", "",
    ).strip()
    if not raw:
        return _DEFAULT_HIGH_FRACTION
    try:
        v = float(raw)
        if v != v:
            return _DEFAULT_HIGH_FRACTION
        return max(confidence_route_low_fraction(), min(1.0, v))
    except (TypeError, ValueError):
        return _DEFAULT_HIGH_FRACTION


# ---------------------------------------------------------------------------
# RouteProposal — frozen advisory record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteProposal:
    """An advisory proposal to demote ``current_route`` to
    ``proposed_route`` for cost reasons.

    ALWAYS advisory — operators review proposals in the IDE stream +
    ``/postmortems`` distribution view. The proposal is NEVER
    auto-applied; ``ctx.route`` is never mutated by this advisor.
    """

    current_route: str
    proposed_route: str
    reason_code: str
    confidence_basis: str
    rolling_margin: Optional[float] = None
    history_size: int = 0
    low_confidence_count: int = 0
    high_confidence_count: int = 0
    posture: str = ""
    op_id: str = ""
    provider: str = ""
    model_id: str = ""
    schema_version: str = CONFIDENCE_ROUTE_ADVISOR_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Cost-contract structural guard
# ---------------------------------------------------------------------------


def _propose_route_change(
    *,
    current_route: str,
    proposed_route: str,
    reason_code: str,
    confidence_basis: str,
    rolling_margin: Optional[float] = None,
    history_size: int = 0,
    low_confidence_count: int = 0,
    high_confidence_count: int = 0,
    posture: str = "",
    op_id: str = "",
    provider: str = "",
    model_id: str = "",
) -> RouteProposal:
    """Construct a RouteProposal with structural cost-contract guard.

    Cost contract structural pin (PRD §26.6 + scope doc Slice 4):
    if ``current_route in {"background", "speculative"}`` AND
    ``proposed_route in {"standard", "complex", "immediate"}``,
    raise ``CostContractViolation``. This is the AST-pinned guard
    that prevents any confidence-driven escalation from BG/SPEC to
    higher-cost routes."""
    cur_norm = (current_route or "").strip().lower()
    prop_norm = (proposed_route or "").strip().lower()
    if (
        cur_norm in COST_GATED_ROUTES
        and prop_norm in _HIGHER_COST_ROUTES
    ):
        raise CostContractViolation(
            op_id=op_id or "<unknown>",
            provider_route=cur_norm,
            provider_tier="(advisor_proposal)",
            is_read_only=False,
            provider_name=provider or "confidence_route_advisor",
            detail=(
                f"advisor proposed BG/SPEC → higher-cost route: "
                f"current={cur_norm} proposed={prop_norm} "
                f"reason={reason_code}"
            ),
        )
    return RouteProposal(
        current_route=cur_norm,
        proposed_route=prop_norm,
        reason_code=reason_code,
        confidence_basis=confidence_basis,
        rolling_margin=rolling_margin,
        history_size=int(history_size or 0),
        low_confidence_count=int(low_confidence_count or 0),
        high_confidence_count=int(high_confidence_count or 0),
        posture=posture,
        op_id=op_id,
        provider=provider,
        model_id=model_id,
    )


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def propose_route_change(
    *,
    current_route: str,
    confidence_history: Sequence[str],
    rolling_margin: Optional[float] = None,
    posture: str = "",
    op_id: str = "",
    provider: str = "",
    model_id: str = "",
) -> Optional[RouteProposal]:
    """Pure-data advisor. Returns a ``RouteProposal`` when the
    rolling-confidence pattern suggests a cost-side route change,
    or ``None`` when no proposal is warranted.

    Parameters
    ----------
    current_route : str
        Current ``ctx.provider_route`` value.
    confidence_history : Sequence[str]
        Per-op confidence verdicts over the rolling history window.
        Each element should be one of ``"ok"`` /
        ``"approaching_floor"`` / ``"below_floor"`` (the
        ConfidenceVerdict.value strings). Tolerates other strings —
        non-canonical values are treated as neither low nor high.
    rolling_margin : Optional[float]
        Most-recent op's rolling margin (informational; for
        operator surface).
    posture : str
        Current posture string. Informational; the advisor is
        posture-agnostic in Slice 4 but the surface preserves the
        field for Slice 5+ posture-relevant tuning.
    op_id, provider, model_id : str
        Provenance fields for the proposal.

    Decision logic
    --------------
      * BG + ≥ low_fraction low-conf ops → propose SPECULATIVE
      * COMPLEX + ≥ high_fraction high-conf ops → propose STANDARD
      * STANDARD + ≥ high_fraction high-conf ops → propose BACKGROUND
      * Other cells → ``None``

    Cost contract: this function delegates to
    ``_propose_route_change`` for construction, which raises
    ``CostContractViolation`` on any BG/SPEC → higher-cost
    proposal. Decision logic above never hits that case (we only
    propose downward demotions), but the structural guard is a
    defense-in-depth for future code.

    Returns
    -------
    Optional[RouteProposal]
        The proposal, or ``None`` if no change is warranted /
        master flag is off / history is too short.

    NEVER raises EXCEPT ``CostContractViolation`` from the
    structural guard. All other failure paths return ``None``.
    """
    if not confidence_route_routing_enabled():
        return None
    if not current_route:
        return None
    cur_norm = (current_route or "").strip().lower()

    safe_history = list(confidence_history or ())
    if len(safe_history) < 2:
        return None
    # Trim to the configured window
    history_k = confidence_route_history_k()
    if len(safe_history) > history_k:
        safe_history = safe_history[-history_k:]
    history_size = len(safe_history)

    # Tally low-conf vs high-conf vs other
    low_count = sum(
        1 for v in safe_history
        if str(v).strip().lower() in (
            "below_floor", "approaching_floor",
        )
    )
    high_count = sum(
        1 for v in safe_history if str(v).strip().lower() == "ok"
    )

    low_frac = low_count / history_size
    high_frac = high_count / history_size

    low_threshold = confidence_route_low_fraction()
    high_threshold = confidence_route_high_fraction()

    # BG + recurring low → propose SPECULATIVE (further cost demote)
    if cur_norm == _ROUTE_BACKGROUND and low_frac >= low_threshold:
        return _propose_route_change(
            current_route=cur_norm,
            proposed_route=_ROUTE_SPECULATIVE,
            reason_code="cost_demote_recurring_low_confidence_on_bg",
            confidence_basis=(
                f"low_count={low_count}/{history_size} "
                f"(>= {low_threshold:.2f})"
            ),
            rolling_margin=rolling_margin,
            history_size=history_size,
            low_confidence_count=low_count,
            high_confidence_count=high_count,
            posture=posture,
            op_id=op_id,
            provider=provider,
            model_id=model_id,
        )

    # COMPLEX + recurring high → propose STANDARD (lighter cascade)
    if cur_norm == _ROUTE_COMPLEX and high_frac >= high_threshold:
        return _propose_route_change(
            current_route=cur_norm,
            proposed_route=_ROUTE_STANDARD,
            reason_code="cost_demote_recurring_high_confidence_on_complex",
            confidence_basis=(
                f"high_count={high_count}/{history_size} "
                f"(>= {high_threshold:.2f})"
            ),
            rolling_margin=rolling_margin,
            history_size=history_size,
            low_confidence_count=low_count,
            high_confidence_count=high_count,
            posture=posture,
            op_id=op_id,
            provider=provider,
            model_id=model_id,
        )

    # STANDARD + recurring high → propose BACKGROUND (DW only)
    if cur_norm == _ROUTE_STANDARD and high_frac >= high_threshold:
        return _propose_route_change(
            current_route=cur_norm,
            proposed_route=_ROUTE_BACKGROUND,
            reason_code="cost_demote_recurring_high_confidence_on_standard",
            confidence_basis=(
                f"high_count={high_count}/{history_size} "
                f"(>= {high_threshold:.2f})"
            ),
            rolling_margin=rolling_margin,
            history_size=history_size,
            low_confidence_count=low_count,
            high_confidence_count=high_count,
            posture=posture,
            op_id=op_id,
            provider=provider,
            model_id=model_id,
        )

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CONFIDENCE_ROUTE_ADVISOR_SCHEMA_VERSION",
    "RouteProposal",
    "confidence_route_high_fraction",
    "confidence_route_history_k",
    "confidence_route_low_fraction",
    "confidence_route_routing_enabled",
    "propose_route_change",
]
