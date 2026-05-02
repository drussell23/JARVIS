"""PlanFalsificationDetector Slice 1 — pure-stdlib decision primitive.

The structural alternative to ``DynamicRePlanner._STRATEGY_MAP`` (the
hardcoded regex-pattern → strategy table the user explicitly forbids).

Where ``DynamicRePlanner`` reactively pattern-matches an error class
against a hardcoded dict, this primitive lets evidence streams
**structurally falsify** a plan-step's expected_outcome predicate.
The replan trigger fires on STRUCTURAL contradiction (a hypothesis
the model committed to is invalidated by observable evidence), not
on regex-matching opaque error messages.

Architectural reuse pattern (no duplication)
--------------------------------------------

* :class:`PlanStepHypothesis` is a thin plan-aware adapter over the
  same ``expected_outcome`` shape ``HypothesisLedger.Hypothesis``
  uses (`hypothesis_ledger.py:57`). Slice 3 will pair every
  ``PlanResult.ordered_changes`` entry with a ``PlanStepHypothesis``
  by extending PlanGenerator's prompt schema. The ``hypothesis_id``
  field optionally cross-references a HypothesisLedger entry so the
  same falsifiable claim flows through both surfaces.
* :class:`EvidenceItem` is the closed-vocab evidence wrapper. It
  intentionally does NOT import VERIFY result types or AdversarialReview
  finding types — the source classifies its evidence with one of the
  5 :class:`FalsificationKind` values; the detector consumes the
  classification, not the raw payload. This keeps the primitive
  pure-stdlib (no governance imports) while preserving structural
  fidelity.
* :class:`FalsificationVerdict` mirrors the J.A.R.M.A.T.R.I.X.
  closed-taxonomy verdict shape used by every prior Slice 1 primitive
  (Move 5 ProbeOutcome / Move 6 ConsensusOutcome /
  InlinePromptGate PhaseInlineVerdict / SBT-Probe Escalation
  EscalationDecision / Lifecycle Hook AggregateHookDecision).
* Phase C ``MonotonicTighteningVerdict.PASSED`` stamping is
  outcome-aware: REPLAN_TRIGGERED is structural tightening
  (operator-evidence-driven re-plan adds analysis friction);
  NO_FALSIFICATION / INSUFFICIENT_EVIDENCE / DISABLED / FAILED are
  no-op fall-through paths.

Direct-solve principles
-----------------------

* **Asynchronous-ready** — frozen dataclasses propagate cleanly
  through asyncio boundaries; Slice 2 detector wraps the decision
  function in async I/O for filesystem/AST checks.
* **Dynamic** — every numeric (min_evidence_count,
  falsification_max_age_s) clamped floor + ceiling via env helpers.
  NO hardcoded magic constants.
* **Adaptive** — degraded inputs (None / non-tuple / non-dataclass
  elements) all map to closed-taxonomy values rather than raises.
* **Intelligent** — falsification verdict is total + deterministic:
  first matching evidence (lowest step_index, then earliest
  captured_ts) wins. Multi-step contradictions surface the
  earliest-fired falsification + reference the rest in detail
  for operator audit.
* **Robust** — every public function NEVER raises out. Pure-data
  primitive callable from any context.
* **No hardcoding** — 5-value closed taxonomies; per-knob env
  helpers; byte-parity to HypothesisLedger schema verified by
  test (Slice 4 graduation pin).

Authority invariants (AST-pinned by Slice 4 graduation)
-------------------------------------------------------

* Imports stdlib ONLY at hot path. NEVER imports any governance
  module — strongest authority invariant. Module-owned
  ``register_flags`` / ``register_shipped_invariants`` exempt
  (Priority #6 registration-contract exemption).
* No async (Slice 2 detector wraps via asyncio).
* No exec/eval/compile.
* The primitive does NOT decide WHAT counts as falsification
  evidence — the source classifies via :class:`FalsificationKind`.
  This keeps the structural contract: evidence is typed at the
  source (VERIFY phase / exploration / adversarial review / file
  probe / repair iteration); the detector aggregates.

Master flag default-FALSE until Slice 4 graduation:
``JARVIS_PLAN_FALSIFICATION_ENABLED``. Asymmetric env semantics —
empty/whitespace = unset = current default; explicit truthy/falsy
overrides at call time.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


PLAN_FALSIFICATION_SCHEMA_VERSION: str = "plan_falsification.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


def plan_falsification_enabled() -> bool:
    """``JARVIS_PLAN_FALSIFICATION_ENABLED`` (default ``false`` until
    Slice 4 graduation).

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit ``0``/``false``/``no``/``off`` evaluates false;
    explicit truthy values evaluate true. Re-read on every call so
    flips hot-revert without restart.

    The default stays off through Slices 1-3 because graduating
    before the orchestrator wire-up is live (Slice 4) would register
    the detector but never invoke it — operator-confusing. Slice 4
    flips the default after the full stack proves out with combined
    sweep + e2e test demonstrating a synthetic plan-step contradiction
    triggers a re-plan with contradicting evidence threaded in.
    """
    raw = os.environ.get(
        "JARVIS_PLAN_FALSIFICATION_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # pre-graduation default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers — every numeric clamped (floor + ceiling)
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def min_evidence_count() -> int:
    """``JARVIS_PLAN_FALSIFICATION_MIN_EVIDENCE`` — minimum number
    of evidence items required to fire a verdict. Floor 1, ceiling
    16, default 1. Operators may raise this to suppress
    single-data-point replans (require corroboration from ≥N
    independent evidence streams)."""
    return _env_int_clamped(
        "JARVIS_PLAN_FALSIFICATION_MIN_EVIDENCE",
        default=1, floor=1, ceiling=16,
    )


def falsification_max_age_s() -> float:
    """``JARVIS_PLAN_FALSIFICATION_MAX_AGE_S`` — evidence older
    than this (relative to monotonic clock at decision time) is
    ignored. Floor 1.0s, ceiling 3600s, default 300s. Stale
    evidence from a prior op should not falsify a re-planned
    successor."""
    return _env_float_clamped(
        "JARVIS_PLAN_FALSIFICATION_MAX_AGE_S",
        default=300.0, floor=1.0, ceiling=3600.0,
    )


# ---------------------------------------------------------------------------
# Closed taxonomy — 5-value FalsificationKind (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class FalsificationKind(str, enum.Enum):
    """Closed 5-value taxonomy of how an :class:`EvidenceItem`
    contradicts a :class:`PlanStepHypothesis`. The SOURCE classifies
    its evidence with one of these values at capture time; the
    detector routes deterministically.

    Distinct from regex-pattern error matching: each value names a
    STRUCTURAL signal, not a string pattern. Operators add new
    falsification kinds by extending this enum + the source-side
    classification — never by adding regex strings to a pattern
    table.

    * :attr:`FILE_MISSING` — a plan step references a file that
      doesn't exist on disk at the moment of evidence capture.
      Source: deterministic filesystem probe (Slice 2 detector).
    * :attr:`SYMBOL_MISSING` — a plan step references a function /
      class / symbol that doesn't exist in the target file at the
      moment of capture. Source: AST or grep probe.
    * :attr:`VERIFY_REJECTED` — the VERIFY phase produced a
      rejection signal (test failed, contract violated). Source:
      VERIFY runner.
    * :attr:`REPAIR_STUCK` — L2 repair iterated past its bound
      without convergence, suggesting the plan's approach is
      structurally wrong-shape. Source: L2 RepairEngine.
    * :attr:`EVIDENCE_CONTRADICTED` — an explicit
      ``contradicts_plan=True`` flag on an evidence item from a
      higher-fidelity source (AdversarialReview, exploration
      finding, operator annotation). Source: that surface.
    """

    FILE_MISSING = "file_missing"
    SYMBOL_MISSING = "symbol_missing"
    VERIFY_REJECTED = "verify_rejected"
    REPAIR_STUCK = "repair_stuck"
    EVIDENCE_CONTRADICTED = "evidence_contradicted"


_VALID_FALSIFICATION_KINDS: FrozenSet[str] = frozenset(
    {k.value for k in FalsificationKind},
)


# ---------------------------------------------------------------------------
# Closed taxonomy — 5-value FalsificationOutcome (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class FalsificationOutcome(str, enum.Enum):
    """Closed 5-value taxonomy of detector verdicts. Every
    (hypotheses × evidence × flag) input combination maps to
    exactly one outcome.

    * :attr:`REPLAN_TRIGGERED` — at least one evidence item
      structurally falsified at least one plan-step hypothesis.
      Slice 4 wire-up routes this to PlanGenerator re-invocation
      with the contradicting evidence threaded into the new plan
      prompt.
    * :attr:`NO_FALSIFICATION` — evidence and hypotheses present
      but no structural contradictions detected. Plan proceeds
      unchanged.
    * :attr:`INSUFFICIENT_EVIDENCE` — fewer than
      :func:`min_evidence_count` evidence items, OR no
      hypotheses to check against. Distinct from
      NO_FALSIFICATION so observability tells "we didn't find
      contradictions" from "we couldn't decide".
    * :attr:`DISABLED` — master flag off OR garbage input.
      Equivalent to NO_FALSIFICATION for orchestrator purposes
      (proceed unchanged), but distinct in audit.
    * :attr:`FAILED` — defensive sentinel. Last-resort exception
      handler. Falls through to existing ``DynamicRePlanner``
      reactive path — broken detector cannot suppress the legacy
      replan trigger.
    """

    REPLAN_TRIGGERED = "replan_triggered"
    NO_FALSIFICATION = "no_falsification"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Phase C MonotonicTighteningVerdict canonical string
# ---------------------------------------------------------------------------

#: Canonical string from ``adaptation.ledger.MonotonicTighteningVerdict``.
#: Slice 4 graduation pin asserts byte-parity to the live enum.
#: Stamped on REPLAN_TRIGGERED outcomes (operator-evidence-driven
#: re-plan adds analysis friction — structural tightening). Other
#: outcomes stamp empty (advisory or no-op).
_TIGHTENING_PASSED_STR: str = "passed"

_TIGHTENING_OUTCOMES: FrozenSet[FalsificationOutcome] = frozenset({
    FalsificationOutcome.REPLAN_TRIGGERED,
})


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStepHypothesis:
    """Thin plan-aware adapter over the same falsifiable-predicate
    shape :class:`hypothesis_ledger.Hypothesis` uses. Pairs a
    PlanResult.ordered_changes entry with a model-emitted
    expected_outcome predicate.

    Slice 3 PlanGenerator extension will populate this from each
    ordered_change at plan-emit time. The ``hypothesis_id`` field
    optionally cross-references a HypothesisLedger entry so the
    same falsifiable claim flows through both surfaces (plan path
    + self-formed-goal path).

    Fields:
      step_index: 0-based position in PlanResult.ordered_changes.
      file_path: target file the step modifies/creates.
      change_type: "create" / "modify" / "delete" / "rename"
        (free-form, propagated from ordered_change).
      expected_outcome: falsifiable predicate the model commits to
        ("auth.py exists and contains login() function returning
        bool"). Free-form text — the detector MATCHES against
        kind-tagged evidence; it does NOT parse the predicate.
      hypothesis_id: optional cross-ref to HypothesisLedger entry.
        Empty string when not paired.
    """

    step_index: int
    file_path: str
    change_type: str = ""
    expected_outcome: str = ""
    hypothesis_id: str = ""
    schema_version: str = PLAN_FALSIFICATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_index": self.step_index,
            "file_path": self.file_path,
            "change_type": self.change_type,
            "expected_outcome": self.expected_outcome,
            "hypothesis_id": self.hypothesis_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, d: Mapping[str, Any],
    ) -> "PlanStepHypothesis":
        try:
            return cls(
                step_index=int(d.get("step_index", 0) or 0),
                file_path=str(d.get("file_path", "")),
                change_type=str(d.get("change_type", "")),
                expected_outcome=str(d.get("expected_outcome", "")),
                hypothesis_id=str(d.get("hypothesis_id", "")),
                schema_version=str(
                    d.get("schema_version", PLAN_FALSIFICATION_SCHEMA_VERSION),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[PlanFalsification] PlanStepHypothesis from_dict "
                "degraded: %s", exc,
            )
            return cls(step_index=0, file_path="")


@dataclass(frozen=True)
class EvidenceItem:
    """One typed evidence item from an external source. The SOURCE
    classifies its evidence with one of the :class:`FalsificationKind`
    values; the detector routes deterministically — does NOT pattern-
    match payload contents.

    ``target_step_index`` is the optional precise plan-step pointer
    when the source knows which step the evidence concerns (e.g.,
    L2 RepairEngine reports REPAIR_STUCK on the specific step it
    was repairing). When absent, the detector falls back to
    matching by ``target_file_path``.

    ``captured_monotonic`` is a monotonic-clock timestamp at
    capture time; the detector uses this for the
    :func:`falsification_max_age_s` staleness check (NEVER wall-
    clock — that would let a clock skew falsify legitimate
    evidence)."""

    kind: FalsificationKind
    target_step_index: Optional[int] = None
    target_file_path: str = ""
    detail: str = ""
    source: str = ""  # "verify_runner" / "repair_engine" / "ar_review" / etc.
    captured_monotonic: float = 0.0
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PLAN_FALSIFICATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "target_step_index": self.target_step_index,
            "target_file_path": self.target_file_path,
            "detail": self.detail,
            "source": self.source,
            "captured_monotonic": self.captured_monotonic,
            "payload": dict(self.payload),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class FalsificationVerdict:
    """Total verdict from :func:`compute_falsification_verdict`.
    Frozen for safe propagation through Slice 2 async wrapper +
    Slice 4 orchestrator wire-up.

    On REPLAN_TRIGGERED:
      * ``falsified_step_index`` points to the (lowest-indexed,
        earliest-captured) hypothesis whose claim was contradicted
      * ``falsifying_evidence_kinds`` lists every evidence kind
        that contradicted some hypothesis (operator audit)
      * ``contradicting_detail`` is the source's free-form detail
        from the WINNING evidence item — threaded into the re-plan
        prompt by Slice 4

    Phase C tightening stamping is by-construction: REPLAN_TRIGGERED
    stamps ``"passed"`` (operator-evidence-driven re-plan is
    structural tightening); all other outcomes stamp empty.
    """

    outcome: FalsificationOutcome
    falsified_step_index: Optional[int] = None
    falsifying_evidence_kinds: Tuple[str, ...] = ()
    contradicting_detail: str = ""
    total_hypotheses: int = 0
    total_evidence: int = 0
    monotonic_tightening_verdict: str = ""
    schema_version: str = PLAN_FALSIFICATION_SCHEMA_VERSION

    @property
    def is_replan_triggered(self) -> bool:
        return self.outcome is FalsificationOutcome.REPLAN_TRIGGERED

    @property
    def is_tightening(self) -> bool:
        return self.outcome in _TIGHTENING_OUTCOMES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "falsified_step_index": self.falsified_step_index,
            "falsifying_evidence_kinds": list(self.falsifying_evidence_kinds),
            "contradicting_detail": self.contradicting_detail,
            "total_hypotheses": self.total_hypotheses,
            "total_evidence": self.total_evidence,
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Total decision function
# ---------------------------------------------------------------------------


def _evidence_matches_hypothesis(
    evidence: EvidenceItem,
    hypothesis: PlanStepHypothesis,
) -> bool:
    """Match an evidence item to a plan-step hypothesis. Exact
    matching by step_index when present; falls back to file_path
    equality (case-insensitive). NEVER raises."""
    try:
        if (
            evidence.target_step_index is not None
            and evidence.target_step_index == hypothesis.step_index
        ):
            return True
        ef = (evidence.target_file_path or "").strip().lower()
        hf = (hypothesis.file_path or "").strip().lower()
        if ef and hf and ef == hf:
            return True
        return False
    except Exception:  # noqa: BLE001 — defensive
        return False


def compute_falsification_verdict(
    plan_hypotheses: Tuple[PlanStepHypothesis, ...],
    evidence_items: Tuple[EvidenceItem, ...],
    *,
    enabled: Optional[bool] = None,
    decision_monotonic: Optional[float] = None,
) -> FalsificationVerdict:
    """Total mapping function — every (hypotheses × evidence ×
    flag) combination maps to exactly one
    :class:`FalsificationVerdict`. NEVER raises.

    Decision tree (deterministic, no heuristics, no regex):
      1. ``enabled=False`` (or master flag off when enabled=None)
         → DISABLED.
      2. Garbage inputs (non-tuple, non-dataclass elements) defensively
         coerced — non-conforming entries silently dropped.
      3. After coercion: 0 hypotheses → INSUFFICIENT_EVIDENCE.
      4. After staleness filter (max_age_s): fewer than
         :func:`min_evidence_count` evidence items remain →
         INSUFFICIENT_EVIDENCE.
      5. For each evidence item (in stable order: by
         captured_monotonic ascending, then by index): scan
         hypotheses (by step_index ascending) for a match
         (target_step_index OR target_file_path). On first match,
         capture the (step_index, evidence) pair as the WINNING
         falsification.
      6. If at least one match found → REPLAN_TRIGGERED with the
         winner's step_index + every distinct evidence kind that
         matched (operator audit).
      7. No matches → NO_FALSIFICATION.

    Phase C tightening stamping:
      REPLAN_TRIGGERED → ``"passed"``;
      all other outcomes → empty string.
    """
    try:
        # 1. Master flag short-circuit.
        is_enabled = (
            enabled if enabled is not None
            else plan_falsification_enabled()
        )
        if not is_enabled:
            return FalsificationVerdict(
                outcome=FalsificationOutcome.DISABLED,
                monotonic_tightening_verdict="",
            )

        # 2. Defensive coercion.
        if not isinstance(plan_hypotheses, tuple):
            try:
                plan_hypotheses = tuple(plan_hypotheses or ())
            except Exception:  # noqa: BLE001
                plan_hypotheses = ()
        if not isinstance(evidence_items, tuple):
            try:
                evidence_items = tuple(evidence_items or ())
            except Exception:  # noqa: BLE001
                evidence_items = ()

        valid_hyps: Tuple[PlanStepHypothesis, ...] = tuple(
            h for h in plan_hypotheses
            if isinstance(h, PlanStepHypothesis)
        )
        valid_evidence: Tuple[EvidenceItem, ...] = tuple(
            e for e in evidence_items
            if isinstance(e, EvidenceItem)
            and isinstance(e.kind, FalsificationKind)
        )

        # 3. Empty hypotheses.
        if not valid_hyps:
            return FalsificationVerdict(
                outcome=FalsificationOutcome.INSUFFICIENT_EVIDENCE,
                total_hypotheses=0,
                total_evidence=len(valid_evidence),
                monotonic_tightening_verdict="",
            )

        # 4. Staleness filter.
        try:
            now_mono = (
                float(decision_monotonic)
                if decision_monotonic is not None
                else None
            )
        except (TypeError, ValueError):
            now_mono = None
        if now_mono is not None:
            try:
                max_age = float(falsification_max_age_s())
            except Exception:  # noqa: BLE001
                max_age = 300.0
            valid_evidence = tuple(
                e for e in valid_evidence
                if (now_mono - float(e.captured_monotonic or 0.0))
                <= max_age
            )

        try:
            min_n = int(min_evidence_count())
        except Exception:  # noqa: BLE001
            min_n = 1
        if len(valid_evidence) < max(1, min_n):
            return FalsificationVerdict(
                outcome=FalsificationOutcome.INSUFFICIENT_EVIDENCE,
                total_hypotheses=len(valid_hyps),
                total_evidence=len(valid_evidence),
                monotonic_tightening_verdict="",
            )

        # 5. Stable-ordered scan.
        sorted_hyps = sorted(
            valid_hyps, key=lambda h: int(h.step_index or 0),
        )
        sorted_evidence = sorted(
            enumerate(valid_evidence),
            key=lambda pair: (
                float(pair[1].captured_monotonic or 0.0),
                pair[0],
            ),
        )

        winner_step: Optional[int] = None
        winner_detail: str = ""
        kinds_matched: list = []
        for _idx, ev in sorted_evidence:
            for hyp in sorted_hyps:
                if _evidence_matches_hypothesis(ev, hyp):
                    if ev.kind.value not in kinds_matched:
                        kinds_matched.append(ev.kind.value)
                    if winner_step is None:
                        winner_step = int(hyp.step_index)
                        winner_detail = str(ev.detail or "")[:500]
                    break  # one hypothesis per evidence item

        # 6. Match found → REPLAN_TRIGGERED.
        if winner_step is not None:
            return FalsificationVerdict(
                outcome=FalsificationOutcome.REPLAN_TRIGGERED,
                falsified_step_index=winner_step,
                falsifying_evidence_kinds=tuple(kinds_matched),
                contradicting_detail=winner_detail,
                total_hypotheses=len(valid_hyps),
                total_evidence=len(valid_evidence),
                monotonic_tightening_verdict=_TIGHTENING_PASSED_STR,
            )

        # 7. No matches → NO_FALSIFICATION.
        return FalsificationVerdict(
            outcome=FalsificationOutcome.NO_FALSIFICATION,
            total_hypotheses=len(valid_hyps),
            total_evidence=len(valid_evidence),
            monotonic_tightening_verdict="",
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.warning(
            "[PlanFalsification] compute_falsification_verdict "
            "last-resort degraded: %s", exc,
        )
        return FalsificationVerdict(
            outcome=FalsificationOutcome.FAILED,
            monotonic_tightening_verdict="",
        )


# ---------------------------------------------------------------------------
# Convenience constructor — pair PlanResult.ordered_change → hypothesis
# ---------------------------------------------------------------------------


def pair_plan_step_with_hypothesis(
    *,
    step_index: int,
    ordered_change: Mapping[str, Any],
    expected_outcome: str = "",
    hypothesis_id: str = "",
) -> PlanStepHypothesis:
    """Build a :class:`PlanStepHypothesis` from a PlanResult.
    ordered_changes entry. Slice 3 PlanGenerator extension calls
    this once per step at plan-emit time, after the model has
    emitted the per-step ``expected_outcome`` predicate.

    NEVER raises. Garbage ordered_change → PlanStepHypothesis with
    empty fields (the detector will silently skip a hypothesis with
    empty file_path AND no step_index reference)."""
    try:
        return PlanStepHypothesis(
            step_index=int(step_index or 0),
            file_path=str(
                (ordered_change or {}).get("file_path", ""),
            ),
            change_type=str(
                (ordered_change or {}).get("change_type", ""),
            ),
            expected_outcome=str(expected_outcome or "")[:1000],
            hypothesis_id=str(hypothesis_id or "")[:128],
        )
    except Exception:  # noqa: BLE001 — defensive
        return PlanStepHypothesis(step_index=0, file_path="")


# ---------------------------------------------------------------------------
# Public surface — Slice 4 will pin via shipped_code_invariants
# ---------------------------------------------------------------------------

__all__ = [
    "EvidenceItem",
    "FalsificationKind",
    "FalsificationOutcome",
    "FalsificationVerdict",
    "PLAN_FALSIFICATION_SCHEMA_VERSION",
    "PlanStepHypothesis",
    "compute_falsification_verdict",
    "falsification_max_age_s",
    "min_evidence_count",
    "pair_plan_step_with_hypothesis",
    "plan_falsification_enabled",
]
