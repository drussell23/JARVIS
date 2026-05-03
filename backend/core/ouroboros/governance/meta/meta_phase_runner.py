"""RR Pass B Slice 5 — MetaPhaseRunner: composes Slices 1+3+4 evidence.

Per ``memory/project_reverse_russian_doll_pass_b.md`` §7.1, the
``MetaPhaseRunner`` is the single component O+V uses to propose a
new ``PhaseRunner`` subclass. It composes the upstream Pass B
slices into one evidence-bundle producer:

  1. Receives an op whose target file matches
     ``phase_runners/*.py`` (Slice 1 manifest hit).
  2. Routes through the standard 11-phase pipeline using existing
     PhaseRunners — the FSM cognizes about its own extension.
  3. At GATE: the Slice 2 classifier flips
     ``risk_tier = ORDER_2_GOVERNANCE``.
  4. Pre-APPLY: the Slice 3 AST validator runs.
  5. Pre-APPLY: the Slice 4 shadow corpus is identified for replay
     against the candidate runner (the corpus snapshots become
     part of the evidence bundle; **actual replay execution is a
     follow-up under operator-explicit trigger**, see "deferred
     scope" below).
  6. If structural gates pass, the candidate does **not**
     auto-apply — it enters the ``order2_review`` queue with the
     full evidence bundle.
  7. Operator approves via the Slice 6 amendment protocol →
     APPLY runs → AutoCommitter commits with
     ``Order-2-Authorized-By: <operator>`` trailer.

Per Pass B §7.2 (what it doesn't do):
  * Does not generate runners autonomously. O+V proposes; operator
    authorizes.
  * Does not bypass any existing gate. Iron Gate, SemanticGuardian,
    SemanticFirewall, exploration ledger — all run as normal.
    ORDER_2_GOVERNANCE is *additive*.

## Deferred scope: candidate code execution

The cage's whole point is that arbitrary candidate Python is NOT
executed without operator authorization. Slice 5 reasons about the
candidate **structurally** (via Slice 3's pure ``ast.parse`` walk)
and identifies **which corpus snapshots would be replayed**, but
NEVER compiles or instantiates the candidate. The actual
substitute-and-replay step is a follow-up slice that runs under
explicit operator trigger via Slice 6's amendment-protocol REPL.

## Authority invariants (Pass B §7.2)

  * Pure composition + AST inspection. No subprocess, no env
    mutation, no network. Manifest + corpus reads are delegated to
    Slice 1 + Slice 4 (their I/O surfaces).
  * No imports of orchestrator / policy / iron_gate /
    risk_tier_floor / change_engine / candidate_generator / gate /
    semantic_guardian / semantic_firewall / scoped_tool_backend.
  * Allowed: stdlib + ``meta.order2_manifest`` +
    ``meta.order2_classifier`` + ``meta.ast_phase_runner_validator``
    + ``meta.shadow_replay``.
  * Best-effort throughout — every failure is mapped to a
    structured :class:`MetaEvaluationStatus`; never raises.

## Default-off

Behind ``JARVIS_META_PHASE_RUNNER_ENABLED`` until Slice 5's own
clean-session graduation. When off,
:meth:`MetaPhaseRunner.evaluate_candidate` short-circuits to
``MetaEvaluationStatus.DISABLED``. Slice 6's amendment-protocol
hook treats DISABLED as "no Order-2 evaluation" — the cage
degrades to the existing review path.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

from backend.core.ouroboros.governance.meta.ast_phase_runner_validator import (
    ValidationResult,
    ValidationStatus,
    validate_ast,
)
from backend.core.ouroboros.governance.meta.order2_classifier import (
    classify_order2_match,
)
from backend.core.ouroboros.governance.meta.order2_manifest import (
    Order2Manifest,
    get_default_manifest,
)
from backend.core.ouroboros.governance.meta.shadow_replay import (
    ReplayCorpus,
    ReplayLoadStatus,
    ReplaySnapshot,
    get_default_corpus,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Schema version stamped into MetaEvaluation.to_dict so Slice 6
# REPL parsers can pin a version.
META_EVALUATION_SCHEMA_VERSION: int = 1

# Soft cap on the size of the rationale string a caller can attach
# to a candidate proposal. Defends against runaway prose
# overwhelming the order2_review queue render.
MAX_RATIONALE_CHARS: int = 2_048


def is_enabled() -> bool:
    """Master flag — ``JARVIS_META_PHASE_RUNNER_ENABLED`` (default
    false until Slice 5 graduation).

    When off, :meth:`MetaPhaseRunner.evaluate_candidate` short-
    circuits to ``MetaEvaluationStatus.DISABLED``. Slice 6 hook
    treats DISABLED as "no Order-2 evaluation" — the cage degrades
    to the existing review path."""
    return os.environ.get(
        "JARVIS_META_PHASE_RUNNER_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Status enum + frozen evaluation result
# ---------------------------------------------------------------------------


class MetaEvaluationStatus(str, enum.Enum):
    """Outcome of one MetaPhaseRunner evaluation pass.

    Slice 6's order2_review queue renders this verbatim per finding
    so operators can scan a full review with one column.
    """

    READY_FOR_OPERATOR_REVIEW = "READY_FOR_OPERATOR_REVIEW"
    """All structural gates passed (manifest matched + AST valid +
    snapshots available). Candidate is ready for the operator
    amendment protocol — pending shadow-replay execution + operator
    sign-off."""

    DISABLED = "DISABLED"
    """JARVIS_META_PHASE_RUNNER_ENABLED is off — short-circuit."""

    NOT_ORDER_2 = "NOT_ORDER_2"
    """target_files don't match the Slice 1 manifest. The candidate
    isn't proposing an Order-2 amendment; falls through to the
    standard FSM path."""

    AST_VALIDATION_FAILED = "AST_VALIDATION_FAILED"
    """Slice 3 AST validator rejected the candidate. Failure
    detail is in the embedded :class:`ValidationResult`."""

    NO_APPLICABLE_SNAPSHOTS = "NO_APPLICABLE_SNAPSHOTS"
    """Slice 4 corpus has zero snapshots for the target phase, so
    shadow replay can't produce a regression verdict. Treated as
    a hard fail — operators MUST seed a corpus snapshot for the
    target phase before the cage can clear a candidate runner for
    that phase."""

    CORPUS_UNAVAILABLE = "CORPUS_UNAVAILABLE"
    """Slice 4 corpus failed to load (status != LOADED). Could be
    master-flag off, missing dir, parse error, etc. Cage degrades
    to the existing review path; candidate stays in the queue
    pending operator manual decision."""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Defensive: an unexpected exception escaped the composition.
    Should never fire in practice (each composed primitive is
    best-effort by Slice 1-4 contract); included so the evaluator
    never raises."""


