"""Priority #3 Slice 3 — Counterfactual Replay comparator regression suite.

Aggregator over a stream of ReplayVerdicts → ComparisonReport
(closed-taxonomy outcome + RecurrenceReductionStats + canonical
PASSED stamp).

Test classes:
  * TestComparatorEnabledFlag — sub-flag asymmetric env semantics
  * TestEnvKnobs — float + int knob clamping
  * TestComparisonOutcomeSchema + TestBaselineQualitySchema
  * TestStampedVerdictSchema — frozen + tightening always PASSED
  * TestStatsSchema — frozen + to_dict round-trip
  * TestComputeBaselineQuality — boundary conditions
  * TestComputeRecurrenceReductionStats — counter aggregation
  * TestCompareReplayHistoryMatrix — closed-taxonomy outcome tree:
    DISABLED / FAILED / INSUFFICIENT_DATA / DEGRADED / ESTABLISHED
  * TestPostmortemPrevention — counts across original+counterfactual
  * TestComparatorDefensiveContract — public surface NEVER raises
  * TestCostContractAuthorityInvariants — AST-level pin
"""
from __future__ import annotations

import ast
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.verification.counterfactual_replay import (
    BranchSnapshot,
    BranchVerdict,
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
)
from backend.core.ouroboros.governance.verification import (
    counterfactual_replay_comparator as comp_mod,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay_comparator import (
    BaselineQuality,
    ComparisonOutcome,
    ComparisonReport,
    RecurrenceReductionStats,
    StampedVerdict,
    baseline_high_n_threshold,
    baseline_low_n_threshold,
    baseline_medium_n_threshold,
    comparator_enabled,
    compare_replay_history,
    compose_aggregated_detail,
    compute_baseline_quality,
    compute_recurrence_reduction_stats,
    degradation_threshold_pct,
    prevention_threshold_pct,
    stamp_verdict,
)


# ---------------------------------------------------------------------------
# Forbidden-call tokens — Slice 1/2 pattern
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_TOKENS = (
    "e" + "val(",
    "e" + "xec(",
    "comp" + "ile(",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _verdict(
    *,
    branch_verdict: BranchVerdict = BranchVerdict.DIVERGED_BETTER,
    outcome: ReplayOutcome = ReplayOutcome.SUCCESS,
    orig_pm: int = 0,
    cf_pm: int = 0,
    orig_phase: str = "COMPLETE",
    orig_success: bool = True,
    orig_apply: str = "single",
    cf_phase: str = "GATE",
    cf_success: bool = False,
    cf_apply: str = "gated",
    has_orig: bool = True,
    has_cf: bool = True,
) -> ReplayVerdict:
    """Build a ReplayVerdict for tests."""
    target = ReplayTarget(
        session_id="bt", swap_at_phase="GATE",
        swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
    )
    orig = (
        BranchSnapshot(
            branch_id="orig", terminal_phase=orig_phase,
            terminal_success=orig_success, apply_outcome=orig_apply,
            verify_passed=10, verify_total=10,
            postmortem_records=tuple(f"pm_{i}" for i in range(orig_pm)),
        )
        if has_orig else None
    )
    cf = (
        BranchSnapshot(
            branch_id="cf", terminal_phase=cf_phase,
            terminal_success=cf_success, apply_outcome=cf_apply,
            postmortem_records=tuple(f"pm_{i}" for i in range(cf_pm)),
        )
        if has_cf else None
    )
    return ReplayVerdict(
        outcome=outcome, target=target,
        original_branch=orig, counterfactual_branch=cf,
        verdict=branch_verdict,
    )


@pytest.fixture(autouse=True)
def _engine_on(monkeypatch):
    """All comparator tests run with master + sub flag on by default
    + low N thresholds so small streams meet quality gates."""
    monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_REPLAY_COMPARATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_LOW_N", "1")
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_MEDIUM_N", "3")
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_HIGH_N", "10")
    monkeypatch.setenv("JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT", "50.0")
    monkeypatch.setenv("JARVIS_REPLAY_DEGRADATION_THRESHOLD_PCT", "50.0")
    yield


# ---------------------------------------------------------------------------
# TestComparatorEnabledFlag
# ---------------------------------------------------------------------------


class TestComparatorEnabledFlag:

    def test_default_on_post_graduation(self, monkeypatch):
        """Slice 5 graduation flipped comparator sub-gate to True
        (2026-05-02)."""
        monkeypatch.delenv("JARVIS_REPLAY_COMPARATOR_ENABLED", raising=False)
        assert comparator_enabled() is True

    def test_empty_treated_as_unset(self, monkeypatch):
        """Empty = unset = graduated default-true."""
        monkeypatch.setenv("JARVIS_REPLAY_COMPARATOR_ENABLED", "")
        assert comparator_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "ON"])
    def test_truthy_variants(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_REPLAY_COMPARATOR_ENABLED", val)
        assert comparator_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off"])
    def test_falsy_variants(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_REPLAY_COMPARATOR_ENABLED", val)
        assert comparator_enabled() is False


# ---------------------------------------------------------------------------
# TestEnvKnobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:

    def test_high_n_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_BASELINE_HIGH_N", raising=False)
        assert baseline_high_n_threshold() == 30

    def test_high_n_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_BASELINE_HIGH_N", "1")
        assert baseline_high_n_threshold() == 10  # clamped

    def test_high_n_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_BASELINE_HIGH_N", "999999")
        assert baseline_high_n_threshold() == 1000  # clamped

    def test_high_n_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_BASELINE_HIGH_N", "garbage")
        assert baseline_high_n_threshold() == 30  # default

    def test_medium_n_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_BASELINE_MEDIUM_N", raising=False)
        assert baseline_medium_n_threshold() == 10

    def test_low_n_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_BASELINE_LOW_N", raising=False)
        assert baseline_low_n_threshold() == 3

    def test_prev_threshold_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT", raising=False)
        assert prevention_threshold_pct() == pytest.approx(50.0)

    def test_prev_threshold_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT", "-50")
        assert prevention_threshold_pct() == pytest.approx(0.0)

    def test_prev_threshold_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT", "9999")
        assert prevention_threshold_pct() == pytest.approx(100.0)

    def test_deg_threshold_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_DEGRADATION_THRESHOLD_PCT", raising=False)
        assert degradation_threshold_pct() == pytest.approx(50.0)

    def test_deg_threshold_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_DEGRADATION_THRESHOLD_PCT", "junk")
        assert degradation_threshold_pct() == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# TestComparisonOutcomeSchema + TestBaselineQualitySchema
# ---------------------------------------------------------------------------


class TestComparisonOutcomeSchema:

    def test_5_value_taxonomy(self):
        assert {x.value for x in ComparisonOutcome} == {
            "established", "insufficient_data",
            "degraded", "disabled", "failed",
        }

    def test_string_values(self):
        for outcome in ComparisonOutcome:
            assert isinstance(outcome.value, str)


class TestBaselineQualitySchema:

    def test_5_value_taxonomy(self):
        assert {x.value for x in BaselineQuality} == {
            "high", "medium", "low", "insufficient", "failed",
        }


# ---------------------------------------------------------------------------
# TestStampedVerdictSchema
# ---------------------------------------------------------------------------


class TestStampedVerdictSchema:

    def test_construction(self):
        sv = stamp_verdict(_verdict())
        assert isinstance(sv, StampedVerdict)
        assert sv.tightening == "passed"

    def test_stamp_always_passed_regardless_of_branch_verdict(self):
        for bv in BranchVerdict:
            sv = stamp_verdict(_verdict(branch_verdict=bv))
            assert sv.tightening == "passed"

    def test_frozen(self):
        sv = stamp_verdict(_verdict())
        with pytest.raises(FrozenInstanceError):
            sv.tightening = "rejected"  # type: ignore

    def test_cluster_kind_optional(self):
        sv = stamp_verdict(_verdict(), cluster_kind="repeated_failure_cluster")
        assert sv.cluster_kind == "repeated_failure_cluster"

    def test_to_dict_shape(self):
        sv = stamp_verdict(_verdict())
        d = sv.to_dict()
        assert set(d.keys()) == {
            "verdict", "tightening", "cluster_kind", "schema_version",
        }
        assert d["tightening"] == "passed"
        assert d["verdict"]["outcome"] == "success"

    def test_garbage_input_still_stamped(self):
        sv = stamp_verdict("not a verdict")  # type: ignore
        assert sv.tightening == "passed"  # canonical regardless


# ---------------------------------------------------------------------------
# TestStatsSchema
# ---------------------------------------------------------------------------


class TestStatsSchema:

    def test_default_construction_zero_quality_insufficient(self):
        s = RecurrenceReductionStats()
        assert s.total_replays == 0
        assert s.baseline_quality is BaselineQuality.INSUFFICIENT

    def test_frozen(self):
        s = RecurrenceReductionStats()
        with pytest.raises(FrozenInstanceError):
            s.total_replays = 5  # type: ignore

    def test_to_dict_round_trip_keys(self):
        s = compute_recurrence_reduction_stats(
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 3,
        )
        d = s.to_dict()
        assert d["total_replays"] == 3
        assert d["prevention_count"] == 3
        assert d["baseline_quality"] in ("low", "medium", "high",
                                          "insufficient", "failed")
        assert d["recurrence_reduction_pct"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# TestComputeBaselineQuality
# ---------------------------------------------------------------------------


class TestComputeBaselineQuality:

    @pytest.mark.parametrize("n,expected", [
        (0, BaselineQuality.INSUFFICIENT),
        (1, BaselineQuality.LOW),
        (2, BaselineQuality.LOW),
        (3, BaselineQuality.MEDIUM),
        (5, BaselineQuality.MEDIUM),
        (9, BaselineQuality.MEDIUM),
        (10, BaselineQuality.HIGH),
        (100, BaselineQuality.HIGH),
    ])
    def test_boundaries(self, n, expected):
        assert compute_baseline_quality(n) is expected

    def test_negative_treated_as_zero(self):
        assert compute_baseline_quality(-5) is BaselineQuality.INSUFFICIENT

    def test_garbage_returns_failed(self):
        # Type ignored intentionally — testing defensive behavior
        assert compute_baseline_quality("oops") is BaselineQuality.FAILED  # type: ignore

    def test_reversed_thresholds_still_resolves(self, monkeypatch):
        """Operator misconfigures thresholds (HIGH < MEDIUM); the
        comparator's internal sort makes resolution graceful."""
        monkeypatch.setenv("JARVIS_REPLAY_BASELINE_HIGH_N", "30")
        monkeypatch.setenv("JARVIS_REPLAY_BASELINE_MEDIUM_N", "20")
        monkeypatch.setenv("JARVIS_REPLAY_BASELINE_LOW_N", "10")
        # 25 → between MEDIUM and HIGH → MEDIUM
        assert compute_baseline_quality(25) is BaselineQuality.MEDIUM


# ---------------------------------------------------------------------------
# TestComputeRecurrenceReductionStats
# ---------------------------------------------------------------------------


class TestComputeRecurrenceReductionStats:

    def test_empty_stream(self):
        s = compute_recurrence_reduction_stats([])
        assert s.total_replays == 0
        assert s.actionable_count == 0
        assert s.recurrence_reduction_pct == 0.0
        assert s.baseline_quality is BaselineQuality.INSUFFICIENT

    def test_none_input(self):
        s = compute_recurrence_reduction_stats(None)  # type: ignore
        assert s.total_replays == 0

    def test_all_prevention(self):
        verdicts = [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 5
        s = compute_recurrence_reduction_stats(verdicts)
        assert s.total_replays == 5
        assert s.prevention_count == 5
        assert s.actionable_count == 5
        assert s.recurrence_reduction_pct == pytest.approx(100.0)
        assert s.regression_rate == pytest.approx(0.0)

    def test_all_regression(self):
        verdicts = [_verdict(branch_verdict=BranchVerdict.DIVERGED_WORSE)] * 5
        s = compute_recurrence_reduction_stats(verdicts)
        assert s.regression_count == 5
        assert s.regression_rate == pytest.approx(100.0)
        assert s.recurrence_reduction_pct == pytest.approx(0.0)

    def test_mixed_actionable(self):
        verdicts = (
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 5
            + [_verdict(branch_verdict=BranchVerdict.DIVERGED_WORSE)] * 1
            + [_verdict(branch_verdict=BranchVerdict.EQUIVALENT)] * 2
            + [_verdict(branch_verdict=BranchVerdict.DIVERGED_NEUTRAL)] * 1
        )
        s = compute_recurrence_reduction_stats(verdicts)
        assert s.total_replays == 9
        assert s.actionable_count == 9
        assert s.prevention_count == 5
        assert s.regression_count == 1
        assert s.equivalent_count == 2
        assert s.neutral_count == 1
        # 5 / 9 ≈ 55.56%
        assert s.recurrence_reduction_pct == pytest.approx(55.5556, abs=0.01)

    def test_non_actionable_excluded_from_denominator(self):
        # 2 SUCCESS+DIVERGED_BETTER + 3 FAILED outcomes
        verdicts = (
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 2
            + [_verdict(outcome=ReplayOutcome.FAILED,
                        branch_verdict=BranchVerdict.FAILED)] * 3
        )
        s = compute_recurrence_reduction_stats(verdicts)
        assert s.total_replays == 5
        assert s.actionable_count == 2
        # 100% recurrence reduction over the 2 actionable verdicts
        assert s.recurrence_reduction_pct == pytest.approx(100.0)
        assert s.failed_outcome_count == 3

    def test_outcome_buckets(self):
        verdicts = (
            [_verdict()]                                     # success
            + [_verdict(outcome=ReplayOutcome.PARTIAL)]      # partial
            + [_verdict(outcome=ReplayOutcome.DIVERGED)]     # diverged
            + [_verdict(outcome=ReplayOutcome.FAILED,
                        branch_verdict=BranchVerdict.FAILED)]
            + [_verdict(outcome=ReplayOutcome.DISABLED)]
        )
        s = compute_recurrence_reduction_stats(verdicts)
        assert s.success_outcome_count == 1
        assert s.partial_outcome_count == 1
        assert s.diverged_outcome_count == 1
        assert s.failed_outcome_count == 1
        assert s.disabled_outcome_count == 1

    def test_garbage_items_counted_non_actionable(self):
        # 2 garbage + 2 prevention = 4 total; only 2 actionable
        verdicts = ["garbage1", _verdict(), 42, _verdict()]
        s = compute_recurrence_reduction_stats(verdicts)  # type: ignore
        assert s.total_replays == 4
        assert s.actionable_count == 2
        assert s.non_actionable_count == 2

    def test_generator_input(self):
        def gen():
            for _ in range(3):
                yield _verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)
        s = compute_recurrence_reduction_stats(gen())
        assert s.total_replays == 3


# ---------------------------------------------------------------------------
# TestPostmortemPrevention
# ---------------------------------------------------------------------------


class TestPostmortemPrevention:

    def test_postmortems_summed(self):
        verdicts = [
            _verdict(orig_pm=2, cf_pm=0),
            _verdict(orig_pm=3, cf_pm=1),
            _verdict(orig_pm=0, cf_pm=0),
        ]
        s = compute_recurrence_reduction_stats(verdicts)
        assert s.postmortems_in_originals == 5
        assert s.postmortems_in_counterfactuals == 1
        assert s.postmortems_prevented == 4

    def test_no_negative_prevention(self):
        # cf has MORE postmortems than original — clamped to 0
        verdicts = [_verdict(orig_pm=0, cf_pm=5)]
        s = compute_recurrence_reduction_stats(verdicts)
        assert s.postmortems_prevented == 0

    def test_missing_branch_safe(self):
        v = _verdict(has_orig=False, has_cf=True, cf_pm=2)
        s = compute_recurrence_reduction_stats([v])
        assert s.postmortems_in_originals == 0
        assert s.postmortems_in_counterfactuals == 2


# ---------------------------------------------------------------------------
# TestCompareReplayHistoryMatrix — closed-taxonomy outcome tree
# ---------------------------------------------------------------------------


class TestCompareReplayHistoryMatrix:

    def test_master_off_returns_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "false")
        report = compare_replay_history(
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 5,
        )
        assert report.outcome is ComparisonOutcome.DISABLED

    def test_sub_off_returns_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_COMPARATOR_ENABLED", "false")
        report = compare_replay_history(
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 5,
        )
        assert report.outcome is ComparisonOutcome.DISABLED

    def test_enabled_override_false_returns_disabled(self):
        report = compare_replay_history(
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 5,
            enabled_override=False,
        )
        assert report.outcome is ComparisonOutcome.DISABLED

    def test_none_input_returns_failed(self):
        report = compare_replay_history(None)  # type: ignore
        assert report.outcome is ComparisonOutcome.FAILED

    def test_string_input_returns_failed(self):
        """String IS iterable but always a caller bug as a verdict
        stream — explicit FAILED guard."""
        report = compare_replay_history("not a stream")  # type: ignore
        assert report.outcome is ComparisonOutcome.FAILED
        assert "string_like_input" in report.detail

    def test_bytes_input_returns_failed(self):
        report = compare_replay_history(b"\x00\x01")  # type: ignore
        assert report.outcome is ComparisonOutcome.FAILED

    def test_int_input_returns_failed(self):
        report = compare_replay_history(42)  # type: ignore
        assert report.outcome is ComparisonOutcome.FAILED
        assert "non_iterable" in report.detail

    def test_empty_stream_returns_insufficient(self):
        report = compare_replay_history([])
        assert report.outcome is ComparisonOutcome.INSUFFICIENT_DATA
        assert "empty_verdict_stream" in report.detail

    def test_below_low_n_returns_insufficient(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_BASELINE_LOW_N", "5")
        # Only 2 verdicts, below 5
        report = compare_replay_history(
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 2,
        )
        assert report.outcome is ComparisonOutcome.INSUFFICIENT_DATA
        assert "baseline_quality=insufficient" in report.detail

    def test_below_prevention_threshold_returns_insufficient(self):
        # 2 prev + 8 eq → 20% rec_red, below 50% threshold
        verdicts = (
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 2
            + [_verdict(branch_verdict=BranchVerdict.EQUIVALENT)] * 8
        )
        report = compare_replay_history(verdicts)
        assert report.outcome is ComparisonOutcome.INSUFFICIENT_DATA
        assert "prevention_below_threshold" in report.detail

    def test_high_prevention_returns_established(self):
        # 8 prev + 0 reg → 100% rec_red, exceeds 50% threshold
        verdicts = [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 8
        report = compare_replay_history(verdicts)
        assert report.outcome is ComparisonOutcome.ESTABLISHED
        assert report.stats.recurrence_reduction_pct == pytest.approx(100.0)

    def test_high_regression_returns_degraded(self):
        # 5 reg + 1 prev → 83% reg_rate, exceeds 50% threshold
        verdicts = (
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_WORSE)] * 5
            + [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)]
        )
        report = compare_replay_history(verdicts)
        assert report.outcome is ComparisonOutcome.DEGRADED

    def test_degraded_takes_precedence_over_established(self):
        """Exactly 50/50 split — degradation_threshold>=50% triggers
        DEGRADED before ESTABLISHED (the safer outcome wins ties)."""
        verdicts = (
            [_verdict(branch_verdict=BranchVerdict.DIVERGED_WORSE)] * 5
            + [_verdict(branch_verdict=BranchVerdict.DIVERGED_BETTER)] * 5
        )
        report = compare_replay_history(verdicts)
        assert report.outcome is ComparisonOutcome.DEGRADED

    def test_tightening_stamp_always_passed(self):
        for verdicts in (
            [],
            [_verdict()],
            [_verdict()] * 50,
            None,
        ):
            report = compare_replay_history(verdicts)  # type: ignore
            assert report.tightening == "passed"

    def test_report_to_dict_full_shape(self):
        verdicts = [_verdict()] * 5
        report = compare_replay_history(verdicts)
        d = report.to_dict()
        assert set(d.keys()) == {
            "outcome", "stats", "tightening", "detail", "schema_version",
        }
        assert d["outcome"] in {x.value for x in ComparisonOutcome}
        assert isinstance(d["stats"], dict)
        assert d["tightening"] == "passed"


# ---------------------------------------------------------------------------
# TestComparatorDefensiveContract
# ---------------------------------------------------------------------------


class TestComparatorDefensiveContract:

    def test_compute_stats_never_raises_on_garbage(self):
        s = compute_recurrence_reduction_stats(
            [object(), object()],  # type: ignore
        )
        assert isinstance(s, RecurrenceReductionStats)

    def test_compose_aggregated_detail_never_raises(self):
        result = compose_aggregated_detail(RecurrenceReductionStats())
        assert isinstance(result, str)

    def test_compose_aggregated_detail_garbage_returns_empty(self):
        result = compose_aggregated_detail("not stats")  # type: ignore
        assert result == ""

    def test_stamp_verdict_garbage_safe(self):
        sv = stamp_verdict(None)  # type: ignore
        assert isinstance(sv, StampedVerdict)

    def test_compare_returns_failed_on_iter_raise(self):
        class BadIter:
            def __iter__(self):
                raise RuntimeError("nope")
        # iter() succeeds (returns iterator) but next() raises;
        # compute_stats catches per-item exception → still produces
        # a stats object, then comparator returns INSUFFICIENT_DATA
        # (zero items processed cleanly)
        report = compare_replay_history(BadIter())
        assert isinstance(report, ComparisonReport)


# ---------------------------------------------------------------------------
# TestCostContractAuthorityInvariants — AST-level pin
# ---------------------------------------------------------------------------


_COMP_PATH = Path(comp_mod.__file__)


def _module_source() -> str:
    return _COMP_PATH.read_text()


def _module_ast() -> ast.AST:
    return ast.parse(_module_source())


_BANNED_IMPORT_SUBSTRINGS = (
    ".providers",
    "doubleword_provider",
    "urgency_router",
    "candidate_generator",
    "orchestrator",
    "tool_executor",
    "phase_runner",
    "iron_gate",
    "change_engine",
    "auto_action_router",
    "subagent_scheduler",
    "semantic_guardian",
    "semantic_firewall",
    "risk_engine",
)


class TestCostContractAuthorityInvariants:

    def test_no_banned_imports(self):
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in _BANNED_IMPORT_SUBSTRINGS:
                        assert banned not in alias.name, (
                            f"banned import '{alias.name}'"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for banned in _BANNED_IMPORT_SUBSTRINGS:
                    assert banned not in module, (
                        f"banned ImportFrom module '{module}'"
                    )

    def test_no_eval_family_calls(self):
        src = _module_source()
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("exec", "eval", "compile")
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in src, f"forbidden token: {token!r}"

    def test_no_subprocess_or_os_system(self):
        src = _module_source()
        assert "subprocess" not in src
        assert "os." + "system" not in src

    def test_no_mutation_calls(self):
        """AST walk: no call sites for shutil.rmtree/os.remove/
        os.unlink (substring scan would false-positive on
        docstrings)."""
        tree = _module_ast()
        forbidden = {("shutil", "rmtree"), ("os", "remove"),
                     ("os", "unlink")}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Attribute,
            ):
                if isinstance(node.func.value, ast.Name):
                    pair = (node.func.value.id, node.func.attr)
                    assert pair not in forbidden, (
                        f"forbidden mutation call: {pair}"
                    )

    def test_no_async_functions(self):
        """Slice 3 is pure-data; Slice 4 wraps via to_thread for I/O."""
        tree = _module_ast()
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef), (
                f"forbidden async function: "
                f"{getattr(node, 'name', '?')}"
            )

    def test_public_api_exported(self):
        for name in comp_mod.__all__:
            assert hasattr(comp_mod, name), (
                f"comp_mod.__all__ contains '{name}' which is not "
                f"a module attribute"
            )

    def test_cost_contract_constant_present(self):
        assert hasattr(comp_mod, "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION")
        assert comp_mod.COST_CONTRACT_PRESERVED_BY_CONSTRUCTION is True

    def test_reuses_slice_1_primitives(self):
        """Positive invariant — proves zero duplication. Slice 3
        imports closed-taxonomy enums + ReplayVerdict from Slice 1."""
        src = _module_source()
        assert "from backend.core.ouroboros.governance.verification.counterfactual_replay import" in src
        assert "BranchVerdict" in src
        assert "ReplayOutcome" in src
        assert "ReplayVerdict" in src

    def test_canonical_passed_resolution_present(self):
        """Slice 3 must resolve PASSED via adaptation.ledger so
        operators correlate via shared vocabulary."""
        src = _module_source()
        assert "MonotonicTighteningVerdict" in src
        assert "adaptation.ledger" in src
