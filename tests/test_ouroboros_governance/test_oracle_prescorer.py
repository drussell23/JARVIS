# tests/test_ouroboros_governance/test_oracle_prescorer.py
"""Tests for OraclePreScorer — fast approximate quality gate."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.oracle_prescorer import (
    OraclePreScorer,
    PreScoreResult,
)


def _make_oracle(
    blast_risk: str = "low",
    blast_total: int = 2,
    deps: list | None = None,
    dependents: list | None = None,
) -> MagicMock:
    """Build a minimal mock oracle with preset blast radius and graph data."""
    oracle = MagicMock()

    blast = MagicMock()
    blast.risk_level = blast_risk
    blast.total_affected = blast_total
    oracle.compute_blast_radius.return_value = blast

    oracle.get_dependencies.return_value = deps if deps is not None else []
    oracle.get_dependents.return_value = dependents if dependents is not None else []

    return oracle


class TestPreScoreResultDataclass:
    def test_fields_accessible(self):
        result = PreScoreResult(
            pre_score=0.2,
            gate="FAST_TRACK",
            blast_radius_signal=0.1,
            coupling_signal=0.05,
            complexity_signal=0.0,
            test_coverage_signal=0.0,
            locality_signal=0.0,
        )
        assert result.pre_score == 0.2
        assert result.gate == "FAST_TRACK"

    def test_frozen(self):
        result = PreScoreResult(
            pre_score=0.5,
            gate="NORMAL",
            blast_radius_signal=0.0,
            coupling_signal=0.0,
            complexity_signal=0.0,
            test_coverage_signal=0.0,
            locality_signal=0.0,
        )
        with pytest.raises((AttributeError, TypeError)):
            result.pre_score = 0.9  # type: ignore[misc]


class TestOraclePreScorerLowRisk:
    """low blast radius, few deps, has tests → FAST_TRACK"""

    def test_low_risk_candidate(self):
        oracle = _make_oracle(
            blast_risk="low",
            blast_total=1,
            deps=["dep_a"],
            dependents=["user_a"],
        )
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/foo.py"],
            max_complexity=5,
            has_tests=True,
        )
        assert result.gate == "FAST_TRACK"
        assert result.pre_score < 0.3

    def test_fast_track_score_is_low(self):
        oracle = _make_oracle(
            blast_risk="low",
            blast_total=2,
            deps=[],
            dependents=[],
        )
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/foo.py"],
            max_complexity=0,
            has_tests=True,
        )
        assert result.pre_score < 0.3
        assert result.test_coverage_signal == 0.0


class TestOraclePreScorerHighRisk:
    """critical blast radius, many deps, no tests → WARN"""

    def test_high_risk_candidate(self):
        oracle = _make_oracle(
            blast_risk="critical",
            blast_total=60,
            deps=[f"dep_{i}" for i in range(10)],
            dependents=[f"user_{i}" for i in range(12)],
        )
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/critical.py"],
            max_complexity=40,
            has_tests=False,
        )
        assert result.gate == "WARN"
        assert result.pre_score >= 0.7

    def test_no_tests_raises_coverage_signal(self):
        oracle = _make_oracle(blast_risk="high", blast_total=35)
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/heavy.py"],
            max_complexity=20,
            has_tests=False,
        )
        assert result.test_coverage_signal == 1.0


class TestOraclePreScorerMediumRisk:
    """medium blast radius, moderate deps → NORMAL"""

    def test_medium_risk_candidate(self):
        # blast=max(0.3, 15/50)=0.3, coupling=10/20=0.5, complexity=20/30=0.667
        # pre_score ≈ 0.348 → NORMAL
        oracle = _make_oracle(
            blast_risk="medium",
            blast_total=15,
            deps=[f"dep_{i}" for i in range(5)],
            dependents=[f"user_{i}" for i in range(5)],
        )
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/service.py"],
            max_complexity=20,
            has_tests=True,
        )
        assert result.gate == "NORMAL"
        assert 0.3 <= result.pre_score < 0.7


class TestOraclePreScorerFailure:
    """oracle raises exception → returns 0.5 / NORMAL (fail-open)"""

    def test_oracle_failure_returns_neutral(self):
        oracle = MagicMock()
        oracle.compute_blast_radius.side_effect = RuntimeError("graph unavailable")
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/foo.py"],
            max_complexity=10,
            has_tests=True,
        )
        assert result.pre_score == 0.5
        assert result.gate == "NORMAL"

    def test_get_dependencies_failure_returns_neutral(self):
        oracle = MagicMock()
        blast = MagicMock()
        blast.risk_level = "low"
        blast.total_affected = 1
        oracle.compute_blast_radius.return_value = blast
        oracle.get_dependencies.side_effect = ConnectionError("db down")
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/foo.py"],
        )
        assert result.pre_score == 0.5
        assert result.gate == "NORMAL"


class TestOraclePreScorerMultipleFiles:
    """two files, one low risk one high → uses worst blast radius"""

    def test_multiple_files_uses_worst(self):
        oracle = MagicMock()

        blast_low = MagicMock()
        blast_low.risk_level = "low"
        blast_low.total_affected = 2

        blast_high = MagicMock()
        blast_high.risk_level = "critical"
        blast_high.total_affected = 55

        oracle.compute_blast_radius.side_effect = [blast_low, blast_high]
        oracle.get_dependencies.return_value = []
        oracle.get_dependents.return_value = []

        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/a.py", "backend/core/b.py"],
            max_complexity=5,
            has_tests=True,
        )
        # blast_radius_signal should reflect the critical file, not the low one
        assert result.blast_radius_signal > 0.5

    def test_multi_file_locality_signal_nonzero(self):
        oracle = _make_oracle(blast_risk="low", blast_total=1)
        # Override to return same value twice
        blast = MagicMock()
        blast.risk_level = "low"
        blast.total_affected = 1
        oracle.compute_blast_radius.return_value = blast

        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/a.py", "backend/utils/b.py"],
            max_complexity=0,
            has_tests=True,
        )
        # Two different dirs → locality_signal = 1 - 1/2 = 0.5
        assert result.locality_signal == pytest.approx(0.5)

    def test_single_file_locality_signal_is_zero(self):
        oracle = _make_oracle(blast_risk="low", blast_total=1)
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            target_files=["backend/core/a.py"],
            max_complexity=0,
            has_tests=True,
        )
        assert result.locality_signal == 0.0


class TestOraclePreScorerSignals:
    """Unit-level signal verification."""

    def test_coupling_signal_scales_with_deps(self):
        # 20 total deps/dependents → coupling = 1.0
        oracle = _make_oracle(
            blast_risk="low",
            blast_total=1,
            deps=[f"d{i}" for i in range(10)],
            dependents=[f"u{i}" for i in range(10)],
        )
        scorer = OraclePreScorer(oracle)
        result = scorer.score(["backend/core/foo.py"], has_tests=True)
        assert result.coupling_signal == pytest.approx(1.0)

    def test_complexity_signal_capped_at_one(self):
        oracle = _make_oracle(blast_risk="low", blast_total=1)
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            ["backend/core/foo.py"],
            max_complexity=90,
            has_tests=True,
        )
        assert result.complexity_signal == pytest.approx(1.0)

    def test_complexity_zero_gives_zero_signal(self):
        oracle = _make_oracle(blast_risk="low", blast_total=1)
        scorer = OraclePreScorer(oracle)
        result = scorer.score(
            ["backend/core/foo.py"],
            max_complexity=0,
            has_tests=True,
        )
        assert result.complexity_signal == pytest.approx(0.0)