@dataclass(frozen=True)
class MetaEvaluation:
    """Full evidence bundle for one candidate-runner proposal.

    Slice 6's order2_review queue persists this verbatim + renders
    each field for the operator's sign-off decision. The bundle is
    structural-only — Slice 5 produces it WITHOUT executing the
    candidate.
    """

    schema_version: int
    op_id: str
    target_phase: str
    target_files: Tuple[str, ...]
    rationale: str
    status: MetaEvaluationStatus
    manifest_matched: bool = False
    ast_validation: Optional[ValidationResult] = None
    applicable_snapshots: Tuple[ReplaySnapshot, ...] = field(
        default_factory=tuple,
    )
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready_for_review(self) -> bool:
        return self.status is MetaEvaluationStatus.READY_FOR_OPERATOR_REVIEW

    def to_dict(self) -> dict:
        """Stable serialization for Slice 6 queue persistence."""
        return {
            "schema_version": self.schema_version,
            "op_id": self.op_id,
            "target_phase": self.target_phase,
            "target_files": list(self.target_files),
            "rationale": self.rationale,
            "status": self.status.value,
            "manifest_matched": self.manifest_matched,
            "ast_validation": (
                {
                    "status": self.ast_validation.status.value,
                    "reason": (
                        self.ast_validation.reason.value
                        if self.ast_validation.reason else None
                    ),
                    "detail": self.ast_validation.detail,
                    "classes_inspected": list(
                        self.ast_validation.classes_inspected,
                    ),
                }
                if self.ast_validation is not None else None
            ),
            "applicable_snapshots": [
                {"op_id": s.op_id, "phase": s.phase,
                 "tags": list(s.tags)}
                for s in self.applicable_snapshots
            ],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class MetaPhaseRunner:
    """Composes Slice 1+2+3+4 evidence into one
    :class:`MetaEvaluation`. Stateless — every
    :meth:`evaluate_candidate` call is independent. Caller injects
    manifest + corpus or accepts the safe defaults (process-wide
    singletons from Slice 1 + 4).

    Authority constraint: this class NEVER executes the candidate.
    It produces the evidence bundle the operator needs to authorize
    the amendment via Slice 6's protocol; actual candidate exec is a
    follow-up under operator-explicit trigger.
    """

    def __init__(
        self,
        manifest: Optional[Order2Manifest] = None,
        corpus: Optional[ReplayCorpus] = None,
    ) -> None:
        self._manifest = manifest
        self._corpus = corpus

    def _man(self) -> Order2Manifest:
        return (
            self._manifest if self._manifest is not None
            else get_default_manifest()
        )

    def _cor(self) -> ReplayCorpus:
        return (
            self._corpus if self._corpus is not None
            else get_default_corpus()
        )

    def evaluate_candidate(
        self,
        *,
        op_id: str,
        target_phase: str,
        target_files: Sequence[str],
        candidate_source: str,
        rationale: str = "",
        repo: str = "jarvis",
    ) -> MetaEvaluation:
        """Evaluate a proposed PhaseRunner subclass; produce evidence
        bundle. NEVER raises; never executes the candidate.

        Slice 6's order2_review queue persists the returned
        :class:`MetaEvaluation` for operator sign-off. The
        operator's authorization is what actually triggers
        execution + APPLY in a future slice."""
        rationale_clipped = (rationale or "")[:MAX_RATIONALE_CHARS]
        target_files_t = tuple(t for t in (target_files or ()) if t)

        # 0. Master-flag short-circuit.
        if not is_enabled():
            return MetaEvaluation(
                schema_version=META_EVALUATION_SCHEMA_VERSION,
                op_id=op_id, target_phase=target_phase,
                target_files=target_files_t,
                rationale=rationale_clipped,
                status=MetaEvaluationStatus.DISABLED,
                notes=("master_flag_off",),
            )

        try:
            # 1. Manifest classification (Slice 1 + 2).
            manifest = self._man()
            matched = classify_order2_match(
                target_files_t, repo=repo, manifest=manifest,
            )
            if not matched:
                return MetaEvaluation(
                    schema_version=META_EVALUATION_SCHEMA_VERSION,
                    op_id=op_id, target_phase=target_phase,
                    target_files=target_files_t,
                    rationale=rationale_clipped,
                    status=MetaEvaluationStatus.NOT_ORDER_2,
                    manifest_matched=False,
                    notes=("manifest_miss",),
                )

            # 2. AST validation (Slice 3).
            ast_result = validate_ast(candidate_source)
            if ast_result.status is ValidationStatus.FAILED:
                logger.info(
                    "[MetaPhaseRunner] op=%s AST validation FAILED "
                    "reason=%s detail=%r",
                    op_id,
                    (ast_result.reason.value
                     if ast_result.reason else "?"),
                    ast_result.detail,
                )
                return MetaEvaluation(
                    schema_version=META_EVALUATION_SCHEMA_VERSION,
                    op_id=op_id, target_phase=target_phase,
                    target_files=target_files_t,
                    rationale=rationale_clipped,
                    status=MetaEvaluationStatus.AST_VALIDATION_FAILED,
                    manifest_matched=True,
                    ast_validation=ast_result,
                    notes=(f"ast_status:{ast_result.status.value}",),
                )

            # 3. Corpus availability (Slice 4).
            corpus = self._cor()
            if corpus.status is not ReplayLoadStatus.LOADED:
                return MetaEvaluation(
                    schema_version=META_EVALUATION_SCHEMA_VERSION,
                    op_id=op_id, target_phase=target_phase,
                    target_files=target_files_t,
                    rationale=rationale_clipped,
                    status=MetaEvaluationStatus.CORPUS_UNAVAILABLE,
                    manifest_matched=True,
                    ast_validation=ast_result,
                    notes=(f"corpus_status:{corpus.status.value}",),
                )

            applicable = corpus.for_phase(target_phase)
            if not applicable:
                return MetaEvaluation(
                    schema_version=META_EVALUATION_SCHEMA_VERSION,
                    op_id=op_id, target_phase=target_phase,
                    target_files=target_files_t,
                    rationale=rationale_clipped,
                    status=MetaEvaluationStatus.NO_APPLICABLE_SNAPSHOTS,
                    manifest_matched=True,
                    ast_validation=ast_result,
                    notes=(
                        f"corpus_snapshots_for_phase:{target_phase}=0",
                    ),
                )

            # 4. All structural gates passed — ready for operator review.
            logger.info(
                "[MetaPhaseRunner] op=%s READY_FOR_OPERATOR_REVIEW "
                "phase=%s applicable_snapshots=%d ast_classes=%s",
                op_id, target_phase, len(applicable),
                ast_result.classes_inspected,
            )
            return MetaEvaluation(
                schema_version=META_EVALUATION_SCHEMA_VERSION,
                op_id=op_id, target_phase=target_phase,
                target_files=target_files_t,
                rationale=rationale_clipped,
                status=MetaEvaluationStatus.READY_FOR_OPERATOR_REVIEW,
                manifest_matched=True,
                ast_validation=ast_result,
                applicable_snapshots=applicable,
                notes=(),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[MetaPhaseRunner] op=%s INTERNAL_ERROR (composition "
                "should be best-effort): %s",
                op_id, exc,
            )
            return MetaEvaluation(
                schema_version=META_EVALUATION_SCHEMA_VERSION,
                op_id=op_id, target_phase=target_phase,
                target_files=target_files_t,
                rationale=rationale_clipped,
                status=MetaEvaluationStatus.INTERNAL_ERROR,
                notes=(f"exception:{type(exc).__name__}:{exc!s}",),
            )


__all__ = [
    "MAX_RATIONALE_CHARS",
    "META_EVALUATION_SCHEMA_VERSION",
    "MetaEvaluation",
    "MetaEvaluationStatus",
    "MetaPhaseRunner",
    "is_enabled",
]


# ---------------------------------------------------------------------------
# Pass B Graduation Slice 2 — substrate AST pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta._invariant_helpers import (
        make_pass_b_substrate_invariant,
    )
    inv = make_pass_b_substrate_invariant(
        invariant_name="pass_b_meta_phase_runner_substrate",
        target_file=(
            "backend/core/ouroboros/governance/meta/meta_phase_runner.py"
        ),
        description=(
            "Pass B Slice 5 substrate: is_enabled + MetaPhaseRunner "
            "+ MetaEvaluation (frozen) present; no dynamic-code "
            "calls. Note: master flag stays default-FALSE pre-soak "
            "graduation per W2(5) policy."
        ),
        required_funcs=("is_enabled",),
        required_classes=("MetaPhaseRunner", "MetaEvaluation"),
        frozen_classes=("MetaEvaluation",),
    )
    return [inv] if inv is not None else []
