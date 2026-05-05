"""Upgrade 2 (PRD §31.3) Slice 1 — Closed taxonomy of decision
kinds for the DecisionRecord ledger.

Forces every site that calls :func:`capture_phase_decision` /
:func:`DecisionRuntime.record` through a single closed enum
rather than freeform strings. Symmetric discipline to
:class:`BudgetOutcome` (Upgrade 1) / :class:`OutcomeKind` (M11)
/ :class:`FailureModeKind` (Upgrade 3) / :class:`CuriositySource`
(M9).

**Backward-compat (load-bearing)**: existing freeform ``kind``
strings on already-shipped ledger files continue to read
cleanly via :meth:`DecisionRecord.from_dict` — the new enum
uses ``str`` subclass so its ``.value`` substitutes for the
freeform string at the byte-level. Old records written with
strings like ``"route_assignment"`` (route_runner.py:193)
remain readable and replay'able. New writes use
``DecisionKind.PHASE_TRANSITION.value`` etc.

**12 values** (PRD §31.3.2 names ≥10):

  1. ROUTE_SELECTION — :func:`urgency_router.classify` outcome
  2. GATE_PASS — Iron Gate (post-GENERATE) accept verdict
  3. GATE_FAIL — Iron Gate reject verdict (with reason_code)
  4. VALIDATOR_PASS — VALIDATE phase pass (per validator)
  5. VALIDATOR_FAIL — VALIDATE phase fail (per validator)
  6. RISK_ESCALATION — :func:`risk_tier_floor.apply_floor_to_-
     name` raised the tier
  7. PROBE_TRIGGER — :class:`HypothesisProbe` invocation
  8. SBT_TRIGGER — :class:`SpeculativeBranchTree` invocation
  9. AUTO_ACTION_PROPOSAL — :mod:`auto_action_router` advisory
     emission
  10. APPROVAL_REQUEST — :class:`OrangePRReviewer` queue insert
  11. PHASE_TRANSITION — generic phase boundary capture
      (preserves existing route_runner / gate_runner /
      complete_runner / plan_runner ``kind=`` strings; READS
      these as PHASE_TRANSITION even though writes used the
      legacy string).
  12. DISABLED — sentinel for master-off paths

Closed enum — no operator-supplied kind strings. Adding a new
decision site means adding an enum value here first, then
wiring the call site. AST-pinned at Slice 5.
"""
from __future__ import annotations

import enum


DECISION_KIND_SCHEMA_VERSION: str = "decision_kind.1"


class DecisionKind(str, enum.Enum):
    """Closed taxonomy. ``str`` subclass so ``.value``
    substitutes for the freeform string at the byte level —
    backward-compat with pre-Upgrade-2 ledgers preserved by
    construction."""

    ROUTE_SELECTION = "route_selection"
    GATE_PASS = "gate_pass"
    GATE_FAIL = "gate_fail"
    VALIDATOR_PASS = "validator_pass"
    VALIDATOR_FAIL = "validator_fail"
    RISK_ESCALATION = "risk_escalation"
    PROBE_TRIGGER = "probe_trigger"
    SBT_TRIGGER = "sbt_trigger"
    AUTO_ACTION_PROPOSAL = "auto_action_proposal"
    APPROVAL_REQUEST = "approval_request"
    PHASE_TRANSITION = "phase_transition"
    DISABLED = "disabled"


__all__ = [
    "DECISION_KIND_SCHEMA_VERSION",
    "DecisionKind",
]
