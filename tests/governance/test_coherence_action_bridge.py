"""Priority #1 Slice 4 — auto_action_router bridge tests.

Coverage:

  * **Sub-gate flag** — asymmetric env semantics, default false.
  * **Env knobs** — `tighten_factor` defaults + clamps; advisory
    path resolution.
  * **Closed taxonomies** — `CoherenceAdvisoryAction` 6-value pin,
    `TighteningProposalStatus` 4-value pin, `RecordOutcome`
    5-value pin.
  * **1:1 mapping** — every `BehavioralDriftKind` maps to exactly
    one `CoherenceAdvisoryAction`; no orphaned kinds.
  * **Monotonic-tightening verification** — PASSED for
    smaller-is-tighter when proposed<current; WOULD_LOOSEN when
    proposed>=current; same for larger-is-tighter; NEUTRAL on
    None intent. Canonical `MonotonicTighteningVerdict` string
    paired correctly.
  * **Default proposer** — numeric kinds produce intent with
    expected current/proposed; non-numeric kinds return None;
    floor-clamped (recurrence_count floor 2; route_drift_pct
    floor 5).
  * **Bridge propose** — disabled returns empty; non-DRIFT_DETECTED
    verdicts return empty; per-finding advisory production.
  * **WOULD_LOOSEN structural reject** — bridge converts
    loosening proposals to NEUTRAL_NOTIFICATION (audit chain
    cannot be corrupted by bad proposer).
  * **Advisory persistence** — record + read round-trip;
    schema-tolerance; drift_kind filter.
  * **Defensive contract** — every public function NEVER raises.
  * **Authority invariants** — AST-pinned: stdlib + Slice 1 +
    AdaptationLedger.MonotonicTighteningVerdict + Tier 1 #3 only;
    no orchestrator imports; MUST reference both load-bearing
    symbols.
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    MonotonicTighteningVerdict,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (
    BehavioralDriftFinding,
    BehavioralDriftKind,
    BehavioralDriftVerdict,
    CoherenceOutcome,
    DriftBudgets,
    DriftSeverity,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (
    COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION,
    CoherenceAdvisory,
    CoherenceAdvisoryAction,
    RecordOutcome,
    TighteningIntent,
    TighteningProposalStatus,
    advisory_path_default,
    coherence_action_bridge_enabled,
    propose_coherence_action,
    read_coherence_advisories,
    record_coherence_advisory,
    tighten_factor,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    _DefaultTighteningProposer,
    _KIND_TO_ACTION,
    _verify_monotonic_tightening,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_path_advisory():
    d = Path(tempfile.mkdtemp(prefix="cab_test_")).resolve()
    yield d / "coherence_advisory.jsonl"
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_finding(*, kind=None, severity=None, delta=10.0, budget=5.0):
    return BehavioralDriftFinding(
        kind=kind or BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
        severity=severity or DriftSeverity.HIGH,
        detail="test finding",
        delta_metric=delta,
        budget_metric=budget,
    )


def _make_verdict(*, findings=None, outcome=None):
    f = findings if findings is not None else (_make_finding(),)
    o = outcome or CoherenceOutcome.DRIFT_DETECTED
    return BehavioralDriftVerdict(
        outcome=o, findings=f,
        largest_severity=DriftSeverity.HIGH,
        drift_signature="a" * 64, detail="test",
    )


# ---------------------------------------------------------------------------
# 1. Sub-gate flag
# ---------------------------------------------------------------------------


class TestSubGateFlag:
    def test_default_is_false(self):
        os.environ.pop(
            "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED", None,
        )
        assert coherence_action_bridge_enabled() is False

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_truthy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": v},
        ):
            assert coherence_action_bridge_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off"],
    )
    def test_falsy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": v},
        ):
            assert coherence_action_bridge_enabled() is False


# ---------------------------------------------------------------------------
# 2. Env knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_tighten_factor_default(self):
        os.environ.pop("JARVIS_COHERENCE_TIGHTEN_FACTOR", None)
        assert tighten_factor() == 0.8

    def test_tighten_factor_floor(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_TIGHTEN_FACTOR": "0.1"},
        ):
            assert tighten_factor() == 0.5

    def test_tighten_factor_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_TIGHTEN_FACTOR": "1.5"},
        ):
            assert tighten_factor() == 0.95

    def test_tighten_factor_garbage_returns_default(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_TIGHTEN_FACTOR": "not-a-number"},
        ):
            assert tighten_factor() == 0.8

    def test_advisory_path_env_override(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ADVISORY_PATH": "/tmp/custom.jsonl"},
        ):
            p = advisory_path_default()
            assert "custom.jsonl" in str(p)


# ---------------------------------------------------------------------------
# 3. Closed taxonomies (J.A.R.M.A.T.R.I.X. pins)
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:
    def test_advisory_action_has_6_values(self):
        assert len(list(CoherenceAdvisoryAction)) == 6

    def test_advisory_action_values(self):
        expected = {
            "tighten_risk_budget",
            "operator_notification_posture",
            "raise_risk_tier_for_module",
            "operator_notification_policy",
            "inject_postmortem_recall_hint",
            "tighten_confidence_budget",
        }
        assert {
            a.value for a in CoherenceAdvisoryAction
        } == expected

    def test_tightening_status_4_values(self):
        assert len(list(TighteningProposalStatus)) == 4

    def test_record_outcome_5_values(self):
        assert len(list(RecordOutcome)) == 5

    def test_record_outcome_values(self):
        expected = {
            "recorded", "deduped", "rejected_loosen",
            "disabled", "failed",
        }
        assert {o.value for o in RecordOutcome} == expected


# ---------------------------------------------------------------------------
# 4. 1:1 mapping completeness
# ---------------------------------------------------------------------------


class TestMappingCompleteness:
    def test_every_drift_kind_maps_to_action(self):
        for kind in BehavioralDriftKind:
            assert kind in _KIND_TO_ACTION, (
                f"{kind} missing from mapping"
            )

    def test_mapping_size(self):
        assert len(_KIND_TO_ACTION) == 6

    def test_mapping_actions_unique(self):
        # 1:1 — every action used exactly once
        actions = list(_KIND_TO_ACTION.values())
        assert len(set(actions)) == len(actions)

    @pytest.mark.parametrize(
        "kind,expected_action",
        [
            (
                BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
                CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET,
            ),
            (
                BehavioralDriftKind.POSTURE_LOCKED,
                (
                    CoherenceAdvisoryAction
                    .OPERATOR_NOTIFICATION_POSTURE
                ),
            ),
            (
                BehavioralDriftKind.SYMBOL_FLUX_DRIFT,
                CoherenceAdvisoryAction.RAISE_RISK_TIER_FOR_MODULE,
            ),
            (
                BehavioralDriftKind.POLICY_DEFAULT_DRIFT,
                (
                    CoherenceAdvisoryAction
                    .OPERATOR_NOTIFICATION_POLICY
                ),
            ),
            (
                BehavioralDriftKind.RECURRENCE_DRIFT,
                CoherenceAdvisoryAction.INJECT_POSTMORTEM_RECALL_HINT,
            ),
            (
                BehavioralDriftKind.CONFIDENCE_DRIFT,
                CoherenceAdvisoryAction.TIGHTEN_CONFIDENCE_BUDGET,
            ),
        ],
    )
    def test_specific_mappings(self, kind, expected_action):
        assert _KIND_TO_ACTION[kind] is expected_action


# ---------------------------------------------------------------------------
# 5. Monotonic-tightening verification
# ---------------------------------------------------------------------------


class TestMonotonicTightening:
    def test_smaller_is_tighter_passes_when_proposed_smaller(self):
        intent = TighteningIntent(
            parameter_name="x",
            current_value=25.0, proposed_value=20.0,
            direction="smaller_is_tighter",
        )
        status, ledger = _verify_monotonic_tightening(intent)
        assert status is TighteningProposalStatus.PASSED
        assert (
            ledger == MonotonicTighteningVerdict.PASSED.value
        )

    def test_smaller_is_tighter_loosens_when_proposed_larger(self):
        intent = TighteningIntent(
            parameter_name="x",
            current_value=25.0, proposed_value=30.0,
            direction="smaller_is_tighter",
        )
        status, ledger = _verify_monotonic_tightening(intent)
        assert (
            status is TighteningProposalStatus.WOULD_LOOSEN
        )
        assert ledger == (
            MonotonicTighteningVerdict
            .REJECTED_WOULD_LOOSEN.value
        )

    def test_smaller_is_tighter_equal_is_loosen(self):
        # Equal isn't strict tightening
        intent = TighteningIntent(
            parameter_name="x",
            current_value=25.0, proposed_value=25.0,
            direction="smaller_is_tighter",
        )
        status, _ = _verify_monotonic_tightening(intent)
        assert status is TighteningProposalStatus.WOULD_LOOSEN

    def test_larger_is_tighter_passes_when_proposed_larger(self):
        intent = TighteningIntent(
            parameter_name="threshold",
            current_value=0.7, proposed_value=0.8,
            direction="larger_is_tighter",
        )
        status, ledger = _verify_monotonic_tightening(intent)
        assert status is TighteningProposalStatus.PASSED

    def test_larger_is_tighter_loosens_when_proposed_smaller(self):
        intent = TighteningIntent(
            parameter_name="threshold",
            current_value=0.7, proposed_value=0.6,
            direction="larger_is_tighter",
        )
        status, _ = _verify_monotonic_tightening(intent)
        assert status is TighteningProposalStatus.WOULD_LOOSEN

    def test_none_intent_neutral_notification(self):
        status, ledger = _verify_monotonic_tightening(None)
        assert status is (
            TighteningProposalStatus.NEUTRAL_NOTIFICATION
        )
        assert (
            ledger == MonotonicTighteningVerdict.PASSED.value
        )

    def test_unknown_direction_failed(self):
        intent = TighteningIntent(
            parameter_name="x",
            current_value=1.0, proposed_value=2.0,
            direction="???",
        )
        status, _ = _verify_monotonic_tightening(intent)
        assert status is TighteningProposalStatus.FAILED


# ---------------------------------------------------------------------------
# 6. Default proposer
# ---------------------------------------------------------------------------


class TestDefaultProposer:
    def test_route_drift_produces_intent(self):
        p = _DefaultTighteningProposer()
        b = DriftBudgets.from_env()
        f = _make_finding(
            kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
        )
        intent = p.propose(f, b)
        assert intent is not None
        assert intent.parameter_name == "route_drift_pct"
        assert intent.current_value == b.route_drift_pct
        assert intent.proposed_value < intent.current_value
        assert intent.direction == "smaller_is_tighter"

    def test_recurrence_drift_floor_2(self):
        p = _DefaultTighteningProposer()
        # Recurrence floor enforces proposed >= 2
        b = DriftBudgets(recurrence_count=2)
        f = _make_finding(
            kind=BehavioralDriftKind.RECURRENCE_DRIFT,
        )
        intent = p.propose(f, b)
        # Already at floor; can't tighten further → None
        assert intent is None

    def test_confidence_drift_floor_10(self):
        p = _DefaultTighteningProposer()
        b = DriftBudgets(confidence_rise_pct=10.0)
        f = _make_finding(
            kind=BehavioralDriftKind.CONFIDENCE_DRIFT,
        )
        intent = p.propose(f, b)
        # Already at floor → None
        assert intent is None

    @pytest.mark.parametrize(
        "kind",
        [
            BehavioralDriftKind.POSTURE_LOCKED,
            BehavioralDriftKind.SYMBOL_FLUX_DRIFT,
            BehavioralDriftKind.POLICY_DEFAULT_DRIFT,
        ],
    )
    def test_non_numeric_kinds_return_none(self, kind):
        p = _DefaultTighteningProposer()
        b = DriftBudgets.from_env()
        f = _make_finding(kind=kind)
        assert p.propose(f, b) is None

    def test_route_drift_floor_5(self):
        # Edge case: budget already at floor; proposer must return None
        p = _DefaultTighteningProposer()
        b = DriftBudgets(route_drift_pct=5.0)
        f = _make_finding(
            kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
        )
        intent = p.propose(f, b)
        # 5.0 * 0.8 = 4.0; clamped to floor 5.0; proposed >=
        # current → None
        assert intent is None


# ---------------------------------------------------------------------------
# 7. Bridge propose — top-level decision tree
# ---------------------------------------------------------------------------


class TestBridgePropose:
    def test_disabled_returns_empty(self):
        os.environ.pop(
            "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED", None,
        )
        v = _make_verdict()
        out = propose_coherence_action(v)
        assert out == tuple()

    def test_coherent_verdict_returns_empty(self):
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.COHERENT,
            findings=tuple(), drift_signature="",
        )
        out = propose_coherence_action(
            v, enabled_override=True,
        )
        assert out == tuple()

    def test_insufficient_data_returns_empty(self):
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.INSUFFICIENT_DATA,
            findings=tuple(), drift_signature="",
        )
        out = propose_coherence_action(
            v, enabled_override=True,
        )
        assert out == tuple()

    def test_failed_verdict_returns_empty(self):
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.FAILED,
            findings=tuple(), drift_signature="",
        )
        out = propose_coherence_action(
            v, enabled_override=True,
        )
        assert out == tuple()

    def test_garbage_input_returns_empty(self):
        out = propose_coherence_action(
            "not a verdict",  # type: ignore[arg-type]
            enabled_override=True,
        )
        assert out == tuple()

    def test_per_finding_advisory_count(self):
        findings = (
            _make_finding(
                kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            ),
            _make_finding(
                kind=BehavioralDriftKind.POSTURE_LOCKED,
            ),
            _make_finding(
                kind=BehavioralDriftKind.RECURRENCE_DRIFT,
                delta=5.0,
            ),
        )
        v = _make_verdict(findings=findings)
        out = propose_coherence_action(
            v, enabled_override=True,
        )
        assert len(out) == 3
        kinds = {a.drift_kind for a in out}
        assert kinds == {
            BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            BehavioralDriftKind.POSTURE_LOCKED,
            BehavioralDriftKind.RECURRENCE_DRIFT,
        }

    def test_action_correctly_mapped(self):
        v = _make_verdict()  # default kind = BEHAVIORAL_ROUTE_DRIFT
        out = propose_coherence_action(
            v, enabled_override=True,
        )
        assert len(out) == 1
        assert (
            out[0].action
            is CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET
        )

    def test_severity_carried_through(self):
        f = _make_finding(severity=DriftSeverity.HIGH)
        v = _make_verdict(findings=(f,))
        out = propose_coherence_action(
            v, enabled_override=True,
        )
        assert out[0].severity is DriftSeverity.HIGH

    def test_drift_signature_carried_through(self):
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.DRIFT_DETECTED,
            findings=(_make_finding(),),
            largest_severity=DriftSeverity.HIGH,
            drift_signature="my-unique-sig", detail="x",
        )
        out = propose_coherence_action(
            v, enabled_override=True,
        )
        assert out[0].drift_signature == "my-unique-sig"

    def test_advisory_id_stable_for_same_inputs(self):
        ts = time.time()
        v = _make_verdict()
        out1 = propose_coherence_action(
            v, enabled_override=True, now_ts=ts,
        )
        out2 = propose_coherence_action(
            v, enabled_override=True, now_ts=ts,
        )
        assert out1[0].advisory_id == out2[0].advisory_id


# ---------------------------------------------------------------------------
# 8. WOULD_LOOSEN structural reject
# ---------------------------------------------------------------------------


class TestWouldLoosenReject:
    """Bridge MUST convert WOULD_LOOSEN proposals to NEUTRAL_
    NOTIFICATION before persisting. The audit chain cannot
    structurally contain a loosen-actionable advisory."""

    def test_bad_proposer_loosens_converted_to_neutral(self):
        class BadProposer:
            def propose(self, finding, budgets):
                # Maliciously propose a LOOSER value
                return TighteningIntent(
                    parameter_name="x",
                    current_value=25.0,
                    proposed_value=50.0,  # LOOSER!
                    direction="smaller_is_tighter",
                )

        v = _make_verdict()
        out = propose_coherence_action(
            v, enabled_override=True,
            proposer=BadProposer(),
        )
        assert len(out) == 1
        # Bridge structurally rejects the loosening intent
        assert out[0].tightening_intent is None
        assert out[0].tightening_status is (
            TighteningProposalStatus.NEUTRAL_NOTIFICATION
        )
        # Ledger verdict reflects the original loosen attempt
        assert out[0].monotonic_tightening_verdict == (
            MonotonicTighteningVerdict
            .REJECTED_WOULD_LOOSEN.value
        )

    def test_record_rejects_loosen_advisory_defensively(
        self, tmp_path_advisory,
    ):
        """If a WOULD_LOOSEN advisory somehow reaches record
        (shouldn't happen via propose but defensive), it's
        rejected with REJECTED_LOOSEN."""
        adv = CoherenceAdvisory(
            advisory_id="x" * 16,
            drift_signature="sig",
            drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            action=CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET,
            severity=DriftSeverity.HIGH,
            detail="bad",
            recorded_at_ts=time.time(),
            tightening_status=(
                TighteningProposalStatus.WOULD_LOOSEN
            ),
        )
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": "true"},
        ):
            out = record_coherence_advisory(
                adv, path=tmp_path_advisory,
            )
            assert out is RecordOutcome.REJECTED_LOOSEN


# ---------------------------------------------------------------------------
# 9. Persistence — record + read round-trip
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_record_disabled_returns_disabled(
        self, tmp_path_advisory,
    ):
        os.environ.pop(
            "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED", None,
        )
        adv = CoherenceAdvisory(
            advisory_id="a" * 16, drift_signature="sig",
            drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            action=CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET,
            severity=DriftSeverity.HIGH, detail="x",
            recorded_at_ts=time.time(),
            tightening_status=TighteningProposalStatus.PASSED,
        )
        out = record_coherence_advisory(
            adv, path=tmp_path_advisory,
        )
        assert out is RecordOutcome.DISABLED

    def test_record_enabled_returns_recorded(
        self, tmp_path_advisory,
    ):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": "true"},
        ):
            v = _make_verdict()
            advisories = propose_coherence_action(
                v, enabled_override=True,
            )
            for a in advisories:
                out = record_coherence_advisory(
                    a, path=tmp_path_advisory,
                )
                assert out is RecordOutcome.RECORDED

    def test_record_garbage_returns_failed(
        self, tmp_path_advisory,
    ):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": "true"},
        ):
            out = record_coherence_advisory(
                "not an advisory",  # type: ignore[arg-type]
                path=tmp_path_advisory,
            )
            assert out is RecordOutcome.FAILED

    def test_round_trip(self, tmp_path_advisory):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": "true"},
        ):
            v = _make_verdict()
            advisories = propose_coherence_action(
                v, enabled_override=True,
            )
            for a in advisories:
                record_coherence_advisory(
                    a, path=tmp_path_advisory,
                )
            recovered = read_coherence_advisories(
                path=tmp_path_advisory, since_ts=0.0,
            )
            assert len(recovered) == len(advisories)
            assert (
                recovered[0].advisory_id
                == advisories[0].advisory_id
            )
            assert recovered[0].drift_kind is advisories[0].drift_kind
            assert recovered[0].action is advisories[0].action

    def test_read_filter_by_drift_kind(self, tmp_path_advisory):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": "true"},
        ):
            findings = (
                _make_finding(
                    kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
                ),
                _make_finding(
                    kind=BehavioralDriftKind.POSTURE_LOCKED,
                ),
            )
            v = _make_verdict(findings=findings)
            advisories = propose_coherence_action(
                v, enabled_override=True,
            )
            for a in advisories:
                record_coherence_advisory(
                    a, path=tmp_path_advisory,
                )
            filtered = read_coherence_advisories(
                path=tmp_path_advisory, since_ts=0.0,
                drift_kind=BehavioralDriftKind.POSTURE_LOCKED,
            )
            assert len(filtered) == 1
            assert filtered[0].drift_kind is (
                BehavioralDriftKind.POSTURE_LOCKED
            )

    def test_read_since_ts_filter(self, tmp_path_advisory):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": "true"},
        ):
            v = _make_verdict()
            advisories = propose_coherence_action(
                v, enabled_override=True,
            )
            for a in advisories:
                record_coherence_advisory(
                    a, path=tmp_path_advisory,
                )
            future = read_coherence_advisories(
                path=tmp_path_advisory,
                since_ts=time.time() + 3600,
            )
            assert future == tuple()

    def test_read_limit_keeps_newest(self, tmp_path_advisory):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": "true"},
        ):
            for _ in range(5):
                v = _make_verdict()
                ts = time.time()
                advisories = propose_coherence_action(
                    v, enabled_override=True, now_ts=ts,
                )
                for a in advisories:
                    record_coherence_advisory(
                        a, path=tmp_path_advisory,
                    )
                time.sleep(0.001)
            limited = read_coherence_advisories(
                path=tmp_path_advisory, since_ts=0.0, limit=2,
            )
            assert len(limited) == 2

    def test_read_corrupt_lines_skipped(self, tmp_path_advisory):
        # Write a malformed line directly
        tmp_path_advisory.parent.mkdir(parents=True, exist_ok=True)
        v = _make_verdict()
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": "true"},
        ):
            advs = propose_coherence_action(
                v, enabled_override=True,
            )
            assert advs
            with open(tmp_path_advisory, "w") as f:
                f.write("garbage-line\n")
                f.write(json.dumps(advs[0].to_dict()) + "\n")
                f.write('{"schema_version":"wrong"}\n')
        recovered = read_coherence_advisories(
            path=tmp_path_advisory, since_ts=0.0,
        )
        # One valid recovered; corrupt + wrong-schema dropped
        assert len(recovered) == 1


# ---------------------------------------------------------------------------
# 10. Schema integrity
# ---------------------------------------------------------------------------


class TestSchemaIntegrity:
    def test_advisory_frozen(self):
        a = CoherenceAdvisory(
            advisory_id="x" * 16, drift_signature="s",
            drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            action=CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET,
            severity=DriftSeverity.HIGH, detail="d",
            recorded_at_ts=1.0,
            tightening_status=TighteningProposalStatus.PASSED,
        )
        with pytest.raises((AttributeError, Exception)):
            a.severity = DriftSeverity.LOW  # type: ignore[misc]

    def test_intent_frozen(self):
        i = TighteningIntent(
            parameter_name="x", current_value=1.0,
            proposed_value=0.5,
            direction="smaller_is_tighter",
        )
        with pytest.raises((AttributeError, Exception)):
            i.proposed_value = 999.0  # type: ignore[misc]

    def test_to_dict_round_trip(self):
        a = CoherenceAdvisory(
            advisory_id="abc", drift_signature="sig",
            drift_kind=BehavioralDriftKind.RECURRENCE_DRIFT,
            action=(
                CoherenceAdvisoryAction
                .INJECT_POSTMORTEM_RECALL_HINT
            ),
            severity=DriftSeverity.MEDIUM,
            detail="test",
            recorded_at_ts=12345.6,
            tightening_status=TighteningProposalStatus.PASSED,
            tightening_intent=TighteningIntent(
                parameter_name="recurrence_count",
                current_value=3.0, proposed_value=2.0,
                direction="smaller_is_tighter",
            ),
        )
        d = a.to_dict()
        recovered = CoherenceAdvisory.from_dict(d)
        assert recovered is not None
        assert recovered.advisory_id == a.advisory_id
        assert recovered.drift_kind is a.drift_kind
        assert recovered.action is a.action
        assert (
            recovered.tightening_intent.proposed_value
            == 2.0
        )

    def test_from_dict_schema_mismatch_returns_none(self):
        d = {"schema_version": "wrong"}
        assert CoherenceAdvisory.from_dict(d) is None

    def test_from_dict_malformed_returns_none(self):
        d = {
            "schema_version": (
                COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION
            ),
            "drift_kind": "not_a_real_kind",
        }
        assert CoherenceAdvisory.from_dict(d) is None

    def test_schema_version_stable(self):
        assert (
            COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION
            == "coherence_action_bridge.1"
        )

    def test_is_actionable_helper(self):
        passed = CoherenceAdvisory(
            advisory_id="x", drift_signature="s",
            drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            action=CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET,
            severity=DriftSeverity.HIGH, detail="",
            recorded_at_ts=1.0,
            tightening_status=TighteningProposalStatus.PASSED,
        )
        assert passed.is_actionable() is True

        neutral = CoherenceAdvisory(
            advisory_id="y", drift_signature="s",
            drift_kind=BehavioralDriftKind.POSTURE_LOCKED,
            action=(
                CoherenceAdvisoryAction
                .OPERATOR_NOTIFICATION_POSTURE
            ),
            severity=DriftSeverity.MEDIUM, detail="",
            recorded_at_ts=1.0,
            tightening_status=(
                TighteningProposalStatus.NEUTRAL_NOTIFICATION
            ),
        )
        assert neutral.is_actionable() is False


# ---------------------------------------------------------------------------
# 11. Defensive contract
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_propose_with_garbage_proposer(self):
        class BadProposer:
            def propose(self, finding, budgets):
                raise RuntimeError("proposer-broken")

        v = _make_verdict()
        out = propose_coherence_action(
            v, enabled_override=True,
            proposer=BadProposer(),
        )
        # Per-finding defensive — proposer raise → skip that
        # finding without crashing the overall call
        assert isinstance(out, tuple)

    def test_read_nonexistent_path_returns_empty(self):
        result = read_coherence_advisories(
            path=Path("/tmp/nonexistent-xyz-abc.jsonl"),
            since_ts=0.0,
        )
        assert result == tuple()


# ---------------------------------------------------------------------------
# 12. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


def _module_source() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "verification" / "coherence_action_bridge.py"
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariants:
    @pytest.fixture
    def source(self):
        return _module_source()

    def test_no_orchestrator_imports(self, source):
        forbidden = [
            "orchestrator", "iron_gate", "policy",
            "change_engine", "candidate_generator", "providers",
            "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "phase_runners",
            "semantic_guardian", "semantic_firewall",
            "risk_engine",
        ]
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                module = module or ""
                for f in forbidden:
                    assert f not in module, (
                        f"forbidden import: {module}"
                    )

    def test_governance_imports_in_allowlist(self, source):
        """Slice 4 may import:
          * Slice 1 (coherence_auditor)
          * Slice 2 (coherence_window_store, lazy via fn)
          * adaptation.ledger (MonotonicTighteningVerdict only)
          * cross_process_jsonl (Tier 1 #3)"""
        tree = ast.parse(source)
        allowed = {
            "backend.core.ouroboros.governance.adaptation.ledger",
            "backend.core.ouroboros.governance.cross_process_jsonl",
            "backend.core.ouroboros.governance.verification.coherence_auditor",
            "backend.core.ouroboros.governance.verification.coherence_window_store",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "governance" in node.module:
                    assert node.module in allowed, (
                        f"governance import outside allowlist: "
                        f"{node.module}"
                    )

    def test_must_reference_monotonic_tightening_verdict(
        self, source,
    ):
        """STRUCTURAL universal-cage-rule pin. Bridge MUST
        reference `MonotonicTighteningVerdict` from
        `adaptation.ledger`. Catches a refactor that drops
        the AdaptationLedger vocabulary integration."""
        assert "MonotonicTighteningVerdict" in source, (
            "bridge dropped MonotonicTighteningVerdict — "
            "Phase C universal cage rule integration is gone"
        )

    def test_must_reference_flock_append_line(self, source):
        """STRUCTURAL cross-process safety pin."""
        assert "flock_append_line" in source

    def test_must_import_from_adaptation_ledger(self, source):
        """Pin the EXACT importfrom line."""
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    == "backend.core.ouroboros.governance"
                    ".adaptation.ledger"
                ):
                    for alias in node.names:
                        if (
                            alias.name
                            == "MonotonicTighteningVerdict"
                        ):
                            found = True
        assert found, (
            "bridge must import MonotonicTighteningVerdict via "
            "importfrom from adaptation.ledger"
        )

    def test_no_mutation_tools(self, source):
        forbidden = [
            "edit_file", "write_file", "delete_file",
            "subprocess." + "run", "subprocess." + "Popen",
            "os." + "system", "shutil.rmtree",
        ]
        for f in forbidden:
            assert f not in source

    def test_no_exec_eval_compile(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    )

    def test_no_async_functions(self, source):
        """Slice 4 is sync; Slice 5 surfaces will wrap async."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef)

    def test_public_api_exported(self, source):
        for name in (
            "propose_coherence_action",
            "record_coherence_advisory",
            "read_coherence_advisories",
            "CoherenceAdvisory", "CoherenceAdvisoryAction",
            "TighteningProposalStatus", "TighteningIntent",
            "TighteningProposer",
            "RecordOutcome",
            "coherence_action_bridge_enabled",
            "tighten_factor",
            "advisory_path_default",
            "COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION",
        ):
            assert f'"{name}"' in source
