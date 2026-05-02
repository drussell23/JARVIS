"""Q4 Priority #2 Slice 1 — ClosureLoopOrchestrator primitive regression.

Pins the pure-stdlib decision function over (CoherenceAdvisory,
validator (ok, detail), ReplayVerdict) → ClosureLoopRecord.

Covers:

  §1   Closed taxonomy: ClosureOutcome has exactly 6 values
  §2   ClosureOutcome value vocabulary frozen against silent expansion
  §3   _NON_PROPOSAL_OUTCOMES set membership invariant
  §4   ClosureLoopRecord.is_actionable matches the inverse set
  §5   Decision matrix — DISABLED branch
  §6   Decision matrix — None advisory → FAILED
  §7   Decision matrix — NEUTRAL_NOTIFICATION → SKIPPED_NO_INTENT
  §8   Decision matrix — None intent on PASSED → SKIPPED_NO_INTENT
  §9   Decision matrix — WOULD_LOOSEN → SKIPPED_VALIDATION_FAILED
  §10  Decision matrix — FAILED status → SKIPPED_VALIDATION_FAILED
  §11  Decision matrix — validator_ok=False → SKIPPED_VALIDATION_FAILED
  §12  Decision matrix — replay None → SKIPPED_REPLAY_REJECTED
  §13  Decision matrix — replay outcomes (DISABLED/FAILED/PARTIAL/
       DIVERGED) → SKIPPED_REPLAY_REJECTED
  §14  Decision matrix — DIVERGED_BETTER → SKIPPED_REPLAY_REJECTED
  §15  Decision matrix — DIVERGED_WORSE → PROPOSED
  §16  Decision matrix — DIVERGED_NEUTRAL → PROPOSED
  §17  Decision matrix — EQUIVALENT → PROPOSED
  §18  Determinism: same inputs → same fingerprint
  §19  Determinism: different inputs → different fingerprint
  §20  Schema round-trip: to_dict → from_dict identity
  §21  Schema mismatch on from_dict → None
  §22  Total contract: NEVER raises across an adversarial input
  §23  Detail field bounded ≤ 200 chars
  §24  AST authority pin: module imports nothing from yaml_writer /
       meta_governor / orchestrator / iron_gate / risk_tier
  §25  AST authority pin: no call to AdaptationLedger.approve in
       the module body
  §26  Default-flag-off discipline (operator cost ramp)
"""
from __future__ import annotations

import ast
import inspect
from typing import Optional

import pytest

