"""Terminal-reason classification — Slice 12P Phase 2.

Closed taxonomy for ``operation_terminal`` causes, lifting them
from grep-only strings in debug.log into structured fields on
summary.json. NEVER raises.

The wedge: summary.json.operations[] currently contains no
terminal-reason attribution, so operators chasing why an op died
(provider exhaustion vs. structural rejection vs. cost cap vs.
shutdown) have to grep debug.log. Slice 12P closes that gap.

Classification is PURE — composes the canonical
``terminal_reason_code`` string the breaker / Iron Gate / cost
governor / shutdown waiter / Slice 12O cooldown produce. New
terminal reasons added in future slices MUST extend this enum +
classifier (AST-pinned).
"""
from __future__ import annotations

import enum
import logging
from typing import Optional

logger = logging.getLogger("Ouroboros.TerminalReason")


class TerminalReasonClass(str, enum.Enum):
    """Closed 6-value taxonomy of op-terminal causes. Bubbles up
    into ``summary.json.operations[].terminal_reason_class`` so
    operators can attribute terminations without grepping
    debug.log."""

    PROVIDER_EXHAUSTION = "provider_exhaustion"
    """Upstream provider refused requests — DW + Claude both
    exhausted, circuit-breaker terminal_structural / terminal_quota.
    Eligible for Slice 12O macro-cooldown retry."""

    STRUCTURAL_GATE_REJECTION = "structural_gate_rejection"
    """Internal gate (Iron Gate exploration, ASCII strictness,
    SemanticGuardian, AdversarialReviewer) rejected the
    generation. NOT eligible for Slice 12O cooldown — would
    re-trigger the same rejection. Slice 12P Phase 3 reflexive
    healing applies here."""

    COST_BUDGET_EXHAUSTED = "cost_budget_exhausted"
    """CostGovernor floor breached — remaining budget below
    minimum-viable-attempt threshold. Op terminates cleanly."""

    WALL_CLOCK_CAP = "wall_clock_cap"
    """WallClockWatchdog Layer-2 graceful shutdown fired —
    overall session wall budget exceeded."""

    CANCELLED_SHUTDOWN = "cancelled_shutdown"
    """asyncio cancellation propagated to the op during
    coordinated shutdown (Slice 12O cooldown_cancelled_shutdown,
    session_exhausted shutdown cascade)."""

    OTHER = "other"
    """Defensive fallback for terminal reasons not yet classified.
    A new terminal_reason_code that lands in OTHER signals a
    classifier gap — extend the substring rules below."""


# Pure substring rules (first-match-wins, in priority order so
# more-specific patterns can pre-empt less-specific ones). All
# matches are lowercased on both sides to be case-tolerant.
#
# AST-pinned: the test surface walks this dict + asserts every
# TerminalReasonClass value (except OTHER) has at least one entry.

_CLASSIFIER_RULES: tuple = (
    # CANCELLED_SHUTDOWN — must come BEFORE PROVIDER_EXHAUSTION
    # because cooldown_cancelled_shutdown is structurally cleaner
    # information than the underlying exhaustion that triggered
    # the cooldown.
    ("cooldown_cancelled_shutdown",
     TerminalReasonClass.CANCELLED_SHUTDOWN),
    ("session_exhausted_shutdown",
     TerminalReasonClass.CANCELLED_SHUTDOWN),
    ("cancelled_during_shutdown",
     TerminalReasonClass.CANCELLED_SHUTDOWN),

    # WALL_CLOCK_CAP — distinct from CANCELLED_SHUTDOWN because
    # it's the upstream session-wide signal vs. per-op cancel.
    ("wall_clock_cap", TerminalReasonClass.WALL_CLOCK_CAP),

    # COST_BUDGET_EXHAUSTED — distinct from PROVIDER_EXHAUSTION
    # because the budget signal is internal-resource-driven, not
    # upstream-provider-driven.
    ("budget_floor_breached", TerminalReasonClass.COST_BUDGET_EXHAUSTED),
    ("cost_cap_reached", TerminalReasonClass.COST_BUDGET_EXHAUSTED),
    ("budget_exhausted", TerminalReasonClass.COST_BUDGET_EXHAUSTED),

    # STRUCTURAL_GATE_REJECTION — Iron Gate + downstream gates.
    # Listed before PROVIDER_EXHAUSTION so an exploration_insufficient
    # rejection (which then chains to circuit_breaker_tripped) gets
    # attributed to its ROOT cause (the gate), not its symptom.
    ("exploration_insufficient",
     TerminalReasonClass.STRUCTURAL_GATE_REJECTION),
    ("ascii_gate_failed",
     TerminalReasonClass.STRUCTURAL_GATE_REJECTION),
    ("semantic_guard_",
     TerminalReasonClass.STRUCTURAL_GATE_REJECTION),
    ("adversarial_reviewer_rejected",
     TerminalReasonClass.STRUCTURAL_GATE_REJECTION),
    ("iron_gate_",
     TerminalReasonClass.STRUCTURAL_GATE_REJECTION),

    # PROVIDER_EXHAUSTION — must come LAST in this group because
    # other rejections can chain through the circuit breaker.
    ("circuit_breaker_tripped:terminal_structural",
     TerminalReasonClass.PROVIDER_EXHAUSTION),
    ("circuit_breaker_tripped:terminal_quota",
     TerminalReasonClass.PROVIDER_EXHAUSTION),
    ("all_providers_exhausted",
     TerminalReasonClass.PROVIDER_EXHAUSTION),
    ("provider_exhausted",
     TerminalReasonClass.PROVIDER_EXHAUSTION),
    ("stream_rupture",
     TerminalReasonClass.PROVIDER_EXHAUSTION),
    ("stream_disconnected",
     TerminalReasonClass.PROVIDER_EXHAUSTION),
    ("stream_eof",
     TerminalReasonClass.PROVIDER_EXHAUSTION),
    ("stream_timeout",
     TerminalReasonClass.PROVIDER_EXHAUSTION),
)


def classify_terminal_reason(
    code: Optional[str],
) -> TerminalReasonClass:
    """Pure classifier. Returns the matching TerminalReasonClass
    for the given terminal_reason_code. Empty / None / unmatched
    → OTHER. NEVER raises."""
    if not isinstance(code, str) or not code:
        return TerminalReasonClass.OTHER
    code_lower = code.lower()
    for needle, klass in _CLASSIFIER_RULES:
        if needle in code_lower:
            return klass
    return TerminalReasonClass.OTHER


def is_reflexive_healing_eligible(
    code: Optional[str],
) -> bool:
    """True iff the terminal_reason_code is a structural gate
    rejection — the class where Slice 12P Phase 3 reflexive
    healing can usefully feed a strict developer-feedback prompt
    back to the model. Provider exhaustion is NOT eligible
    (Slice 12O cooldown handles those); shutdown / wall / budget
    causes terminate the op cleanly with no useful retry."""
    return classify_terminal_reason(code) == \
        TerminalReasonClass.STRUCTURAL_GATE_REJECTION


__all__ = [
    "TerminalReasonClass",
    "classify_terminal_reason",
    "is_reflexive_healing_eligible",
]
