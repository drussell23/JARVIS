"""Priority #1 Slice 1 — Behavioral primitive regression tests.

Coverage:

  * **Closed-taxonomy pins** — BehavioralDriftKind 6 values,
    CoherenceOutcome 5 values, DriftSeverity 4 values; any
    silent extension caught.
  * **Parity with SemanticIndex** — `_recency_weight` formula
    byte-equivalent to ``semantic_index._recency_weight`` across
    a parameterized age sweep. Pins the literal-reuse contract:
    we re-implement to stay pure-stdlib but the formula MUST
    match.
  * **Distribution math** — ``_total_variation_distance`` and
    ``_normalize_distribution`` correctness on
    identical/flipped/disjoint/empty inputs.
  * **Recency-weighted aggregation** — ``compute_behavioral_
    signature`` weights recent ops more than older ops; older
    ops decay per halflife.
  * **Posture lock detection** — ``_max_consecutive_hours``
    correctly identifies the longest run of a single posture
    value within a window, including tail run to window end.
  * **Drift detection — every kind** — each of 6
    BehavioralDriftKind values fires correctly when its
    threshold is crossed and stays silent when it isn't.
  * **Severity classification** — DriftSeverity ratios
    (1.0/1.5/3.0 boundaries) correctly classify findings.
  * **Outcome decision tree** — DISABLED / FAILED /
    INSUFFICIENT_DATA / COHERENT / DRIFT_DETECTED selected per
    closed-tree contract.
  * **Drift signature dedup stability** — same drift across
    repeated calls produces same `drift_signature` sha256.
  * **Defensive contract** — every public function NEVER
    raises; garbage inputs map to FAILED outcome or empty
    signatures.
  * **Schema integrity** — frozen dataclasses, to_dict /
    from_dict round-trip, schema_version stable.
  * **Authority invariants** — AST-pinned: stdlib only, no
    governance imports anywhere, no exec/eval/compile, no
    mutation tools, no async (Slice 1 is sync primitive).
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.verification.coherence_auditor import (
    COHERENCE_AUDITOR_SCHEMA_VERSION,
    BehavioralDriftFinding,
    BehavioralDriftKind,
    BehavioralDriftVerdict,
    BehavioralSignature,
    CoherenceOutcome,
    DriftBudgets,
    DriftSeverity,
    OpRecord,
    PostureRecord,
    WindowData,
    budget_confidence_rise_pct,
    budget_posture_locked_hours,
    budget_recurrence_count,
    budget_route_drift_pct,
    coherence_auditor_enabled,
    compute_behavioral_drift,
    compute_behavioral_signature,
    halflife_days,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
    _max_consecutive_hours,
    _normalize_distribution,
    _recency_weight,
    _severity_for_ratio,
    _total_variation_distance,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _coherent_signature(*, p99: int = 5) -> BehavioralSignature:
    """Build a signature within all default budgets."""
    return BehavioralSignature(
        window_start_ts=0.0,
        window_end_ts=86400.0 * 7,
        route_distribution={
            "standard": 0.6, "immediate": 0.4,
        },
        posture_distribution={"explore": 0.7, "consolidate": 0.3},
        module_fingerprints={"foo.py": "abc123"},
        p99_confidence_drop_count=p99,
        recurrence_index={},
        ops_summary={"apply": 10, "verify": 9, "commit": 9},
        posture_max_consecutive_hours=12.0,  # well under 48h budget
    )


# ---------------------------------------------------------------------------
# 1. Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_is_false(self):
        os.environ.pop("JARVIS_COHERENCE_AUDITOR_ENABLED", None)
        assert coherence_auditor_enabled() is False

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE", "Yes"],
    )
    def test_truthy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_AUDITOR_ENABLED": v},
        ):
            assert coherence_auditor_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off", "FALSE"],
    )
    def test_falsy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_AUDITOR_ENABLED": v},
        ):
            assert coherence_auditor_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t\n"])
    def test_whitespace_treated_as_unset(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_AUDITOR_ENABLED": v},
        ):
            assert coherence_auditor_enabled() is False


# ---------------------------------------------------------------------------
# 2. Env knobs — clamping
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_route_drift_pct_default(self):
        os.environ.pop(
            "JARVIS_COHERENCE_BUDGET_ROUTE_DRIFT_PCT", None,
        )
        assert budget_route_drift_pct() == 25.0

    def test_route_drift_pct_floor_clamp(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_BUDGET_ROUTE_DRIFT_PCT": "1.0"},
        ):
            assert budget_route_drift_pct() == 5.0

    def test_route_drift_pct_ceiling_clamp(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_BUDGET_ROUTE_DRIFT_PCT": "999"},
        ):
            assert budget_route_drift_pct() == 100.0

    def test_recurrence_count_floor(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_BUDGET_RECURRENCE_COUNT": "1"},
        ):
            assert budget_recurrence_count() == 2

    def test_recurrence_count_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_BUDGET_RECURRENCE_COUNT": "999"},
        ):
            assert budget_recurrence_count() == 50

    def test_posture_locked_hours_default(self):
        os.environ.pop(
            "JARVIS_COHERENCE_BUDGET_POSTURE_LOCKED_HOURS", None,
        )
        assert budget_posture_locked_hours() == 48.0

    def test_confidence_rise_pct_default(self):
        os.environ.pop(
            "JARVIS_COHERENCE_BUDGET_CONFIDENCE_RISE_PCT", None,
        )
        assert budget_confidence_rise_pct() == 50.0

    def test_halflife_days_default(self):
        os.environ.pop("JARVIS_COHERENCE_HALFLIFE_DAYS", None)
        assert halflife_days() == 14.0

    def test_garbage_value_falls_back_to_default(self):
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_COHERENCE_BUDGET_ROUTE_DRIFT_PCT":
                    "not-a-number",
            },
        ):
            assert budget_route_drift_pct() == 25.0


# ---------------------------------------------------------------------------
# 3. Closed-taxonomy pins — Move 4/5/6 J.A.R.M.A.T.R.I.X. discipline
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:
    def test_drift_kind_has_exactly_6_values(self):
        assert len(list(BehavioralDriftKind)) == 6

    def test_drift_kind_values(self):
        expected = {
            "behavioral_route_drift", "posture_locked",
            "symbol_flux_drift", "policy_default_drift",
            "recurrence_drift", "confidence_drift",
        }
        assert {
            k.value for k in BehavioralDriftKind
        } == expected

    def test_outcome_has_exactly_5_values(self):
        assert len(list(CoherenceOutcome)) == 5

    def test_outcome_values(self):
        expected = {
            "coherent", "drift_detected",
            "insufficient_data", "disabled", "failed",
        }
        assert {o.value for o in CoherenceOutcome} == expected

    def test_severity_has_exactly_4_values(self):
        assert len(list(DriftSeverity)) == 4

    def test_severity_values(self):
        expected = {"none", "low", "medium", "high"}
        assert {s.value for s in DriftSeverity} == expected


# ---------------------------------------------------------------------------
# 4. Parity with SemanticIndex — load-bearing literal-reuse contract
# ---------------------------------------------------------------------------


class TestSemanticIndexParity:
    """Coherence Auditor re-implements the recency_weight formula
    inline (rather than importing) to stay pure-stdlib. This pin
    enforces byte-exact parity with the source-of-truth in
    ``semantic_index._recency_weight``. If SemanticIndex's formula
    changes, this test fails immediately."""

    @pytest.mark.parametrize(
        "age_seconds",
        [
            0.0, 60.0, 3600.0, 86400.0,
            86400.0 * 3, 86400.0 * 7, 86400.0 * 14,
            86400.0 * 30, 86400.0 * 60,
        ],
    )
    @pytest.mark.parametrize("halflife", [3.0, 7.0, 14.0, 30.0])
    def test_recency_weight_byte_parity(
        self, age_seconds, halflife,
    ):
        from backend.core.ouroboros.governance.semantic_index import (  # noqa: E501
            _recency_weight as si_weight,
        )
        ours = _recency_weight(age_seconds, halflife)
        theirs = si_weight(age_seconds, halflife)
        assert ours == theirs, (
            f"recency_weight diverged at age={age_seconds:.0f}s "
            f"hl={halflife}d: ours={ours} theirs={theirs}"
        )

    def test_recency_weight_negative_age_returns_one(self):
        assert _recency_weight(-1.0, 14.0) == 1.0

    def test_recency_weight_zero_halflife_returns_one(self):
        assert _recency_weight(86400.0, 0.0) == 1.0


# ---------------------------------------------------------------------------
# 5. Distribution math
# ---------------------------------------------------------------------------


class TestDistributionMath:
    def test_normalize_empty_returns_empty(self):
        assert _normalize_distribution({}) == {}

    def test_normalize_zero_total_returns_empty(self):
        assert _normalize_distribution({"a": 0.0, "b": 0.0}) == {}

    def test_normalize_sums_to_one(self):
        result = _normalize_distribution({"a": 3.0, "b": 7.0})
        assert sum(result.values()) == pytest.approx(1.0)
        assert result["a"] == pytest.approx(0.3)
        assert result["b"] == pytest.approx(0.7)

    def test_tvd_identical_returns_zero(self):
        p = {"a": 0.5, "b": 0.5}
        assert _total_variation_distance(p, p) == 0.0

    def test_tvd_flipped_returns_half(self):
        # p = (0.5, 0.5), q = (0, 1) → TVD = 0.5*(0.5 + 0.5) = 0.5
        p = {"a": 0.5, "b": 0.5}
        q = {"a": 0.0, "b": 1.0}
        assert _total_variation_distance(p, q) == 0.5

    def test_tvd_disjoint_returns_one(self):
        p = {"a": 1.0}
        q = {"b": 1.0}
        assert _total_variation_distance(p, q) == 1.0

    def test_tvd_empty_returns_zero(self):
        assert _total_variation_distance({}, {}) == 0.0

    def test_tvd_one_empty_treats_as_unobserved(self):
        # One side empty → TVD = 0.5 * sum(|p[k] - 0|) = 0.5 (if
        # other sums to 1)
        p = {"a": 0.5, "b": 0.5}
        assert _total_variation_distance(p, {}) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 6. Severity classification
# ---------------------------------------------------------------------------


class TestSeverity:
    @pytest.mark.parametrize(
        "ratio,expected",
        [
            (0.0, DriftSeverity.NONE),
            (0.5, DriftSeverity.NONE),
            (0.999, DriftSeverity.NONE),
            (1.0, DriftSeverity.LOW),
            (1.49, DriftSeverity.LOW),
            (1.5, DriftSeverity.MEDIUM),
            (2.99, DriftSeverity.MEDIUM),
            (3.0, DriftSeverity.HIGH),
            (10.0, DriftSeverity.HIGH),
        ],
    )
    def test_severity_boundaries(self, ratio, expected):
        assert _severity_for_ratio(ratio) is expected

    def test_severity_garbage_returns_none(self):
        # Should not raise even on weird inputs
        result = _severity_for_ratio(float("nan"))
        # nan comparisons all return false → NONE
        assert result is DriftSeverity.NONE


# ---------------------------------------------------------------------------
# 7. Max consecutive hours
# ---------------------------------------------------------------------------


class TestMaxConsecutiveHours:
    def test_empty_returns_zero(self):
        assert _max_consecutive_hours(tuple(), 86400.0) == 0.0

    def test_single_record_run_to_window_end(self):
        records = (PostureRecord("explore", 0.0),)
        result = _max_consecutive_hours(records, 3600.0 * 5)
        assert result == 5.0

    def test_two_records_same_posture_run_to_end(self):
        records = (
            PostureRecord("explore", 0.0),
            PostureRecord("explore", 3600.0 * 2),
        )
        # No transition; tail run from t=0 to window_end=10h = 10h
        assert _max_consecutive_hours(records, 3600.0 * 10) == 10.0

    def test_transition_resets_run(self):
        records = (
            PostureRecord("explore", 0.0),
            PostureRecord("harden", 3600.0 * 3),
        )
        # explore: 0→3 = 3h. harden tail: 3→10 = 7h. Max = 7h.
        assert _max_consecutive_hours(records, 3600.0 * 10) == 7.0

    def test_unsorted_records_handled(self):
        records = (
            PostureRecord("harden", 3600.0 * 3),
            PostureRecord("explore", 0.0),
        )
        assert _max_consecutive_hours(records, 3600.0 * 10) == 7.0


# ---------------------------------------------------------------------------
# 8. compute_behavioral_signature
# ---------------------------------------------------------------------------


class TestComputeBehavioralSignature:
    def test_garbage_input_returns_empty_signature(self):
        sig = compute_behavioral_signature("not a WindowData")  # type: ignore[arg-type]
        assert isinstance(sig, BehavioralSignature)
        assert sig.route_distribution == {}

    def test_empty_window_data(self):
        data = WindowData(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
        )
        sig = compute_behavioral_signature(data)
        assert sig.route_distribution == {}
        assert sig.posture_distribution == {}
        assert sig.posture_max_consecutive_hours == 0.0

    def test_route_distribution_normalized(self):
        data = WindowData(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            op_records=tuple(
                OpRecord(f"op-{i}", "standard", 86400.0 * 7)
                for i in range(3)
            ) + tuple(
                OpRecord(f"op-bg-{i}", "background", 86400.0 * 7)
                for i in range(2)
            ),
        )
        sig = compute_behavioral_signature(data)
        # Same recency-weight for all (all at window end), so
        # 3:2 ratio → 0.6:0.4
        assert sig.route_distribution["standard"] == pytest.approx(0.6)
        assert sig.route_distribution["background"] == pytest.approx(0.4)

    def test_recency_weighting_recent_outweighs_old(self):
        # 1 recent op vs 2 old ops — with 14d halflife and one op
        # at the window end vs two ops 30d before, recent op
        # weight ~ 1.0, each old op weight ~ 0.226. So recent
        # 1*1.0 = 1.0 vs old 2*0.226 = 0.452 → recent wins.
        data = WindowData(
            window_start_ts=0.0, window_end_ts=86400.0 * 30,
            op_records=(
                OpRecord("recent-1", "immediate", 86400.0 * 30),
                OpRecord("old-1", "background", 0.0),
                OpRecord("old-2", "background", 0.0),
            ),
        )
        sig = compute_behavioral_signature(data)
        assert (
            sig.route_distribution.get("immediate", 0.0)
            > sig.route_distribution.get("background", 0.0)
        )

    def test_module_fingerprints_passthrough(self):
        data = WindowData(
            window_start_ts=0.0, window_end_ts=86400.0,
            module_fingerprints={"a.py": "h1", "b.py": "h2"},
        )
        sig = compute_behavioral_signature(data)
        assert sig.module_fingerprints == {"a.py": "h1", "b.py": "h2"}

    def test_signature_id_deterministic(self):
        data = WindowData(
            window_start_ts=0.0, window_end_ts=86400.0,
            module_fingerprints={"a.py": "h1"},
        )
        sig1 = compute_behavioral_signature(data)
        sig2 = compute_behavioral_signature(data)
        assert sig1.signature_id() == sig2.signature_id()
        assert len(sig1.signature_id()) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# 9. Drift detection — every kind
# ---------------------------------------------------------------------------


class TestDriftDetection:
    def test_disabled_short_circuit(self):
        sig = _coherent_signature()
        v = compute_behavioral_drift(
            sig, sig, enabled_override=False,
        )
        assert v.outcome is CoherenceOutcome.DISABLED

    def test_failed_on_garbage_curr(self):
        sig = _coherent_signature()
        v = compute_behavioral_drift(
            sig, "not a sig",  # type: ignore[arg-type]
            enabled_override=True,
        )
        assert v.outcome is CoherenceOutcome.FAILED

    def test_failed_on_garbage_prev(self):
        sig = _coherent_signature()
        v = compute_behavioral_drift(
            "not a sig", sig,  # type: ignore[arg-type]
            enabled_override=True,
        )
        assert v.outcome is CoherenceOutcome.FAILED

    def test_insufficient_data_first_window(self):
        sig = _coherent_signature()
        v = compute_behavioral_drift(
            None, sig, enabled_override=True,
        )
        assert v.outcome is CoherenceOutcome.INSUFFICIENT_DATA

    def test_coherent_when_within_all_budgets(self):
        sig = _coherent_signature()
        v = compute_behavioral_drift(
            sig, sig, enabled_override=True,
        )
        assert v.outcome is CoherenceOutcome.COHERENT
        assert len(v.findings) == 0

    def test_route_drift_detected(self):
        prev = _coherent_signature()
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            # Distribution flipped — 100% TVD
            route_distribution={"background": 1.0},
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints=dict(prev.module_fingerprints),
            p99_confidence_drop_count=5,
            recurrence_index={}, ops_summary={},
            posture_max_consecutive_hours=12.0,
        )
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        kinds = {f.kind for f in v.findings}
        assert (
            BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT in kinds
        )

    def test_posture_locked_detected(self):
        prev = _coherent_signature()
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution=dict(prev.route_distribution),
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints=dict(prev.module_fingerprints),
            p99_confidence_drop_count=5,
            recurrence_index={}, ops_summary={},
            posture_max_consecutive_hours=72.0,  # > 48h budget
        )
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        kinds = {f.kind for f in v.findings}
        assert BehavioralDriftKind.POSTURE_LOCKED in kinds

    def test_symbol_flux_without_apply(self):
        prev = _coherent_signature()
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution=dict(prev.route_distribution),
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints={"foo.py": "DIFFERENT"},
            p99_confidence_drop_count=5,
            recurrence_index={}, ops_summary={},
            posture_max_consecutive_hours=12.0,
        )
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
            apply_event_paths=frozenset(),
        )
        kinds = {f.kind for f in v.findings}
        assert BehavioralDriftKind.SYMBOL_FLUX_DRIFT in kinds

    def test_symbol_flux_suppressed_with_apply(self):
        prev = _coherent_signature()
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution=dict(prev.route_distribution),
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints={"foo.py": "DIFFERENT"},
            p99_confidence_drop_count=5,
            recurrence_index={}, ops_summary={},
            posture_max_consecutive_hours=12.0,
        )
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
            apply_event_paths=frozenset({"foo.py"}),
        )
        kinds = {f.kind for f in v.findings}
        assert (
            BehavioralDriftKind.SYMBOL_FLUX_DRIFT not in kinds
        )

    def test_symbol_flux_new_module_skipped(self):
        prev = _coherent_signature()
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution=dict(prev.route_distribution),
            posture_distribution=dict(prev.posture_distribution),
            # New module added — fingerprint not in prev
            module_fingerprints={
                "foo.py": "abc123",  # unchanged
                "new_module.py": "fresh",
            },
            p99_confidence_drop_count=5,
            recurrence_index={}, ops_summary={},
            posture_max_consecutive_hours=12.0,
        )
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        kinds = {f.kind for f in v.findings}
        # New module isn't flux — registry is observer's job
        assert (
            BehavioralDriftKind.SYMBOL_FLUX_DRIFT not in kinds
        )

    def test_policy_default_drift_detected(self):
        sig = _coherent_signature()
        v = compute_behavioral_drift(
            sig, sig, enabled_override=True,
            policy_observations={
                "JARVIS_FOO": (True, False),  # registered != observed
            },
        )
        kinds = {f.kind for f in v.findings}
        assert (
            BehavioralDriftKind.POLICY_DEFAULT_DRIFT in kinds
        )

    def test_policy_default_aligned_no_drift(self):
        sig = _coherent_signature()
        v = compute_behavioral_drift(
            sig, sig, enabled_override=True,
            policy_observations={
                "JARVIS_FOO": (True, True),
            },
        )
        kinds = {f.kind for f in v.findings}
        assert (
            BehavioralDriftKind.POLICY_DEFAULT_DRIFT not in kinds
        )

    def test_recurrence_drift_detected(self):
        prev = _coherent_signature()
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution=dict(prev.route_distribution),
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints=dict(prev.module_fingerprints),
            p99_confidence_drop_count=5,
            recurrence_index={"timeout_failure": 5},  # > 3 budget
            ops_summary={},
            posture_max_consecutive_hours=12.0,
        )
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        kinds = {f.kind for f in v.findings}
        assert BehavioralDriftKind.RECURRENCE_DRIFT in kinds

    def test_recurrence_below_budget_no_drift(self):
        prev = _coherent_signature()
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution=dict(prev.route_distribution),
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints=dict(prev.module_fingerprints),
            p99_confidence_drop_count=5,
            recurrence_index={"timeout_failure": 2},  # ≤ 3 budget
            ops_summary={},
            posture_max_consecutive_hours=12.0,
        )
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        kinds = {f.kind for f in v.findings}
        assert BehavioralDriftKind.RECURRENCE_DRIFT not in kinds

    def test_confidence_drift_detected(self):
        prev = _coherent_signature(p99=10)
        # 100% rise → 20 vs 10 → +100% > 50% budget
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution=dict(prev.route_distribution),
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints=dict(prev.module_fingerprints),
            p99_confidence_drop_count=20,
            recurrence_index={}, ops_summary={},
            posture_max_consecutive_hours=12.0,
        )
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        kinds = {f.kind for f in v.findings}
        assert BehavioralDriftKind.CONFIDENCE_DRIFT in kinds

    def test_confidence_decline_no_drift(self):
        prev = _coherent_signature(p99=20)
        curr = _coherent_signature(p99=10)  # going DOWN
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        kinds = {f.kind for f in v.findings}
        assert BehavioralDriftKind.CONFIDENCE_DRIFT not in kinds

    def test_multiple_kinds_aggregate(self):
        prev = _coherent_signature(p99=10)
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution={"background": 1.0},  # ROUTE drift
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints={"foo.py": "DIFF"},  # SYMBOL flux
            p99_confidence_drop_count=30,  # CONFIDENCE +200%
            recurrence_index={"x": 10},  # RECURRENCE drift
            ops_summary={},
            posture_max_consecutive_hours=72.0,  # POSTURE locked
        )
        v = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        kinds = {f.kind for f in v.findings}
        # 5 of 6 kinds — POLICY needs caller-supplied
        # observations
        assert len(kinds) >= 5


# ---------------------------------------------------------------------------
# 10. Drift signature dedup stability
# ---------------------------------------------------------------------------


class TestDriftSignatureStability:
    def test_same_drift_same_signature(self):
        prev = _coherent_signature()
        curr = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution={"background": 1.0},
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints=dict(prev.module_fingerprints),
            p99_confidence_drop_count=5,
            recurrence_index={}, ops_summary={},
            posture_max_consecutive_hours=12.0,
        )
        v1 = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        v2 = compute_behavioral_drift(
            prev, curr, enabled_override=True,
        )
        assert v1.drift_signature == v2.drift_signature
        assert len(v1.drift_signature) == 64

    def test_different_drift_different_signature(self):
        prev = _coherent_signature()
        # Drift A: route drift
        curr_a = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution={"background": 1.0},
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints=dict(prev.module_fingerprints),
            p99_confidence_drop_count=5,
            recurrence_index={}, ops_summary={},
            posture_max_consecutive_hours=12.0,
        )
        # Drift B: posture lock
        curr_b = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=86400.0 * 7,
            route_distribution=dict(prev.route_distribution),
            posture_distribution=dict(prev.posture_distribution),
            module_fingerprints=dict(prev.module_fingerprints),
            p99_confidence_drop_count=5,
            recurrence_index={}, ops_summary={},
            posture_max_consecutive_hours=72.0,
        )
        v_a = compute_behavioral_drift(
            prev, curr_a, enabled_override=True,
        )
        v_b = compute_behavioral_drift(
            prev, curr_b, enabled_override=True,
        )
        assert v_a.drift_signature != v_b.drift_signature

    def test_coherent_has_empty_signature(self):
        sig = _coherent_signature()
        v = compute_behavioral_drift(
            sig, sig, enabled_override=True,
        )
        assert v.outcome is CoherenceOutcome.COHERENT
        assert v.drift_signature == ""


# ---------------------------------------------------------------------------
# 11. is_actionable / has_drift
# ---------------------------------------------------------------------------


class TestVerdictHelpers:
    def test_coherent_not_actionable(self):
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.COHERENT,
        )
        assert v.has_drift() is False
        assert v.is_actionable() is False

    def test_drift_low_severity_not_actionable(self):
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.DRIFT_DETECTED,
            largest_severity=DriftSeverity.LOW,
        )
        assert v.has_drift() is True
        assert v.is_actionable() is False  # below MEDIUM threshold

    def test_drift_medium_severity_actionable(self):
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.DRIFT_DETECTED,
            largest_severity=DriftSeverity.MEDIUM,
        )
        assert v.is_actionable() is True

    def test_drift_high_severity_actionable(self):
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.DRIFT_DETECTED,
            largest_severity=DriftSeverity.HIGH,
        )
        assert v.is_actionable() is True


# ---------------------------------------------------------------------------
# 12. Schema integrity — frozen + round-trip + version stable
# ---------------------------------------------------------------------------


class TestSchemaIntegrity:
    def test_signature_is_frozen(self):
        sig = _coherent_signature()
        with pytest.raises((AttributeError, Exception)):
            sig.window_start_ts = 999.0  # type: ignore[misc]

    def test_finding_is_frozen(self):
        f = BehavioralDriftFinding(
            kind=BehavioralDriftKind.POSTURE_LOCKED,
            severity=DriftSeverity.LOW,
            detail="test",
            delta_metric=1.0, budget_metric=0.5,
        )
        with pytest.raises((AttributeError, Exception)):
            f.severity = DriftSeverity.HIGH  # type: ignore[misc]

    def test_verdict_is_frozen(self):
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.COHERENT,
        )
        with pytest.raises((AttributeError, Exception)):
            v.outcome = CoherenceOutcome.FAILED  # type: ignore[misc]

    def test_signature_to_dict_round_trip(self):
        sig = _coherent_signature()
        d = sig.to_dict()
        recon = BehavioralSignature.from_dict(d)
        assert recon is not None
        assert recon.window_start_ts == sig.window_start_ts
        assert dict(recon.route_distribution) == dict(
            sig.route_distribution,
        )
        assert dict(recon.module_fingerprints) == dict(
            sig.module_fingerprints,
        )

    def test_signature_from_dict_schema_mismatch_returns_none(self):
        d = {"schema_version": "wrong_version"}
        assert BehavioralSignature.from_dict(d) is None

    def test_signature_from_dict_malformed_returns_none(self):
        d = {
            "schema_version": COHERENCE_AUDITOR_SCHEMA_VERSION,
            "window_start_ts": "not_a_float",
        }
        assert BehavioralSignature.from_dict(d) is None

    def test_schema_version_stable(self):
        assert (
            COHERENCE_AUDITOR_SCHEMA_VERSION
            == "coherence_auditor.1"
        )

    def test_drift_budgets_from_env(self):
        b = DriftBudgets.from_env()
        assert b.route_drift_pct == 25.0
        assert b.posture_locked_hours == 48.0
        assert b.recurrence_count == 3


# ---------------------------------------------------------------------------
# 13. Defensive contract — never raises
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_compute_signature_with_none_does_not_raise(self):
        # Type-violating but defensive
        sig = compute_behavioral_signature(None)  # type: ignore[arg-type]
        assert isinstance(sig, BehavioralSignature)

    def test_compute_drift_with_both_none_returns_failed_or_insufficient(
        self,
    ):
        v = compute_behavioral_drift(
            None, None, enabled_override=True,
        )
        # curr is None → FAILED (curr validity check before
        # prev presence check)
        assert v.outcome is CoherenceOutcome.FAILED

    def test_compute_drift_with_corrupted_distributions(self):
        # Distributions with NaN values should not raise
        sig_a = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=1.0,
            route_distribution={"a": float("nan")},
            posture_distribution={},
            module_fingerprints={},
            p99_confidence_drop_count=0,
            recurrence_index={}, ops_summary={},
        )
        v = compute_behavioral_drift(
            sig_a, sig_a, enabled_override=True,
        )
        # Either COHERENT or DRIFT_DETECTED; never raises
        assert v.outcome in (
            CoherenceOutcome.COHERENT,
            CoherenceOutcome.DRIFT_DETECTED,
        )


# ---------------------------------------------------------------------------
# 14. Authority invariants — AST-pinned import discipline
# ---------------------------------------------------------------------------


def _module_source() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "verification" / "coherence_auditor.py"
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariants:
    @pytest.fixture
    def source(self):
        return _module_source()

    def test_no_governance_imports(self, source):
        """Slice 1 is PURE-STDLIB. No governance imports of any
        kind — strongest authority invariant possible. Slice 3
        observer does the collection from governance modules;
        Slice 1 receives pre-aggregated data."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "backend." not in module, (
                    f"forbidden backend import: {module}"
                )
                assert "governance" not in module, (
                    f"forbidden governance import: {module}"
                )

    def test_no_orchestrator_or_authority_imports(self, source):
        forbidden_substrings = [
            "orchestrator", "iron_gate", "policy",
            "change_engine", "candidate_generator",
            "providers", "doubleword_provider",
            "urgency_router", "auto_action_router",
            "subagent_scheduler", "tool_executor",
            "phase_runners", "semantic_guardian",
            "semantic_firewall", "risk_engine",
            "ast_canonical", "semantic_index",
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
                for f in forbidden_substrings:
                    assert f not in module, (
                        f"forbidden import: {module}"
                    )

    def test_no_mutation_tools(self, source):
        forbidden = [
            "edit_file", "write_file", "delete_file",
            "subprocess." + "run", "subprocess." + "Popen",
            "os." + "system", "os.remove", "os.unlink",
            "shutil.rmtree",
        ]
        for f in forbidden:
            assert f not in source, (
                f"coherence_auditor contains forbidden token: "
                f"{f!r}"
            )

    def test_no_exec_eval_compile(self, source):
        """Critical safety pin — auditor compares fingerprints,
        NEVER executes shipped code. Mirrors Move 6 Slice 2
        ast_canonical's discipline."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    ), (
                        f"forbidden call: {node.func.id}"
                    )

    def test_no_async_functions(self, source):
        """Slice 1 is sync primitive; Slice 3 introduces async."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef), (
                f"forbidden async function in Slice 1: "
                f"{node.name}"
            )

    def test_public_api_exported(self, source):
        for name in (
            "compute_behavioral_signature",
            "compute_behavioral_drift",
            "BehavioralSignature",
            "BehavioralDriftVerdict",
            "BehavioralDriftKind",
            "CoherenceOutcome",
            "DriftSeverity",
            "DriftBudgets",
            "WindowData",
            "OpRecord",
            "PostureRecord",
            "coherence_auditor_enabled",
            "COHERENCE_AUDITOR_SCHEMA_VERSION",
        ):
            assert f'"{name}"' in source, (
                f"public API {name!r} not in __all__"
            )

    def test_stdlib_only_imports(self, source):
        """Final pin: every import must be stdlib (no third-
        party, no backend.*, nothing). Whitelist is exhaustive."""
        stdlib_only = {
            "__future__", "enum", "hashlib", "logging",
            "os", "dataclasses", "typing",
        }
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # Strip submodule path; check root
                root = module.split(".", 1)[0]
                assert root in stdlib_only, (
                    f"non-stdlib import: {module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    assert root in stdlib_only, (
                        f"non-stdlib import: {alias.name}"
                    )