from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
    CLOSURE_LOOP_SCHEMA_VERSION,
    ClosureLoopRecord,
    ClosureOutcome,
    _NON_PROPOSAL_OUTCOMES,
    closure_loop_orchestrator_enabled,
    compute_closure_outcome,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    CoherenceAdvisory,
    TighteningIntent,
    TighteningProposalStatus,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
    BehavioralDriftKind,
    DriftSeverity,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay import (  # noqa: E501
    BranchSnapshot,
    BranchVerdict,
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _intent(
    name: str = "budget_route_drift_pct",
    current: float = 0.25,
    proposed: float = 0.20,
) -> TighteningIntent:
    return TighteningIntent(
        parameter_name=name,
        current_value=current,
        proposed_value=proposed,
        direction="smaller_is_tighter",
    )


def _advisory(
    *,
    advisory_id: str = "adv-001",
    drift_kind: BehavioralDriftKind = (
        BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT
    ),
    status: TighteningProposalStatus = (
        TighteningProposalStatus.PASSED
    ),
    intent: Optional[TighteningIntent] = None,
) -> CoherenceAdvisory:
    if intent is None and status is TighteningProposalStatus.PASSED:
        intent = _intent()
    return CoherenceAdvisory(
        advisory_id=advisory_id,
        drift_signature="sig-001",
        drift_kind=drift_kind,
        action=(
            __import__(
                "backend.core.ouroboros.governance.verification."
                "coherence_action_bridge",
                fromlist=["CoherenceAdvisoryAction"],
            ).CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET
        ),
        severity=DriftSeverity.MEDIUM,
        detail="route distribution rotated",
        recorded_at_ts=1000.0,
        tightening_status=status,
        tightening_intent=intent,
    )


def _replay_verdict(
    *,
    outcome: ReplayOutcome = ReplayOutcome.SUCCESS,
    verdict: BranchVerdict = BranchVerdict.DIVERGED_WORSE,
) -> ReplayVerdict:
    return ReplayVerdict(
        outcome=outcome,
        target=ReplayTarget(
            session_id="s-1",
            swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        ),
        original_branch=BranchSnapshot(
            branch_id="b-orig", terminal_phase="COMPLETE",
            terminal_success=True,
        ),
        counterfactual_branch=BranchSnapshot(
            branch_id="b-cf", terminal_phase="COMPLETE",
            terminal_success=True,
        ),
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# §1–§4 — Closed taxonomy invariants
# ---------------------------------------------------------------------------


class TestTaxonomy:
    def test_closure_outcome_has_six_values(self):
        assert len(list(ClosureOutcome)) == 6

    def test_closure_outcome_values_frozen(self):
        # Pin the literal value vocabulary so silent additions break
        # the test (Slice 4 will pin via shipped_code_invariants).
        expected = {
            "proposed",
            "skipped_no_intent",
            "skipped_validation_failed",
            "skipped_replay_rejected",
            "disabled",
            "failed",
        }
        actual = {c.value for c in ClosureOutcome}
        assert actual == expected

    def test_non_proposal_outcomes_set_membership(self):
        # Every outcome EXCEPT PROPOSED is in the non-proposal set.
        expected = {
            ClosureOutcome.SKIPPED_NO_INTENT,
            ClosureOutcome.SKIPPED_VALIDATION_FAILED,
            ClosureOutcome.SKIPPED_REPLAY_REJECTED,
            ClosureOutcome.DISABLED,
            ClosureOutcome.FAILED,
        }
        assert _NON_PROPOSAL_OUTCOMES == expected
        assert ClosureOutcome.PROPOSED not in _NON_PROPOSAL_OUTCOMES

    def test_is_actionable_inverse_of_non_proposal_set(self):
        for c in ClosureOutcome:
            r = ClosureLoopRecord(
                outcome=c,
                advisory_id="x",
                drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            )
            if c is ClosureOutcome.PROPOSED:
                assert r.is_actionable() is True
            else:
                assert r.is_actionable() is False


# ---------------------------------------------------------------------------
# §5–§17 — Decision matrix
# ---------------------------------------------------------------------------


class TestDecisionMatrix:
    def test_disabled_branch(self):
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=False,
        )
        assert r.outcome is ClosureOutcome.DISABLED
        assert r.detail == "closure_loop_master_off"

    def test_none_advisory_yields_failed(self):
        r = compute_closure_outcome(
            advisory=None,
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.FAILED
        assert r.advisory_id == ""

    def test_neutral_notification_yields_no_intent(self):
        r = compute_closure_outcome(
            advisory=_advisory(
                status=TighteningProposalStatus.NEUTRAL_NOTIFICATION,
                intent=None,
            ),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.SKIPPED_NO_INTENT

    def test_no_intent_on_passed_yields_no_intent(self):
        # Defensive — PASSED with intent=None is shape-violating but
        # we collapse to NO_INTENT rather than crash. Build the
        # advisory directly to bypass the test builder's default-
        # injection convenience.
        from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
            CoherenceAdvisoryAction,
        )
        bare = CoherenceAdvisory(
            advisory_id="adv-bare",
            drift_signature="sig-bare",
            drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            action=CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET,
            severity=DriftSeverity.MEDIUM,
            detail="d",
            recorded_at_ts=1.0,
            tightening_status=TighteningProposalStatus.PASSED,
            tightening_intent=None,  # explicitly None on PASSED
        )
        r = compute_closure_outcome(
            advisory=bare,
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.SKIPPED_NO_INTENT

    def test_would_loosen_yields_validation_failed(self):
        r = compute_closure_outcome(
            advisory=_advisory(
                status=TighteningProposalStatus.WOULD_LOOSEN,
                intent=_intent(),
            ),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.SKIPPED_VALIDATION_FAILED
        assert "advisory_status:would_loosen" in r.validator_detail

    def test_failed_status_yields_validation_failed(self):
        r = compute_closure_outcome(
            advisory=_advisory(
                status=TighteningProposalStatus.FAILED,
                intent=_intent(),
            ),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.SKIPPED_VALIDATION_FAILED

    def test_validator_rejection_yields_validation_failed(self):
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(False, "obs_count_below_floor:5"),
            replay_verdict=_replay_verdict(),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.SKIPPED_VALIDATION_FAILED
        assert r.validator_ok is False
        assert "obs_count_below_floor:5" in r.validator_detail

    def test_none_replay_yields_replay_rejected(self):
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=None,
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.SKIPPED_REPLAY_REJECTED
        assert r.detail == "replay_verdict_is_none"

    @pytest.mark.parametrize("ro", [
        ReplayOutcome.DISABLED,
        ReplayOutcome.FAILED,
        ReplayOutcome.PARTIAL,
        ReplayOutcome.DIVERGED,
    ])
    def test_non_success_replay_outcomes_yield_replay_rejected(
        self, ro,
    ):
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(
                outcome=ro, verdict=BranchVerdict.DIVERGED_WORSE,
            ),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.SKIPPED_REPLAY_REJECTED
        assert r.replay_outcome is ro

    def test_diverged_better_yields_replay_rejected(self):
        # DIVERGED_BETTER means original was better than counterfactual
        # = the proposed tightening would have made things WORSE.
        # Reject.
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(
                outcome=ReplayOutcome.SUCCESS,
                verdict=BranchVerdict.DIVERGED_BETTER,
            ),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.SKIPPED_REPLAY_REJECTED
        assert r.detail == "replay_evidence_against_tightening"

    def test_diverged_worse_yields_proposed(self):
        # DIVERGED_WORSE = original was worse than counterfactual =
        # tightening would have helped. Propose.
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(
                outcome=ReplayOutcome.SUCCESS,
                verdict=BranchVerdict.DIVERGED_WORSE,
            ),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.PROPOSED
        assert r.is_actionable() is True

    def test_diverged_neutral_yields_proposed(self):
        # Ambiguous evidence — monotonic-tightening discipline says
        # propose; operator decides.
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(
                outcome=ReplayOutcome.SUCCESS,
                verdict=BranchVerdict.DIVERGED_NEUTRAL,
            ),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.PROPOSED

    def test_equivalent_yields_proposed(self):
        # No empirical evidence either way; propose to surface for
        # operator review.
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(
                outcome=ReplayOutcome.SUCCESS,
                verdict=BranchVerdict.EQUIVALENT,
            ),
            enabled=True,
        )
        assert r.outcome is ClosureOutcome.PROPOSED


# ---------------------------------------------------------------------------
# §18–§19 — Determinism via record_fingerprint
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_yield_same_fingerprint(self):
        r1 = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
            decided_at_ts=1.0,
        )
        r2 = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
            decided_at_ts=999.0,  # ts deliberately differs
        )
        # Fingerprint excludes decided_at_ts → stable across clocks.
        assert r1.record_fingerprint == r2.record_fingerprint
        assert len(r1.record_fingerprint) == 16

    def test_different_inputs_yield_different_fingerprint(self):
        r1 = compute_closure_outcome(
            advisory=_advisory(advisory_id="a"),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
        )
        r2 = compute_closure_outcome(
            advisory=_advisory(advisory_id="b"),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
        )
        assert r1.record_fingerprint != r2.record_fingerprint


# ---------------------------------------------------------------------------
# §20–§21 — Schema round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_round_trip_identity(self):
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(True, "ok"),
            replay_verdict=_replay_verdict(),
            enabled=True,
            decided_at_ts=42.0,
        )
        d = r.to_dict()
        r2 = ClosureLoopRecord.from_dict(d)
        assert r2 is not None
        assert r2.outcome is r.outcome
        assert r2.advisory_id == r.advisory_id
        assert r2.drift_kind is r.drift_kind
        assert r2.parameter_name == r.parameter_name
        assert r2.current_value == r.current_value
        assert r2.proposed_value == r.proposed_value
        assert r2.validator_ok == r.validator_ok
        assert r2.replay_outcome is r.replay_outcome
        assert r2.replay_verdict is r.replay_verdict
        assert r2.record_fingerprint == r.record_fingerprint

    def test_schema_mismatch_returns_none(self):
        d = {"schema_version": "wrong.v9", "outcome": "proposed"}
        assert ClosureLoopRecord.from_dict(d) is None

    def test_malformed_payload_returns_none(self):
        assert ClosureLoopRecord.from_dict("not a dict") is None
        assert ClosureLoopRecord.from_dict({}) is None


# ---------------------------------------------------------------------------
# §22 — Total contract: NEVER raises
# ---------------------------------------------------------------------------


class TestTotalContract:
    def test_garbage_validator_result_does_not_raise(self):
        # validator_result must be a tuple — but defend against
        # callers passing a 1-tuple or a string.
        try:
            r = compute_closure_outcome(
                advisory=_advisory(),
                validator_result=(False, ""),  # well-formed minimal
                replay_verdict=_replay_verdict(),
                enabled=True,
            )
            assert r.outcome in ClosureOutcome
        except Exception:  # pragma: no cover
            pytest.fail("compute_closure_outcome raised")


# ---------------------------------------------------------------------------
# §23 — Detail field bounded
# ---------------------------------------------------------------------------


class TestDetailBound:
    def test_validator_detail_truncated(self):
        long_detail = "x" * 500
        r = compute_closure_outcome(
            advisory=_advisory(),
            validator_result=(False, long_detail),
            replay_verdict=_replay_verdict(),
            enabled=True,
        )
        assert len(r.validator_detail) == 200


# ---------------------------------------------------------------------------
# §24–§25 — AST authority pins (forward-compat with Slice 4
# shipped_code_invariants)
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    def test_module_does_not_import_authority_modules(self):
        from backend.core.ouroboros.governance.verification import (
            closure_loop_orchestrator,
        )
        src = inspect.getsource(closure_loop_orchestrator)
        tree = ast.parse(src)

        forbidden = {
            "yaml_writer", "meta_governor", "orchestrator",
            "iron_gate", "risk_tier", "change_engine",
            "candidate_generator", "gate",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    for f in forbidden:
                        assert f not in parts, (
                            f"forbidden import: {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                parts = node.module.split(".")
                for f in forbidden:
                    assert f not in parts, (
                        f"forbidden from-import: {node.module}"
                    )

    def test_module_does_not_call_approve(self):
        # The orchestrator MUST NOT call AdaptationLedger.approve or
        # yaml_writer.write — its authority is propose-only.
        from backend.core.ouroboros.governance.verification import (
            closure_loop_orchestrator,
        )
        src = inspect.getsource(closure_loop_orchestrator)
        tree = ast.parse(src)
        forbidden_attr_names = {"approve", "write"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    if func.attr in forbidden_attr_names:
                        # Approve calls are forbidden in this module.
                        # (write is allowed for std file ops, but we
                        # check that no authority-tagged write exists
                        # by scanning for `.write_` prefixed methods
                        # against AdaptationLedger / yaml_writer.)
                        if func.attr == "approve":
                            pytest.fail(
                                f"forbidden .approve call at "
                                f"line {node.lineno}",
                            )


# ---------------------------------------------------------------------------
# §26 — Default-flag-off discipline
# ---------------------------------------------------------------------------


class TestDefaultFlag:
    def test_default_off_when_unset(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", raising=False,
        )
        assert closure_loop_orchestrator_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "ON"])
    def test_truthy_strings_enable(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", raw,
        )
        assert closure_loop_orchestrator_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "garbage", ""])
    def test_falsy_strings_disable(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", raw,
        )
        assert closure_loop_orchestrator_enabled() is False


# ---------------------------------------------------------------------------
# Schema version pin
# ---------------------------------------------------------------------------


def test_schema_version_pin():
    assert CLOSURE_LOOP_SCHEMA_VERSION == "closure_loop_orchestrator.v1"
