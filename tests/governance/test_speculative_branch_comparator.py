"""Priority #4 Slice 3 — SBT comparator + aggregator regression suite.

Aggregator over a stream of TreeVerdictResults → SBTComparisonReport
(closed-taxonomy outcome + SBTEffectivenessStats + canonical PASSED
stamp).

Test classes:
  * TestComparatorEnabledFlag — sub-flag asymmetric env semantics
  * TestEnvKnobs — float + int knob clamping
  * TestEffectivenessOutcomeSchema + TestSBTBaselineQualitySchema
  * TestStampedTreeVerdictSchema — frozen + tightening always PASSED
  * TestStatsSchema — frozen + to_dict round-trip
  * TestComputeBaselineQuality — boundary conditions
  * TestComputeSBTEffectivenessStats — counter aggregation
  * TestCompareTreeHistoryMatrix — closed-taxonomy outcome tree:
    DISABLED / FAILED / INSUFFICIENT_DATA / INEFFECTIVE / ESTABLISHED
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

from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchEvidence,
    BranchOutcome,
    BranchResult,
    BranchTreeTarget,
    EvidenceKind,
    TreeVerdict,
    TreeVerdictResult,
)
from backend.core.ouroboros.governance.verification import (
    speculative_branch_comparator as comp_mod,
)
from backend.core.ouroboros.governance.verification.speculative_branch_comparator import (
    EffectivenessOutcome,
    SBTBaselineQuality,
    SBTComparisonReport,
    SBTEffectivenessStats,
    StampedTreeVerdict,
    baseline_high_n_threshold,
    baseline_low_n_threshold,
    baseline_medium_n_threshold,
    comparator_enabled,
    compare_tree_history,
    compose_aggregated_detail,
    compute_baseline_quality,
    compute_sbt_effectiveness_stats,
    ineffective_threshold_pct,
    resolution_threshold_pct,
    stamp_tree_verdict,
)


# ---------------------------------------------------------------------------
# Forbidden-call tokens
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
    outcome: TreeVerdict = TreeVerdict.CONVERGED,
    n_branches: int = 1,
    confidence: float = 0.9,
    aggregate_confidence: float = 0.9,
) -> TreeVerdictResult:
    target = BranchTreeTarget(
        decision_id="d", ambiguity_kind="x",
    )
    branches = tuple(
        BranchResult(
            branch_id=f"b{i}",
            outcome=BranchOutcome.SUCCESS,
            evidence=(
                BranchEvidence(
                    kind=EvidenceKind.FILE_READ,
                    content_hash=f"h{i}",
                    confidence=confidence,
                ),
            ),
            fingerprint=f"fp{i}",
        )
        for i in range(n_branches)
    )
    return TreeVerdictResult(
        outcome=outcome,
        target=target,
        branches=branches,
        winning_branch_idx=0 if outcome is TreeVerdict.CONVERGED else None,
        winning_fingerprint="fp0" if outcome is TreeVerdict.CONVERGED else "",
        aggregate_confidence=aggregate_confidence,
    )


@pytest.fixture(autouse=True)
def _engine_on(monkeypatch):
    monkeypatch.setenv("JARVIS_SBT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SBT_COMPARATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SBT_BASELINE_LOW_N", "1")
    monkeypatch.setenv("JARVIS_SBT_BASELINE_MEDIUM_N", "3")
    monkeypatch.setenv("JARVIS_SBT_BASELINE_HIGH_N", "10")
    monkeypatch.setenv("JARVIS_SBT_RESOLUTION_THRESHOLD_PCT", "50.0")
    monkeypatch.setenv("JARVIS_SBT_INEFFECTIVE_THRESHOLD_PCT", "50.0")
    yield


# ---------------------------------------------------------------------------
# TestComparatorEnabledFlag
# ---------------------------------------------------------------------------


class TestComparatorEnabledFlag:

    def test_default_false(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_COMPARATOR_ENABLED", raising=False)
        assert comparator_enabled() is False

    def test_empty_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_COMPARATOR_ENABLED", "")
        assert comparator_enabled() is False

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_SBT_COMPARATOR_ENABLED", v)
        assert comparator_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off"])
    def test_falsy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_SBT_COMPARATOR_ENABLED", v)
        assert comparator_enabled() is False


# ---------------------------------------------------------------------------
# TestEnvKnobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:

    def test_high_n_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_BASELINE_HIGH_N", raising=False)
        assert baseline_high_n_threshold() == 30

    def test_high_n_clamps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_BASELINE_HIGH_N", "1")
        assert baseline_high_n_threshold() == 10
        monkeypatch.setenv("JARVIS_SBT_BASELINE_HIGH_N", "999999")
        assert baseline_high_n_threshold() == 1000

    def test_high_n_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_BASELINE_HIGH_N", "junk")
        assert baseline_high_n_threshold() == 30

    def test_medium_n_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_BASELINE_MEDIUM_N", raising=False)
        assert baseline_medium_n_threshold() == 10

    def test_low_n_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_BASELINE_LOW_N", raising=False)
        assert baseline_low_n_threshold() == 3

    def test_resolution_threshold_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SBT_RESOLUTION_THRESHOLD_PCT", raising=False,
        )
        assert resolution_threshold_pct() == pytest.approx(50.0)

    def test_resolution_threshold_clamps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_RESOLUTION_THRESHOLD_PCT", "-50")
        assert resolution_threshold_pct() == pytest.approx(0.0)
        monkeypatch.setenv("JARVIS_SBT_RESOLUTION_THRESHOLD_PCT", "9999")
        assert resolution_threshold_pct() == pytest.approx(100.0)

    def test_ineffective_threshold_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SBT_INEFFECTIVE_THRESHOLD_PCT", raising=False,
        )
        assert ineffective_threshold_pct() == pytest.approx(50.0)

    def test_ineffective_threshold_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SBT_INEFFECTIVE_THRESHOLD_PCT", "junk",
        )
        assert ineffective_threshold_pct() == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# TestEffectivenessOutcomeSchema + TestSBTBaselineQualitySchema
# ---------------------------------------------------------------------------


class TestEffectivenessOutcomeSchema:

    def test_5_value_taxonomy(self):
        assert {x.value for x in EffectivenessOutcome} == {
            "established", "insufficient_data", "ineffective",
            "disabled", "failed",
        }


class TestSBTBaselineQualitySchema:

    def test_5_value_taxonomy(self):
        assert {x.value for x in SBTBaselineQuality} == {
            "high", "medium", "low", "insufficient", "failed",
        }


# ---------------------------------------------------------------------------
# TestStampedTreeVerdictSchema
# ---------------------------------------------------------------------------


class TestStampedTreeVerdictSchema:

    def test_construction(self):
        sv = stamp_tree_verdict(_verdict())
        assert isinstance(sv, StampedTreeVerdict)
        assert sv.tightening == "passed"

    def test_stamp_always_passed_regardless_of_outcome(self):
        for outcome in TreeVerdict:
            sv = stamp_tree_verdict(_verdict(outcome=outcome))
            assert sv.tightening == "passed"

    def test_frozen(self):
        sv = stamp_tree_verdict(_verdict())
        with pytest.raises(FrozenInstanceError):
            sv.tightening = "rejected"  # type: ignore

    def test_cluster_kind_optional(self):
        sv = stamp_tree_verdict(_verdict(), cluster_kind="my_cluster")
        assert sv.cluster_kind == "my_cluster"

    def test_to_dict_shape(self):
        sv = stamp_tree_verdict(_verdict())
        d = sv.to_dict()
        assert set(d.keys()) == {
            "verdict", "tightening", "cluster_kind", "schema_version",
        }
        assert d["tightening"] == "passed"

    def test_garbage_input_still_stamped(self):
        sv = stamp_tree_verdict("not a verdict")  # type: ignore
        assert sv.tightening == "passed"


# ---------------------------------------------------------------------------
# TestStatsSchema
# ---------------------------------------------------------------------------


class TestStatsSchema:

    def test_default_construction_zero_quality_insufficient(self):
        s = SBTEffectivenessStats()
        assert s.total_trees == 0
        assert s.baseline_quality is SBTBaselineQuality.INSUFFICIENT

    def test_frozen(self):
        s = SBTEffectivenessStats()
        with pytest.raises(FrozenInstanceError):
            s.total_trees = 5  # type: ignore

    def test_to_dict_round_trip_keys(self):
        s = compute_sbt_effectiveness_stats([_verdict()] * 3)
        d = s.to_dict()
        assert d["total_trees"] == 3
        assert d["converged_count"] == 3
        assert d["baseline_quality"] in (
            "high", "medium", "low", "insufficient", "failed",
        )
        assert d["ambiguity_resolution_rate"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# TestComputeBaselineQuality
# ---------------------------------------------------------------------------


class TestComputeBaselineQuality:

    @pytest.mark.parametrize("n,expected", [
        (0, SBTBaselineQuality.INSUFFICIENT),
        (1, SBTBaselineQuality.LOW),
        (2, SBTBaselineQuality.LOW),
        (3, SBTBaselineQuality.MEDIUM),
        (5, SBTBaselineQuality.MEDIUM),
        (9, SBTBaselineQuality.MEDIUM),
        (10, SBTBaselineQuality.HIGH),
        (100, SBTBaselineQuality.HIGH),
    ])
    def test_boundaries(self, n, expected):
        assert compute_baseline_quality(n) is expected

    def test_negative_treated_as_zero(self):
        assert compute_baseline_quality(-5) is SBTBaselineQuality.INSUFFICIENT

    def test_garbage_returns_failed(self):
        assert compute_baseline_quality("oops") is SBTBaselineQuality.FAILED  # type: ignore

    def test_reversed_thresholds_still_resolves(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_BASELINE_HIGH_N", "30")
        monkeypatch.setenv("JARVIS_SBT_BASELINE_MEDIUM_N", "20")
        monkeypatch.setenv("JARVIS_SBT_BASELINE_LOW_N", "10")
        # 25 → between MEDIUM and HIGH → MEDIUM
        assert compute_baseline_quality(25) is SBTBaselineQuality.MEDIUM


# ---------------------------------------------------------------------------
# TestComputeSBTEffectivenessStats
# ---------------------------------------------------------------------------


class TestComputeSBTEffectivenessStats:

    def test_empty_stream(self):
        s = compute_sbt_effectiveness_stats([])
        assert s.total_trees == 0
        assert s.actionable_count == 0
        assert s.ambiguity_resolution_rate == 0.0
        assert s.baseline_quality is SBTBaselineQuality.INSUFFICIENT

    def test_none_input(self):
        s = compute_sbt_effectiveness_stats(None)  # type: ignore
        assert s.total_trees == 0

    def test_all_converged(self):
        s = compute_sbt_effectiveness_stats([_verdict()] * 5)
        assert s.total_trees == 5
        assert s.converged_count == 5
        assert s.actionable_count == 5
        assert s.ambiguity_resolution_rate == pytest.approx(100.0)
        assert s.escalation_rate == pytest.approx(0.0)

    def test_all_diverged(self):
        s = compute_sbt_effectiveness_stats(
            [_verdict(outcome=TreeVerdict.DIVERGED)] * 5,
        )
        assert s.diverged_count == 5
        assert s.actionable_count == 5
        assert s.escalation_rate == pytest.approx(100.0)
        assert s.ambiguity_resolution_rate == pytest.approx(0.0)

    def test_mixed_actionable(self):
        verdicts = (
            [_verdict()] * 6  # CONVERGED
            + [_verdict(outcome=TreeVerdict.DIVERGED)] * 2
            + [_verdict(outcome=TreeVerdict.INCONCLUSIVE)] * 2
        )
        s = compute_sbt_effectiveness_stats(verdicts)
        assert s.total_trees == 10
        assert s.actionable_count == 10
        assert s.converged_count == 6
        assert s.diverged_count == 2
        assert s.inconclusive_count == 2
        # 6/10 = 60%
        assert s.ambiguity_resolution_rate == pytest.approx(60.0)
        assert s.escalation_rate == pytest.approx(20.0)

    def test_truncated_excluded_from_actionable(self):
        verdicts = (
            [_verdict()] * 3
            + [_verdict(outcome=TreeVerdict.TRUNCATED)] * 2
        )
        s = compute_sbt_effectiveness_stats(verdicts)
        assert s.total_trees == 5
        assert s.actionable_count == 3
        assert s.truncated_count == 2
        # 3/3 actionable = 100%; tf_rate = 2/5 = 40%
        assert s.ambiguity_resolution_rate == pytest.approx(100.0)
        assert s.truncated_failed_rate == pytest.approx(40.0)

    def test_failed_outcomes(self):
        verdicts = (
            [_verdict(outcome=TreeVerdict.FAILED)] * 4
            + [_verdict()]
        )
        s = compute_sbt_effectiveness_stats(verdicts)
        assert s.failed_count == 4
        assert s.truncated_failed_rate == pytest.approx(80.0)

    def test_garbage_items_counted_failed(self):
        verdicts = ["bad", _verdict(), 42, _verdict(), _verdict()]
        s = compute_sbt_effectiveness_stats(verdicts)  # type: ignore
        assert s.total_trees == 5
        assert s.converged_count == 3
        assert s.failed_count == 2

    def test_avg_branches_and_evidence(self):
        v = _verdict(n_branches=3)
        s = compute_sbt_effectiveness_stats([v, v, v])
        assert s.avg_branches_per_tree == pytest.approx(3.0)
        assert s.avg_evidence_per_tree == pytest.approx(3.0)

    def test_avg_aggregate_confidence_only_converged(self):
        verdicts = (
            [_verdict(aggregate_confidence=0.9)] * 2  # CONVERGED with conf
            + [_verdict(outcome=TreeVerdict.DIVERGED, aggregate_confidence=0.0)] * 2
        )
        s = compute_sbt_effectiveness_stats(verdicts)
        # Mean over converged with confidence > 0
        assert s.avg_aggregate_confidence == pytest.approx(0.9)

    def test_generator_input(self):
        def gen():
            for _ in range(3):
                yield _verdict()
        s = compute_sbt_effectiveness_stats(gen())
        assert s.total_trees == 3


# ---------------------------------------------------------------------------
# TestCompareTreeHistoryMatrix
# ---------------------------------------------------------------------------


class TestCompareTreeHistoryMatrix:

    def test_master_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        report = compare_tree_history([_verdict()] * 5)
        assert report.outcome is EffectivenessOutcome.DISABLED

    def test_sub_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_COMPARATOR_ENABLED", "false")
        report = compare_tree_history([_verdict()] * 5)
        assert report.outcome is EffectivenessOutcome.DISABLED

    def test_enabled_override_false(self):
        report = compare_tree_history(
            [_verdict()] * 5, enabled_override=False,
        )
        assert report.outcome is EffectivenessOutcome.DISABLED

    def test_none_failed(self):
        assert compare_tree_history(None).outcome is EffectivenessOutcome.FAILED  # type: ignore

    def test_string_failed(self):
        report = compare_tree_history("not a stream")  # type: ignore
        assert report.outcome is EffectivenessOutcome.FAILED
        assert "string_like_input" in report.detail

    def test_bytes_failed(self):
        assert (
            compare_tree_history(b"\x00").outcome  # type: ignore
            is EffectivenessOutcome.FAILED
        )

    def test_int_failed(self):
        report = compare_tree_history(42)  # type: ignore
        assert report.outcome is EffectivenessOutcome.FAILED
        assert "non_iterable" in report.detail

    def test_empty_insufficient(self):
        report = compare_tree_history([])
        assert report.outcome is EffectivenessOutcome.INSUFFICIENT_DATA
        assert "empty_verdict_stream" in report.detail

    def test_below_low_n_insufficient(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_BASELINE_LOW_N", "5")
        report = compare_tree_history([_verdict()] * 2)
        assert report.outcome is EffectivenessOutcome.INSUFFICIENT_DATA
        assert "baseline_quality=insufficient" in report.detail

    def test_below_resolution_threshold_insufficient(self):
        # 1 CONVERGED + 4 DIVERGED → 20% res rate, below 50% threshold
        verdicts = [_verdict()] + [_verdict(outcome=TreeVerdict.DIVERGED)] * 4
        report = compare_tree_history(verdicts)
        assert report.outcome is EffectivenessOutcome.INSUFFICIENT_DATA
        assert "resolution_below_threshold" in report.detail

    def test_high_resolution_established(self):
        report = compare_tree_history([_verdict()] * 8)
        assert report.outcome is EffectivenessOutcome.ESTABLISHED
        assert report.stats.ambiguity_resolution_rate == pytest.approx(100.0)

    def test_high_truncated_failed_ineffective(self):
        # 4 TRUNCATED + 1 CONVERGED → 80% tf_rate, exceeds 50%
        verdicts = (
            [_verdict(outcome=TreeVerdict.TRUNCATED)] * 4
            + [_verdict()]
        )
        report = compare_tree_history(verdicts)
        assert report.outcome is EffectivenessOutcome.INEFFECTIVE

    def test_ineffective_takes_precedence_over_established(self):
        # 50/50 truncated-vs-converged. ineffective_threshold=50% triggers
        # INEFFECTIVE before ESTABLISHED would (both rates exactly 50%).
        verdicts = (
            [_verdict()] * 5
            + [_verdict(outcome=TreeVerdict.TRUNCATED)] * 5
        )
        report = compare_tree_history(verdicts)
        assert report.outcome is EffectivenessOutcome.INEFFECTIVE

    def test_tightening_stamp_always_passed(self):
        for verdicts in (
            [], [_verdict()], [_verdict()] * 50, None,
        ):
            r = compare_tree_history(verdicts)  # type: ignore
            assert r.tightening == "passed"

    def test_report_to_dict_full_shape(self):
        report = compare_tree_history([_verdict()] * 5)
        d = report.to_dict()
        assert set(d.keys()) == {
            "outcome", "stats", "tightening", "detail",
            "schema_version",
        }
        assert d["outcome"] in {x.value for x in EffectivenessOutcome}
        assert isinstance(d["stats"], dict)
        assert d["tightening"] == "passed"


# ---------------------------------------------------------------------------
# TestComparatorDefensiveContract
# ---------------------------------------------------------------------------


class TestComparatorDefensiveContract:

    def test_compute_stats_never_raises_on_garbage(self):
        s = compute_sbt_effectiveness_stats(
            [object(), object()],  # type: ignore
        )
        assert isinstance(s, SBTEffectivenessStats)

    def test_compose_aggregated_detail_never_raises(self):
        result = compose_aggregated_detail(SBTEffectivenessStats())
        assert isinstance(result, str)

    def test_compose_aggregated_detail_garbage_returns_empty(self):
        result = compose_aggregated_detail("not stats")  # type: ignore
        assert result == ""

    def test_stamp_garbage_safe(self):
        sv = stamp_tree_verdict(None)  # type: ignore
        assert isinstance(sv, StampedTreeVerdict)

    def test_compare_returns_failed_on_iter_raise(self):
        class BadIter:
            def __iter__(self):
                raise RuntimeError("nope")
        report = compare_tree_history(BadIter())
        assert isinstance(report, SBTComparisonReport)


# ---------------------------------------------------------------------------
# TestCostContractAuthorityInvariants
# ---------------------------------------------------------------------------


_COMP_PATH = Path(comp_mod.__file__)


def _module_source() -> str:
    return _COMP_PATH.read_text()


def _module_ast() -> ast.AST:
    return ast.parse(_module_source())


_BANNED_IMPORT_SUBSTRINGS = (
    ".providers", "doubleword_provider", "urgency_router",
    "candidate_generator", "orchestrator", "tool_executor",
    "phase_runner", "iron_gate", "change_engine",
    "auto_action_router", "subagent_scheduler",
    "semantic_guardian", "semantic_firewall", "risk_engine",
)


class TestCostContractAuthorityInvariants:

    def test_no_banned_imports(self):
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in _BANNED_IMPORT_SUBSTRINGS:
                        assert banned not in alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for banned in _BANNED_IMPORT_SUBSTRINGS:
                    assert banned not in module

    def test_no_eval_family_calls(self):
        src = _module_source()
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in ("exec", "eval", "compile")
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in src

    def test_no_subprocess_or_os_system(self):
        src = _module_source()
        assert "subprocess" not in src
        assert "os." + "system" not in src

    def test_no_mutation_calls(self):
        tree = _module_ast()
        forbidden = {("shutil", "rmtree"), ("os", "remove"),
                     ("os", "unlink")}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Attribute,
            ):
                if isinstance(node.func.value, ast.Name):
                    pair = (node.func.value.id, node.func.attr)
                    assert pair not in forbidden

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
        """Positive invariant — proves zero duplication."""
        src = _module_source()
        assert "from backend.core.ouroboros.governance.verification.speculative_branch import" in src
        assert "TreeVerdict" in src
        assert "TreeVerdictResult" in src

    def test_canonical_passed_resolution(self):
        """Slice 3 must resolve PASSED via adaptation.ledger so
        operators correlate via shared vocabulary."""
        src = _module_source()
        assert "MonotonicTighteningVerdict" in src
        assert "adaptation.ledger" in src
