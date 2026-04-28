"""Phase 2 — Closed-Loop Self-Verification (PRD §24.10 Critical Path #2).

Architectural foundation for verification beyond replay. Without
closed-loop verification, RSI converges on metric-gaming, not
capability gain.

Phase 2 layering:

  * Slice 2.1 — PropertyOracle primitive (pure-function dispatcher).
  * Slice 2.2 — RepeatRunner (statistical re-verification).
  * Slice 2.3 — property_capture (PLAN-time claim recording).
  * Slice 2.4 — POSTMORTEM verification-failure integration.
  * Slice 2.5 — Graduation flip.

Public surface (Slice 2.1):

  * PropertyOracle / Property / PropertyVerdict / PropertyEvaluator
  * VerdictKind (PASSED / FAILED / INSUFFICIENT_EVIDENCE / EVALUATOR_ERROR)
  * oracle_enabled / get_default_oracle / register_evaluator

Authority invariants (pinned by tests):
  * NEVER imports orchestrator / phase_runner / candidate_generator —
    verification is a substrate primitive, NOT a cognitive consumer.
  * NEVER raises out of any public method — defensive everywhere.
  * Pure stdlib + Antigravity canonical_hash adapter only.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.verification.property_oracle import (
    Property,
    PropertyEvaluator,
    PropertyOracle,
    PropertyVerdict,
    VerdictKind,
    get_default_oracle,
    oracle_enabled,
    register_evaluator,
)
from backend.core.ouroboros.governance.verification.repeat_runner import (
    EvidenceCollector,
    RepeatRunner,
    RepeatVerdict,
    RunBudget,
    get_default_runner,
    repeat_runner_enabled,
)

__all__ = [
    "EvidenceCollector",
    "Property",
    "PropertyEvaluator",
    "PropertyOracle",
    "PropertyVerdict",
    "RepeatRunner",
    "RepeatVerdict",
    "RunBudget",
    "VerdictKind",
    "get_default_oracle",
    "get_default_runner",
    "oracle_enabled",
    "register_evaluator",
    "repeat_runner_enabled",
]
