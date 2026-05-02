"""Priority #5 Slice 3 — CIGW comparator + aggregator regression suite.

Aggregator over a stream of GradientReports → CIGWComparisonReport
(closed-taxonomy outcome + CIGWAggregateStats + canonical PASSED
stamp).

Test classes:
  * TestComparatorEnabledFlag — sub-flag asymmetric env semantics
  * TestEnvKnobs — float + int knob clamping
  * TestCIGWEffectivenessOutcomeSchema + TestCIGWBaselineQualitySchema
  * TestStampedGradientReportSchema — frozen + tightening always PASSED
  * TestStatsSchema — frozen + to_dict round-trip
  * TestComputeBaselineQuality — boundary conditions
  * TestComputeCIGWAggregateStats — counter aggregation
  * TestCompareGradientHistoryMatrix — closed-taxonomy outcome tree
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

from backend.core.ouroboros.governance.verification.gradient_watcher import (
    GradientBreach,
    GradientOutcome,
    GradientReading,
    GradientReport,
    GradientSeverity,
    MeasurementKind,
)
from backend.core.ouroboros.governance.verification import (
    gradient_comparator as comp_mod,
)
from backend.core.ouroboros.governance.verification.gradient_comparator import (
    CIGWAggregateStats,
    CIGWBaselineQuality,
    CIGWComparisonReport,
    CIGWEffectivenessOutcome,
    StampedGradientReport,
    baseline_high_n_threshold,
    baseline_low_n_threshold,
    baseline_medium_n_threshold,
    comparator_enabled,
    compare_gradient_history,
    compose_aggregated_detail,
    compute_baseline_quality,
    compute_cigw_aggregate_stats,
    degraded_threshold_pct,
    healthy_threshold_pct,
    stamp_gradient_report,
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


def _reading(
    severity: GradientSeverity = GradientSeverity.NONE,
    kind: MeasurementKind = MeasurementKind.LINE_COUNT,
    target: str = "f.py",
) -> GradientReading:
    return GradientReading(
        target_id=target,
        measurement_kind=kind,
        baseline_mean=100.0,
        current_value=100.0,
        delta_abs=0.0,
        delta_pct=0.0,
        severity=severity,
    )


def _report(
    outcome: GradientOutcome = GradientOutcome.STABLE,
    n_readings: int = 1,
    severity: GradientSeverity = GradientSeverity.NONE,
    n_breaches: int = 0,
) -> GradientReport:
    readings = tuple(
        _reading(severity=severity, target=f"f{i}.py")
        for i in range(n_readings)
    )
    breaches = tuple(
        GradientBreach(
            reading=_reading(
                severity=GradientSeverity.CRITICAL, target=f"b{i}.py",
            ),
            detail=f"breach_{i}",
        )
        for i in range(n_breaches)
    )
    return GradientReport(
        outcome=outcome,
        readings=readings,
        breaches=breaches,
        total_samples=n_readings,
    )


@pytest.fixture(autouse=True)
def _engine_on(monkeypatch):
    monkeypatch.setenv("JARVIS_CIGW_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CIGW_COMPARATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CIGW_BASELINE_LOW_N", "1")
    monkeypatch.setenv("JARVIS_CIGW_BASELINE_MEDIUM_N", "3")
    monkeypatch.setenv("JARVIS_CIGW_BASELINE_HIGH_N", "10")
    monkeypatch.setenv("JARVIS_CIGW_HEALTHY_THRESHOLD_PCT", "80.0")
    monkeypatch.setenv("JARVIS_CIGW_DEGRADED_THRESHOLD_PCT", "30.0")
    yield


# ---------------------------------------------------------------------------
# TestComparatorEnabledFlag
# ---------------------------------------------------------------------------


class TestComparatorEnabledFlag:

    def test_default_false(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_COMPARATOR_ENABLED", raising=False)
        assert comparator_enabled() is False

    def test_empty_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_COMPARATOR_ENABLED", "")
        assert comparator_enabled() is False

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_CIGW_COMPARATOR_ENABLED", v)
        assert comparator_enabled() is True


# ---------------------------------------------------------------------------
# TestEnvKnobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:

    def test_high_n_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_BASELINE_HIGH_N", raising=False)
        assert baseline_high_n_threshold() == 30

    def test_high_n_clamps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_BASELINE_HIGH_N", "1")
        assert baseline_high_n_threshold() == 10
        monkeypatch.setenv("JARVIS_CIGW_BASELINE_HIGH_N", "999999")
        assert baseline_high_n_threshold() == 1000

    def test_medium_n_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_BASELINE_MEDIUM_N", raising=False)
        assert baseline_medium_n_threshold() == 10

    def test_low_n_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CIGW_BASELINE_LOW_N", raising=False)
        assert baseline_low_n_threshold() == 3

    def test_healthy_threshold_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CIGW_HEALTHY_THRESHOLD_PCT", raising=False,
        )
        assert healthy_threshold_pct() == pytest.approx(80.0)

    def test_healthy_threshold_clamps(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_HEALTHY_THRESHOLD_PCT", "-50")
        assert healthy_threshold_pct() == pytest.approx(0.0)
        monkeypatch.setenv("JARVIS_CIGW_HEALTHY_THRESHOLD_PCT", "9999")
        assert healthy_threshold_pct() == pytest.approx(100.0)

    def test_degraded_threshold_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CIGW_DEGRADED_THRESHOLD_PCT", raising=False,
        )
        assert degraded_threshold_pct() == pytest.approx(30.0)

    def test_degraded_threshold_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CIGW_DEGRADED_THRESHOLD_PCT", "junk",
        )
        assert degraded_threshold_pct() == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# TestCIGWEffectivenessOutcomeSchema
# ---------------------------------------------------------------------------


class TestCIGWEffectivenessOutcomeSchema:

    def test_5_value_taxonomy(self):
        assert {x.value for x in CIGWEffectivenessOutcome} == {
            "healthy", "insufficient_data", "degraded",
            "disabled", "failed",
        }


class TestCIGWBaselineQualitySchema:

    def test_5_value_taxonomy(self):
        assert {x.value for x in CIGWBaselineQuality} == {
            "high", "medium", "low", "insufficient", "failed",
        }


# ---------------------------------------------------------------------------
# TestStampedGradientReportSchema
# ---------------------------------------------------------------------------


class TestStampedGradientReportSchema:

    def test_construction(self):
        sv = stamp_gradient_report(_report())
        assert isinstance(sv, StampedGradientReport)
        assert sv.tightening == "passed"

    def test_stamp_always_passed_regardless_of_outcome(self):
        for outcome in GradientOutcome:
            sv = stamp_gradient_report(_report(outcome=outcome))
            assert sv.tightening == "passed"

    def test_frozen(self):
        sv = stamp_gradient_report(_report())
        with pytest.raises(FrozenInstanceError):
            sv.tightening = "rejected"  # type: ignore

    def test_cluster_kind_optional(self):
        sv = stamp_gradient_report(_report(), cluster_kind="my_cluster")
        assert sv.cluster_kind == "my_cluster"

    def test_to_dict_shape(self):
        sv = stamp_gradient_report(_report())
        d = sv.to_dict()
        assert set(d.keys()) == {
            "report", "tightening", "cluster_kind", "schema_version",
        }

    def test_garbage_input_still_stamped(self):
        sv = stamp_gradient_report("not a report")  # type: ignore
        assert sv.tightening == "passed"


# ---------------------------------------------------------------------------
# TestStatsSchema
# ---------------------------------------------------------------------------


class TestStatsSchema:

    def test_default_construction(self):
        s = CIGWAggregateStats()
        assert s.total_reports == 0
        assert s.baseline_quality is CIGWBaselineQuality.INSUFFICIENT
        assert isinstance(s.severity_counts, dict)
        assert isinstance(s.kind_drift_counts, dict)

    def test_frozen(self):
        s = CIGWAggregateStats()
        with pytest.raises(FrozenInstanceError):
            s.total_reports = 5  # type: ignore

    def test_to_dict_round_trip_keys(self):
        s = compute_cigw_aggregate_stats([_report()] * 3)
        d = s.to_dict()
        assert d["total_reports"] == 3
        assert d["stable_count"] == 3
        assert d["baseline_quality"] in (
            "high", "medium", "low", "insufficient", "failed",
        )


# ---------------------------------------------------------------------------
# TestComputeBaselineQuality
# ---------------------------------------------------------------------------


class TestComputeBaselineQuality:

    @pytest.mark.parametrize("n,expected", [
        (0, CIGWBaselineQuality.INSUFFICIENT),
        (1, CIGWBaselineQuality.LOW),
        (2, CIGWBaselineQuality.LOW),
        (3, CIGWBaselineQuality.MEDIUM),
        (5, CIGWBaselineQuality.MEDIUM),
        (9, CIGWBaselineQuality.MEDIUM),
        (10, CIGWBaselineQuality.HIGH),
        (100, CIGWBaselineQuality.HIGH),
    ])
    def test_boundaries(self, n, expected):
        assert compute_baseline_quality(n) is expected

    def test_negative_treated_as_zero(self):
        assert compute_baseline_quality(-5) is CIGWBaselineQuality.INSUFFICIENT

    def test_garbage_returns_failed(self):
        assert compute_baseline_quality("oops") is CIGWBaselineQuality.FAILED  # type: ignore


# ---------------------------------------------------------------------------
# TestComputeCIGWAggregateStats
# ---------------------------------------------------------------------------


class TestComputeCIGWAggregateStats:

    def test_empty_stream(self):
        s = compute_cigw_aggregate_stats([])
        assert s.total_reports == 0
        assert s.actionable_count == 0
        assert s.stable_rate == 0.0
        assert s.baseline_quality is CIGWBaselineQuality.INSUFFICIENT

    def test_none_input(self):
        s = compute_cigw_aggregate_stats(None)  # type: ignore
        assert s.total_reports == 0

    def test_all_stable(self):
        s = compute_cigw_aggregate_stats([_report()] * 5)
        assert s.stable_count == 5
        assert s.actionable_count == 5
        assert s.stable_rate == pytest.approx(100.0)
        assert s.drift_rate == pytest.approx(0.0)

    def test_all_drifting(self):
        s = compute_cigw_aggregate_stats(
            [_report(outcome=GradientOutcome.DRIFTING,
                     severity=GradientSeverity.LOW)] * 5,
        )
        assert s.drifting_count == 5
        assert s.actionable_count == 5
        assert s.drift_rate == pytest.approx(100.0)

    def test_all_breached(self):
        s = compute_cigw_aggregate_stats(
            [_report(outcome=GradientOutcome.BREACHED,
                     severity=GradientSeverity.HIGH, n_breaches=1)] * 3,
        )
        assert s.breached_count == 3
        assert s.breach_rate == pytest.approx(100.0)

    def test_mixed_actionable(self):
        reports = (
            [_report()] * 6
            + [_report(outcome=GradientOutcome.DRIFTING,
                       severity=GradientSeverity.LOW)] * 2
            + [_report(outcome=GradientOutcome.BREACHED,
                       severity=GradientSeverity.HIGH, n_breaches=1)] * 2
        )
        s = compute_cigw_aggregate_stats(reports)
        assert s.total_reports == 10
        assert s.actionable_count == 10
        assert s.stable_count == 6
        assert s.drifting_count == 2
        assert s.breached_count == 2
        # stable_rate = 6/10 = 60%
        assert s.stable_rate == pytest.approx(60.0)
        # drift_rate = (2+2)/10 = 40%
        assert s.drift_rate == pytest.approx(40.0)
        assert s.breach_rate == pytest.approx(20.0)

    def test_total_breaches_summed(self):
        reports = [
            _report(outcome=GradientOutcome.BREACHED,
                    severity=GradientSeverity.HIGH, n_breaches=2),
            _report(outcome=GradientOutcome.BREACHED,
                    severity=GradientSeverity.HIGH, n_breaches=3),
        ]
        s = compute_cigw_aggregate_stats(reports)
        assert s.total_breaches == 5

    def test_disabled_excluded_from_actionable(self):
        reports = (
            [_report()] * 3
            + [_report(outcome=GradientOutcome.DISABLED)] * 2
        )
        s = compute_cigw_aggregate_stats(reports)
        assert s.total_reports == 5
        assert s.disabled_count == 2
        assert s.actionable_count == 3
        # rate over actionable only
        assert s.stable_rate == pytest.approx(100.0)

    def test_failed_outcomes_counted(self):
        reports = (
            [_report(outcome=GradientOutcome.FAILED)] * 3
            + [_report()]
        )
        s = compute_cigw_aggregate_stats(reports)
        assert s.failed_count == 3

    def test_garbage_items_counted_failed(self):
        reports = ["bad", _report(), 42, _report(), _report()]
        s = compute_cigw_aggregate_stats(reports)  # type: ignore
        assert s.total_reports == 5
        assert s.stable_count == 3
        assert s.failed_count == 2

    def test_severity_counters(self):
        report = GradientReport(
            outcome=GradientOutcome.BREACHED,
            readings=(
                _reading(severity=GradientSeverity.NONE),
                _reading(severity=GradientSeverity.LOW,
                         kind=MeasurementKind.IMPORT_COUNT),
                _reading(severity=GradientSeverity.CRITICAL,
                         kind=MeasurementKind.BANNED_TOKEN_COUNT),
            ),
        )
        s = compute_cigw_aggregate_stats([report])
        assert s.severity_counts["none"] == 1
        assert s.severity_counts["low"] == 1
        assert s.severity_counts["critical"] == 1
        # Per-kind drift: only non-NONE severities counted
        assert s.kind_drift_counts["line_count"] == 0  # NONE excluded
        assert s.kind_drift_counts["import_count"] == 1
        assert s.kind_drift_counts["banned_token_count"] == 1


# ---------------------------------------------------------------------------
# TestCompareGradientHistoryMatrix
# ---------------------------------------------------------------------------


class TestCompareGradientHistoryMatrix:

    def test_master_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_ENABLED", "false")
        report = compare_gradient_history([_report()] * 5)
        assert report.outcome is CIGWEffectivenessOutcome.DISABLED

    def test_sub_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_COMPARATOR_ENABLED", "false")
        report = compare_gradient_history([_report()] * 5)
        assert report.outcome is CIGWEffectivenessOutcome.DISABLED

    def test_enabled_override_false(self):
        report = compare_gradient_history(
            [_report()] * 5, enabled_override=False,
        )
        assert report.outcome is CIGWEffectivenessOutcome.DISABLED

    def test_none_failed(self):
        assert compare_gradient_history(None).outcome is CIGWEffectivenessOutcome.FAILED  # type: ignore

    def test_string_failed(self):
        report = compare_gradient_history("not a stream")  # type: ignore
        assert report.outcome is CIGWEffectivenessOutcome.FAILED
        assert "string_like_input" in report.detail

    def test_bytes_failed(self):
        assert (
            compare_gradient_history(b"\x00").outcome  # type: ignore
            is CIGWEffectivenessOutcome.FAILED
        )

    def test_int_failed(self):
        report = compare_gradient_history(42)  # type: ignore
        assert report.outcome is CIGWEffectivenessOutcome.FAILED

    def test_empty_insufficient(self):
        report = compare_gradient_history([])
        assert report.outcome is CIGWEffectivenessOutcome.INSUFFICIENT_DATA
        assert "empty_report_stream" in report.detail

    def test_below_low_n_insufficient(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CIGW_BASELINE_LOW_N", "5")
        report = compare_gradient_history([_report()] * 2)
        assert report.outcome is CIGWEffectivenessOutcome.INSUFFICIENT_DATA
        assert "baseline_quality=insufficient" in report.detail

    def test_high_stable_rate_healthy(self):
        report = compare_gradient_history([_report()] * 8)
        assert report.outcome is CIGWEffectivenessOutcome.HEALTHY
        assert report.stats.stable_rate == pytest.approx(100.0)

    def test_any_breach_degraded(self):
        # 5 STABLE + 1 BREACHED → degraded (any breach triggers it)
        reports = [_report()] * 5 + [
            _report(outcome=GradientOutcome.BREACHED,
                    severity=GradientSeverity.HIGH, n_breaches=1),
        ]
        report = compare_gradient_history(reports)
        assert report.outcome is CIGWEffectivenessOutcome.DEGRADED

    def test_high_drift_rate_degraded(self):
        # 3 stable + 4 drifting → drift_rate = 4/7 = 57% > 30% deg_thr
        reports = (
            [_report()] * 3
            + [_report(outcome=GradientOutcome.DRIFTING,
                       severity=GradientSeverity.LOW)] * 4
        )
        report = compare_gradient_history(reports)
        assert report.outcome is CIGWEffectivenessOutcome.DEGRADED

    def test_below_healthy_above_degraded_insufficient(self, monkeypatch):
        # Tighten healthy threshold to 95%, keep degraded at 30%.
        # 5 stable + 1 drifting → stable=83%, drift=17% → neither
        # threshold met → INSUFFICIENT_DATA
        monkeypatch.setenv("JARVIS_CIGW_HEALTHY_THRESHOLD_PCT", "95.0")
        reports = (
            [_report()] * 5
            + [_report(outcome=GradientOutcome.DRIFTING,
                       severity=GradientSeverity.LOW)]
        )
        report = compare_gradient_history(reports)
        assert report.outcome is CIGWEffectivenessOutcome.INSUFFICIENT_DATA
        assert "stable_below_threshold" in report.detail

    def test_breach_takes_precedence_over_healthy(self):
        # 19 stable + 1 breached → stable=95% > 80% healthy_thr,
        # but breach_rate=5% > 0 → DEGRADED takes precedence
        reports = [_report()] * 19 + [
            _report(outcome=GradientOutcome.BREACHED,
                    severity=GradientSeverity.HIGH, n_breaches=1),
        ]
        report = compare_gradient_history(reports)
        assert report.outcome is CIGWEffectivenessOutcome.DEGRADED

    def test_tightening_stamp_always_passed(self):
        for reports in (
            [], [_report()], [_report()] * 50, None,
        ):
            r = compare_gradient_history(reports)  # type: ignore
            assert r.tightening == "passed"

    def test_report_to_dict_full_shape(self):
        report = compare_gradient_history([_report()] * 5)
        d = report.to_dict()
        assert set(d.keys()) == {
            "outcome", "stats", "tightening", "detail",
            "schema_version",
        }
        assert d["outcome"] in {x.value for x in CIGWEffectivenessOutcome}


# ---------------------------------------------------------------------------
# TestComparatorDefensiveContract
# ---------------------------------------------------------------------------


class TestComparatorDefensiveContract:

    def test_compute_stats_never_raises_on_garbage(self):
        s = compute_cigw_aggregate_stats(
            [object(), object()],  # type: ignore
        )
        assert isinstance(s, CIGWAggregateStats)

    def test_compose_aggregated_detail_never_raises(self):
        result = compose_aggregated_detail(CIGWAggregateStats())
        assert isinstance(result, str)

    def test_compose_aggregated_detail_garbage_returns_empty(self):
        result = compose_aggregated_detail("not stats")  # type: ignore
        assert result == ""

    def test_stamp_garbage_safe(self):
        sv = stamp_gradient_report(None)  # type: ignore
        assert isinstance(sv, StampedGradientReport)

    def test_compare_returns_failed_on_iter_raise(self):
        class BadIter:
            def __iter__(self):
                raise RuntimeError("nope")
        report = compare_gradient_history(BadIter())
        assert isinstance(report, CIGWComparisonReport)


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

    def test_no_async_functions(self):
        """Slice 3 is pure-data; Slice 4 wraps via to_thread."""
        tree = _module_ast()
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef)

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

    def test_public_api_exported(self):
        for name in comp_mod.__all__:
            assert hasattr(comp_mod, name)

    def test_cost_contract_constant_present(self):
        assert hasattr(comp_mod, "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION")
        assert comp_mod.COST_CONTRACT_PRESERVED_BY_CONSTRUCTION is True

    def test_reuses_slice_1_primitives(self):
        """Positive invariant — proves zero duplication."""
        src = _module_source()
        assert "from backend.core.ouroboros.governance.verification.gradient_watcher import" in src
        assert "GradientReport" in src
        assert "GradientOutcome" in src

    def test_canonical_passed_resolution(self):
        """Slice 3 must resolve PASSED via adaptation.ledger so
        operators correlate via shared vocabulary (6 modules now
        stamp PASSED)."""
        src = _module_source()
        assert "MonotonicTighteningVerdict" in src
        assert "adaptation.ledger" in src
