"""Q4 Priority #2 Slice 1 — ClosureLoopOrchestrator primitive.

Composes the four already-shipped pieces of the RSI closure loop:

    Coherence Auditor (BEHAVIORAL_ROUTE_DRIFT etc.)
              │
              ▼  CoherenceAdvisory
    Confidence Threshold Tightener  (cage-validated proposal)
              │
              ▼  validator(ok, detail)
    Counterfactual Replay  (empirical validation against past sessions)
              │
              ▼  ReplayVerdict
    AdaptationLedger.propose  ← Slice 3 wires this end
              │
              ▼  PENDING (operator-approval gated)
    Operator approves via /adapt REPL OR VSCode confidencePolicyPanel
              │
              ▼
    yaml_writer.write          ← OPERATOR-AUTHORIZED ONLY

Today an operator must manually compose advisory → tightener → replay →
``/adapt propose``. Slice 1 ships the **pure-stdlib decision primitive**
that takes the three intermediate artifacts and emits a single
:class:`ClosureLoopRecord` with one of six closed-taxonomy outcomes.

Slice 2 wraps this in an async observer + cross-process flock'd ring
buffer. Slice 3 wires the actual chain. Slice 4 graduates.

Authority invariant (AST-pinned in Slice 4):
  This module imports NOTHING from ``yaml_writer``, ``meta_governor``,
  ``orchestrator``, ``policy``, ``iron_gate``, ``risk_tier``,
  ``change_engine``, ``candidate_generator``, or ``gate``. The module
  CANNOT call ``AdaptationLedger.approve`` — only ``propose`` (wired
  in Slice 3). Operator approval via the existing ``/adapt`` REPL or
  ``ide_policy_router`` POST surface remains the SOLE path to actual
  policy mutation. The closure loop's job is to PREPARE proposals,
  not to APPLY them.

Determinism (Phase 1 substrate):
  ``compute_closure_outcome`` is a TOTAL pure function over its three
  inputs — same inputs always produce the same record (modulo the
  ``decided_at_ts`` field, which the caller stamps from a clock
  source it controls). Tests pass an explicit ``ts`` so verdicts
  are bit-stable.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    CoherenceAdvisory,
    TighteningProposalStatus,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
    BehavioralDriftKind,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay import (  # noqa: E501
    BranchVerdict,
    ReplayOutcome,
    ReplayVerdict,
)


CLOSURE_LOOP_SCHEMA_VERSION = "closure_loop_orchestrator.v1"


# ---------------------------------------------------------------------------
# Closed 6-value taxonomy of closure-loop outcomes (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class ClosureOutcome(str, enum.Enum):
    """6-value closed taxonomy. Every ``compute_closure_outcome``
    invocation returns exactly one — never None, never implicit
    fall-through. Slice 4 AST-pins the value vocabulary so silent
    additions break graduation.

    ``PROPOSED``                 — chain succeeded; the caller (Slice 3)
                                   submits the resulting tightening to
                                   ``AdaptationLedger.propose`` for
                                   operator approval. Note: PROPOSED
                                   does NOT mean "applied" — operator
                                   approval is still required.
    ``SKIPPED_NO_INTENT``        — advisory has no numerical
                                   ``tightening_intent`` (operator-
                                   notification kinds: POSTURE_LOCKED,
                                   SYMBOL_FLUX_DRIFT, POLICY_DEFAULT_DRIFT,
                                   RECURRENCE_DRIFT). The closure loop
                                   only handles numerical tightening
                                   proposals; the operator-only kinds
                                   surface to the human via existing
                                   coherence-advisory display.
    ``SKIPPED_VALIDATION_FAILED``— Tightener cage validator rejected
                                   the proposal. Examples: would-loosen,
                                   schema mismatch, observation_count
                                   below floor, payload deserialization
                                   failure. The cage's ``(ok, detail)``
                                   tuple is propagated into the record's
                                   ``detail`` field for observability.
    ``SKIPPED_REPLAY_REJECTED``  — Counterfactual Replay determined the
                                   proposed tightening would have made
                                   past sessions WORSE
                                   (``BranchVerdict.DIVERGED_BETTER`` —
                                   original was better than counter-
                                   factual; counterfactual was the
                                   tighter policy). Empirical evidence
                                   AGAINST tightening; the proposal is
                                   structurally rejected. Also covers
                                   replay infrastructure failures
                                   (FAILED / PARTIAL / DIVERGED outcomes
                                   where we can't make a clean
                                   determination).
    ``DISABLED``                 — master flag off. No work done; no
                                   record persisted. Returned for
                                   observability so the caller can log
                                   "I was asked to compute but you
                                   turned me off."
    ``FAILED``                   — defensive sentinel: garbage input,
                                   None advisory, schema mismatch on
                                   nested objects. NEVER raised — the
                                   primitive contract is that this
                                   function NEVER raises into callers.
    """

    PROPOSED = "proposed"
    SKIPPED_NO_INTENT = "skipped_no_intent"
    SKIPPED_VALIDATION_FAILED = "skipped_validation_failed"
    SKIPPED_REPLAY_REJECTED = "skipped_replay_rejected"
    DISABLED = "disabled"
    FAILED = "failed"


# Outcomes that the Slice 3 wiring layer MUST NOT submit to
# ``AdaptationLedger.propose``. Pinned as a frozenset so the wiring
# code can do an explicit membership check (``outcome in
# _NON_PROPOSAL_OUTCOMES``) — and so Slice 4's AST validator can pin
# the literal value set against silent expansion. The intention: only
# ``PROPOSED`` reaches the ledger; every other outcome stays in the
# closure-loop history ring buffer for observability and never
# touches operator-facing approval surfaces.
_NON_PROPOSAL_OUTCOMES: frozenset = frozenset({
    ClosureOutcome.SKIPPED_NO_INTENT,
    ClosureOutcome.SKIPPED_VALIDATION_FAILED,
    ClosureOutcome.SKIPPED_REPLAY_REJECTED,
    ClosureOutcome.DISABLED,
    ClosureOutcome.FAILED,
})


# ---------------------------------------------------------------------------
# Frozen result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureLoopRecord:
    """One closure-loop chain evaluation. Frozen for safe propagation
    across the Slice 2 ring buffer + Slice 3 wiring + Slice 4 SSE.

    ``record_fingerprint`` is a stable sha256[:16] over (advisory_id,
    parameter_name, validator_ok, replay_outcome, replay_verdict,
    final_outcome). Same chain inputs produce same fingerprint →
    Slice 2's ring buffer dedupes idempotently.

    ``parameter_name`` carries the tightening parameter from the
    advisory's ``TighteningIntent`` (e.g.,
    ``"budget_route_drift_pct"``) so observers + operators can see
    at a glance WHAT was being tightened. Empty string when the
    advisory had no numerical intent.

    ``detail`` is a free-form one-liner about why the outcome is
    what it is — typically the validator's rejection reason or
    the replay's divergence summary. Bounded to 200 chars in
    ``__post_init__`` so observability surfaces don't have to
    truncate on the read side.
    """

    outcome: ClosureOutcome
    advisory_id: str
    drift_kind: BehavioralDriftKind
    parameter_name: str = ""
    current_value: Optional[float] = None
    proposed_value: Optional[float] = None
    validator_ok: bool = False
    validator_detail: str = ""
    replay_outcome: Optional[ReplayOutcome] = None
    replay_verdict: Optional[BranchVerdict] = None
    detail: str = ""
    decided_at_ts: float = 0.0
    record_fingerprint: str = ""
    schema_version: str = CLOSURE_LOOP_SCHEMA_VERSION

    def is_actionable(self) -> bool:
        """True iff Slice 3 should submit this to
        ``AdaptationLedger.propose``. Equivalent to
        ``outcome is ClosureOutcome.PROPOSED`` — exposed as a method
        so call sites read declaratively and the
        ``_NON_PROPOSAL_OUTCOMES`` invariant has a single source of
        truth."""
        return self.outcome not in _NON_PROPOSAL_OUTCOMES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "advisory_id": self.advisory_id,
            "drift_kind": self.drift_kind.value,
            "parameter_name": self.parameter_name,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "validator_ok": self.validator_ok,
            "validator_detail": self.validator_detail,
            "replay_outcome": (
                self.replay_outcome.value
                if self.replay_outcome is not None else None
            ),
            "replay_verdict": (
                self.replay_verdict.value
                if self.replay_verdict is not None else None
            ),
            "detail": self.detail,
            "decided_at_ts": self.decided_at_ts,
            "record_fingerprint": self.record_fingerprint,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["ClosureLoopRecord"]:
        """Schema-tolerant reconstruction. Returns ``None`` on
        schema mismatch / malformed shape. NEVER raises."""
        try:
            if not isinstance(raw, Mapping):
                return None
            if (
                raw.get("schema_version")
                != CLOSURE_LOOP_SCHEMA_VERSION
            ):
                return None
            outcome = ClosureOutcome(str(raw["outcome"]))
            drift_kind = BehavioralDriftKind(str(raw["drift_kind"]))
            replay_outcome = None
            ro_raw = raw.get("replay_outcome")
            if isinstance(ro_raw, str):
                try:
                    replay_outcome = ReplayOutcome(ro_raw)
                except ValueError:
                    replay_outcome = None
            replay_verdict = None
            rv_raw = raw.get("replay_verdict")
            if isinstance(rv_raw, str):
                try:
                    replay_verdict = BranchVerdict(rv_raw)
                except ValueError:
                    replay_verdict = None
            current_raw = raw.get("current_value")
            current_value = (
                float(current_raw)
                if isinstance(current_raw, (int, float)) else None
            )
            proposed_raw = raw.get("proposed_value")
            proposed_value = (
                float(proposed_raw)
                if isinstance(proposed_raw, (int, float)) else None
            )
            return cls(
                outcome=outcome,
                advisory_id=str(raw.get("advisory_id", "")),
                drift_kind=drift_kind,
                parameter_name=str(raw.get("parameter_name", "")),
                current_value=current_value,
                proposed_value=proposed_value,
                validator_ok=bool(raw.get("validator_ok", False)),
                validator_detail=str(raw.get("validator_detail", "")),
                replay_outcome=replay_outcome,
                replay_verdict=replay_verdict,
                detail=str(raw.get("detail", ""))[:200],
                decided_at_ts=float(raw.get("decided_at_ts", 0.0)),
                record_fingerprint=str(
                    raw.get("record_fingerprint", ""),
                ),
            )
        except (KeyError, ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Internal: stable record fingerprint
# ---------------------------------------------------------------------------


def _record_fingerprint(
    advisory_id: str,
    parameter_name: str,
    validator_ok: bool,
    replay_outcome: Optional[ReplayOutcome],
    replay_verdict: Optional[BranchVerdict],
    final_outcome: ClosureOutcome,
) -> str:
    """sha256[:16] over the chain's tuple of inputs+output. Same
    inputs produce same fingerprint → Slice 2's ring buffer dedup
    is idempotent. NEVER raises."""
    try:
        ro = replay_outcome.value if replay_outcome is not None else "_"
        rv = replay_verdict.value if replay_verdict is not None else "_"
        payload = (
            f"{advisory_id}|{parameter_name}|{int(validator_ok)}|"
            f"{ro}|{rv}|{final_outcome.value}"
        )
        return hashlib.sha256(
            payload.encode("utf-8"),
        ).hexdigest()[:16]
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Total decision function
# ---------------------------------------------------------------------------


def compute_closure_outcome(
    *,
    advisory: Optional[CoherenceAdvisory],
    validator_result: Tuple[bool, str],
    replay_verdict: Optional[ReplayVerdict],
    enabled: bool,
    decided_at_ts: float = 0.0,
) -> ClosureLoopRecord:
    """Total pure decision function over the closure-loop chain.

    NEVER raises into the caller — every failure mode collapses to a
    closed-enum outcome with a sanitized ``detail`` field.

    Decision tree (top-down, first match wins; later checks assume
    earlier checks didn't trigger):

      1. ``enabled`` is False                      → DISABLED
      2. ``advisory`` is None / missing fields     → FAILED
      3. ``advisory.tightening_status``
         is ``NEUTRAL_NOTIFICATION`` or no intent  → SKIPPED_NO_INTENT
      4. ``advisory.tightening_status`` is
         ``WOULD_LOOSEN`` / ``FAILED``             → SKIPPED_VALIDATION_FAILED
      5. ``validator_result[0]`` is False          → SKIPPED_VALIDATION_FAILED
      6. ``replay_verdict`` is None / outcome is
         ``DISABLED`` / ``FAILED`` / ``PARTIAL``   → SKIPPED_REPLAY_REJECTED
      7. ``replay_verdict.verdict`` is
         ``DIVERGED_BETTER``                       → SKIPPED_REPLAY_REJECTED
         (counterfactual=tighter was WORSE than
         original — empirical evidence AGAINST
         the proposed tightening)
      8. Otherwise (verdict is DIVERGED_WORSE,
         DIVERGED_NEUTRAL, EQUIVALENT, or FAILED   → PROPOSED
         on a SUCCESS replay — note FAILED is
         caught at step 6)

    The DIVERGED_NEUTRAL and EQUIVALENT cases land in PROPOSED
    deliberately: monotonic-tightening discipline says the cage
    should always favor TIGHTER unless we have empirical evidence
    AGAINST. Operator approval (via /adapt) remains the gate;
    proposing here just surfaces the candidate to the human.
    """
    if not enabled:
        return ClosureLoopRecord(
            outcome=ClosureOutcome.DISABLED,
            advisory_id=(
                advisory.advisory_id if advisory is not None else ""
            ),
            drift_kind=(
                advisory.drift_kind if advisory is not None
                else BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT
            ),
            detail="closure_loop_master_off",
            decided_at_ts=decided_at_ts,
        )

    # Step 2 — input shape sanity. None or missing nested fields
    # collapse to FAILED with a structured detail.
    if advisory is None:
        return ClosureLoopRecord(
            outcome=ClosureOutcome.FAILED,
            advisory_id="",
            drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            detail="advisory_is_none",
            decided_at_ts=decided_at_ts,
        )

    advisory_id = advisory.advisory_id
    drift_kind = advisory.drift_kind
    intent = advisory.tightening_intent
    parameter_name = intent.parameter_name if intent is not None else ""
    current_value = intent.current_value if intent is not None else None
    proposed_value = intent.proposed_value if intent is not None else None
    validator_ok, validator_detail = validator_result

    # Step 3 — advisory has no numerical intent (operator-notification
    # kinds: POSTURE_LOCKED, SYMBOL_FLUX_DRIFT, etc.). The closure
    # loop only handles tightening proposals; operator-only kinds
    # are surfaced to the human via existing advisory display.
    if (
        advisory.tightening_status
        is TighteningProposalStatus.NEUTRAL_NOTIFICATION
    ) or intent is None:
        outcome = ClosureOutcome.SKIPPED_NO_INTENT
        return ClosureLoopRecord(
            outcome=outcome,
            advisory_id=advisory_id,
            drift_kind=drift_kind,
            parameter_name=parameter_name,
            current_value=current_value,
            proposed_value=proposed_value,
            detail="operator_notification_only",
            decided_at_ts=decided_at_ts,
            record_fingerprint=_record_fingerprint(
                advisory_id, parameter_name, False, None, None,
                outcome,
            ),
        )

    # Step 4 — bridge already structurally rejects WOULD_LOOSEN, but
    # defend in depth: any non-PASSED status flows here and is
    # rejected as a validation failure.
    if (
        advisory.tightening_status
        is not TighteningProposalStatus.PASSED
    ):
        outcome = ClosureOutcome.SKIPPED_VALIDATION_FAILED
        return ClosureLoopRecord(
            outcome=outcome,
            advisory_id=advisory_id,
            drift_kind=drift_kind,
            parameter_name=parameter_name,
            current_value=current_value,
            proposed_value=proposed_value,
            validator_ok=False,
            validator_detail=(
                f"advisory_status:{advisory.tightening_status.value}"
            ),
            detail="advisory_tightening_status_not_passed",
            decided_at_ts=decided_at_ts,
            record_fingerprint=_record_fingerprint(
                advisory_id, parameter_name, False, None, None,
                outcome,
            ),
        )

    # Step 5 — Tightener cage validator rejected the proposal.
    if not validator_ok:
        outcome = ClosureOutcome.SKIPPED_VALIDATION_FAILED
        return ClosureLoopRecord(
            outcome=outcome,
            advisory_id=advisory_id,
            drift_kind=drift_kind,
            parameter_name=parameter_name,
            current_value=current_value,
            proposed_value=proposed_value,
            validator_ok=False,
            validator_detail=str(validator_detail)[:200],
            detail="cage_validator_rejected",
            decided_at_ts=decided_at_ts,
            record_fingerprint=_record_fingerprint(
                advisory_id, parameter_name, False, None, None,
                outcome,
            ),
        )

    # Step 6 — replay couldn't make a clean determination.
    if replay_verdict is None:
        outcome = ClosureOutcome.SKIPPED_REPLAY_REJECTED
        return ClosureLoopRecord(
            outcome=outcome,
            advisory_id=advisory_id,
            drift_kind=drift_kind,
            parameter_name=parameter_name,
            current_value=current_value,
            proposed_value=proposed_value,
            validator_ok=True,
            validator_detail=str(validator_detail)[:200],
            replay_outcome=None,
            replay_verdict=None,
            detail="replay_verdict_is_none",
            decided_at_ts=decided_at_ts,
            record_fingerprint=_record_fingerprint(
                advisory_id, parameter_name, True, None, None,
                outcome,
            ),
        )

    if replay_verdict.outcome in (
        ReplayOutcome.DISABLED,
        ReplayOutcome.FAILED,
        ReplayOutcome.PARTIAL,
        ReplayOutcome.DIVERGED,
    ):
        outcome = ClosureOutcome.SKIPPED_REPLAY_REJECTED
        return ClosureLoopRecord(
            outcome=outcome,
            advisory_id=advisory_id,
            drift_kind=drift_kind,
            parameter_name=parameter_name,
            current_value=current_value,
            proposed_value=proposed_value,
            validator_ok=True,
            validator_detail=str(validator_detail)[:200],
            replay_outcome=replay_verdict.outcome,
            replay_verdict=replay_verdict.verdict,
            detail=(
                f"replay_outcome:{replay_verdict.outcome.value}"
            ),
            decided_at_ts=decided_at_ts,
            record_fingerprint=_record_fingerprint(
                advisory_id, parameter_name, True,
                replay_verdict.outcome, replay_verdict.verdict,
                outcome,
            ),
        )

    # Step 7 — DIVERGED_BETTER means original was better than
    # tighter counterfactual = empirical evidence AGAINST the
    # proposed tightening. Reject.
    if replay_verdict.verdict is BranchVerdict.DIVERGED_BETTER:
        outcome = ClosureOutcome.SKIPPED_REPLAY_REJECTED
        return ClosureLoopRecord(
            outcome=outcome,
            advisory_id=advisory_id,
            drift_kind=drift_kind,
            parameter_name=parameter_name,
            current_value=current_value,
            proposed_value=proposed_value,
            validator_ok=True,
            validator_detail=str(validator_detail)[:200],
            replay_outcome=replay_verdict.outcome,
            replay_verdict=replay_verdict.verdict,
            detail=(
                "replay_evidence_against_tightening"
            ),
            decided_at_ts=decided_at_ts,
            record_fingerprint=_record_fingerprint(
                advisory_id, parameter_name, True,
                replay_verdict.outcome, replay_verdict.verdict,
                outcome,
            ),
        )

    # Step 8 — chain succeeded. Slice 3 caller submits this to
    # AdaptationLedger.propose with status=PROPOSED for operator
    # approval. The orchestrator NEVER calls .approve — that's
    # the operator's authority via /adapt or the IDE panel.
    outcome = ClosureOutcome.PROPOSED
    return ClosureLoopRecord(
        outcome=outcome,
        advisory_id=advisory_id,
        drift_kind=drift_kind,
        parameter_name=parameter_name,
        current_value=current_value,
        proposed_value=proposed_value,
        validator_ok=True,
        validator_detail=str(validator_detail)[:200],
        replay_outcome=replay_verdict.outcome,
        replay_verdict=replay_verdict.verdict,
        detail=(
            f"chain_complete_verdict:"
            f"{replay_verdict.verdict.value}"
        ),
        decided_at_ts=decided_at_ts,
        record_fingerprint=_record_fingerprint(
            advisory_id, parameter_name, True,
            replay_verdict.outcome, replay_verdict.verdict,
            outcome,
        ),
    )


# ---------------------------------------------------------------------------
# Slice 4 graduation hooks (placeholders — actual flag/invariant
# registration lands at graduation)
# ---------------------------------------------------------------------------


def closure_loop_orchestrator_enabled() -> bool:
    """Master switch. Default-FALSE deliberately (operator cost ramp,
    matches Move 6 discipline). Slice 4 may flip the default to true
    after empirical observation in shadow mode."""
    import os
    raw = os.environ.get("JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Slice 4 — graduation: shipped_code_invariants AST pins + FlagRegistry
# seeds. Master flag stays DEFAULT-FALSE deliberately (operator cost
# ramp, mirrors Move 6 graduation discipline) — sub-gates default-true
# when added in follow-up slices. The closure-loop is read-only over
# Coherence Auditor advisories + writes only PROPOSED-status rows to
# AdaptationLedger; operator approval via /adapt or VSCode panel
# remains the sole path to .approve. These invariants pin that
# discipline against silent refactor.
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Module-owned shipped-code invariants. Returns the list so the
    centralized seed loader can register them at boot. NEVER raises
    (returns ``[]`` on import failure — graduation soak path is
    fail-open per the established convention).

    Four invariants pin the closure-loop's authority discipline:

      1. ``closure_loop_outcome_vocabulary`` — the 6-value
         ``ClosureOutcome`` taxonomy is frozen against silent
         expansion (Slice 5b would add more values; this pin breaks
         until the test suite is updated).
      2. ``closure_loop_orchestrator_no_approve`` — orchestrator
         module body contains zero ``.approve`` calls. Authority
         invariant: orchestrator may PROPOSE only.
      3. ``closure_loop_bridge_no_approve`` — bridge module body
         contains zero ``.approve`` calls. Authority invariant:
         bridge may PROPOSE only.
      4. ``closure_loop_observer_no_approve`` — observer module
         body contains zero ``.approve`` calls. Authority invariant:
         observer may PROPOSE only.
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    import ast as _ast

    def _validate_outcome_vocabulary(tree, source) -> tuple:
        violations = []
        # Pin the literal 6-value vocabulary against silent
        # additions. The set must match the closed taxonomy
        # documented in ClosureOutcome's docstring.
        required = {
            "PROPOSED",
            "SKIPPED_NO_INTENT",
            "SKIPPED_VALIDATION_FAILED",
            "SKIPPED_REPLAY_REJECTED",
            "DISABLED",
            "FAILED",
        }
        seen = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and (
                node.name == "ClosureOutcome"
            ):
                for stmt in node.body:
                    if isinstance(stmt, _ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, _ast.Name):
                                seen.add(target.id)
        missing = required - seen
        if missing:
            violations.append(
                f"ClosureOutcome lost values: {sorted(missing)} — "
                "the closed taxonomy is frozen by Slice 4 graduation"
            )
        unexpected = seen - required - {"_generate_next_value_"}
        if unexpected:
            violations.append(
                f"ClosureOutcome gained unpinned values: "
                f"{sorted(unexpected)} — update the AST pin AND "
                "the test suite when widening the vocabulary"
            )
        return tuple(violations)

    def _validate_no_approve_call(tree, source) -> tuple:
        # Generic AST walker — finds .approve(...) function call
        # patterns anywhere in the module body. The closure-loop
        # never calls AdaptationLedger.approve.
        violations = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                func = node.func
                if isinstance(func, _ast.Attribute) and (
                    func.attr == "approve"
                ):
                    violations.append(
                        f"forbidden .approve call at line "
                        f"{node.lineno} — closure-loop may PROPOSE only; "
                        "operator approval via /adapt remains the gate"
                    )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="closure_loop_outcome_vocabulary",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_orchestrator.py"
            ),
            description=(
                "ClosureOutcome's 6-value closed taxonomy is frozen. "
                "Adding a 7th value silently breaks downstream "
                "_NON_PROPOSAL_OUTCOMES set membership invariants "
                "and the proposal-emission gate."
            ),
            validate=_validate_outcome_vocabulary,
        ),
        ShippedCodeInvariant(
            invariant_name="closure_loop_orchestrator_no_approve",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_orchestrator.py"
            ),
            description=(
                "Orchestrator module body MUST NOT contain any "
                ".approve() call. The closure-loop's authority is "
                "PROPOSE only — operator approval via /adapt REPL "
                "or VSCode confidencePolicyPanel is the SOLE path "
                "to AdaptationLedger.approve / yaml_writer.write."
            ),
            validate=_validate_no_approve_call,
        ),
        ShippedCodeInvariant(
            invariant_name="closure_loop_bridge_no_approve",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_bridge.py"
            ),
            description=(
                "Bridge module body MUST NOT contain any "
                ".approve() call. The bridge's authority is "
                "PROPOSE only — same invariant as the orchestrator."
            ),
            validate=_validate_no_approve_call,
        ),
        ShippedCodeInvariant(
            invariant_name="closure_loop_observer_no_approve",
            target_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_observer.py"
            ),
            description=(
                "Observer module body MUST NOT contain any "
                ".approve() call. The observer's authority is "
                "READ + PROPOSE only — same invariant."
            ),
            validate=_validate_no_approve_call,
        ),
    ]


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Mirrors the
    discovery contract used by ``counterfactual_replay`` /
    ``gradient_observer`` etc. — the seed loader walks
    ``verification/`` for modules exposing this name + invokes
    once at boot. Adding a new flag requires zero edits to the seed
    file. Returns count of FlagSpecs registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0
    specs = [
        # --- Master flag (default FALSE — operator cost ramp) -------
        FlagSpec(
            name="JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED",
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master switch for the autonomous RSI closure-loop. "
                "When false, the observer reads no advisories, the "
                "store accepts no records, and the bridge submits "
                "no proposals. Default FALSE deliberately — operator "
                "cost ramp (mirrors Move 6 graduation discipline). "
                "Flip to true after empirical observation in shadow "
                "mode confirms the chain produces sensible records."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_orchestrator.py"
            ),
            example="false",
            since="Q4 Priority #2 Slice 4",
        ),
        # --- Store env knobs -------------------------------------------
        FlagSpec(
            name="JARVIS_CLOSURE_LOOP_HISTORY_DIR",
            type=FlagType.STR,
            default=".jarvis",
            description=(
                "Directory for the closure-loop bounded JSONL ring "
                "buffer (paired with posture / semantic_index / "
                "last_session_summary state under .jarvis/)."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_store.py"
            ),
            example=".jarvis",
            since="Q4 Priority #2 Slice 2",
        ),
        FlagSpec(
            name="JARVIS_CLOSURE_LOOP_HISTORY_MAX_RECORDS",
            type=FlagType.INT,
            default=1024,
            description=(
                "Ring buffer capacity for the closure-loop history. "
                "Clamped [16, 65536]. Bounded growth is a §8 "
                "invariant — operators set this once, system rotates."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_store.py"
            ),
            example="1024",
            since="Q4 Priority #2 Slice 2",
        ),
        # --- Observer cadence + lifecycle env knobs --------------------
        FlagSpec(
            name="JARVIS_CLOSURE_LOOP_OBSERVER_INTERVAL_S",
            type=FlagType.FLOAT,
            default=600.0,
            description=(
                "Base sleep interval between closure-loop observer "
                "passes. Default 600s (10 min) clamped [60.0, 7200.0]. "
                "Matches CIGW + Coherence cadence so the three "
                "observers tick on similar wall-clock cycles."
            ),
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_observer.py"
            ),
            example="600",
            since="Q4 Priority #2 Slice 2",
        ),
        FlagSpec(
            name="JARVIS_CLOSURE_LOOP_OBSERVER_DRIFT_MULTIPLIER",
            type=FlagType.FLOAT,
            default=0.5,
            description=(
                "Multiplier applied to the base interval when the "
                "previous pass emitted records (operator wants "
                "quicker re-tick after drift). Default 0.5; clamped "
                "[0.1, 1.0]; effective interval floored at 60s."
            ),
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_observer.py"
            ),
            example="0.5",
            since="Q4 Priority #2 Slice 2",
        ),
        FlagSpec(
            name="JARVIS_CLOSURE_LOOP_OBSERVER_FAILURE_BACKOFF_CEILING_S",
            type=FlagType.FLOAT,
            default=3600.0,
            description=(
                "Upper bound on the linear failure backoff. Default "
                "3600s (1 hour) clamped [60.0, 86400.0]. Backoff = "
                "min(ceiling, base × consecutive_failures)."
            ),
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_observer.py"
            ),
            example="3600",
            since="Q4 Priority #2 Slice 2",
        ),
        FlagSpec(
            name="JARVIS_CLOSURE_LOOP_OBSERVER_LIVENESS_PULSE_PASSES",
            type=FlagType.INT,
            default=4,
            description=(
                "Emit a liveness record every Nth pass even when "
                "no new advisories. Default 4; clamped [1, 1024]. "
                "Set to 1 in tests for deterministic emission."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_observer.py"
            ),
            example="4",
            since="Q4 Priority #2 Slice 2",
        ),
        FlagSpec(
            name="JARVIS_CLOSURE_LOOP_OBSERVER_DEDUP_RING_SIZE",
            type=FlagType.INT,
            default=256,
            description=(
                "Bounded fingerprint dedup ring size. Default 256; "
                "clamped [16, 16384]. The same advisory processed "
                "twice within the ring window is suppressed."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/verification/"
                "closure_loop_observer.py"
            ),
            example="256",
            since="Q4 Priority #2 Slice 2",
        ),
    ]
    try:
        registry.bulk_register(specs, override=True)
    except Exception:  # noqa: BLE001 — defensive
        return 0
    return len(specs)


__all__ = [
    "CLOSURE_LOOP_SCHEMA_VERSION",
    "ClosureLoopRecord",
    "ClosureOutcome",
    "closure_loop_orchestrator_enabled",
    "compute_closure_outcome",
    "register_flags",
    "register_shipped_invariants",
]
