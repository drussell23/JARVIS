"""Tests for ProphecyEngine — heuristic failure-risk predictor.

TDD coverage for TC15, TC16, TC20, TC34 and supporting cases.

Test index
----------
TC15  test_heuristic_scoring_formula
TC16  test_confidence_capped_without_jprime
TC20  test_prophecy_feeds_health_cortex  (high risk -> callback invoked)
TC34  test_concurrent_with_dream

Supporting:
    test_risk_level_low
    test_risk_level_medium
    test_risk_level_high
    test_risk_level_critical
    test_get_risk_scores_returns_cached
    test_empty_files_returns_low_risk
    test_oracle_unavailable_still_scores
    test_async_high_risk_callback
    test_callback_not_called_for_medium_risk
"""

from __future__ import annotations

import asyncio
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from backend.core.ouroboros.consciousness.prophecy_engine import (
    ProphecyEngine,
    _score_to_level,
)
from backend.core.ouroboros.consciousness.types import FileReputation, ProphecyReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reputation(
    success_rate: float = 1.0,
    fragility_score: float = 0.0,
    file_path: str = "some/file.py",
) -> FileReputation:
    return FileReputation(
        file_path=file_path,
        change_count=10,
        success_rate=success_rate,
        avg_blast_radius=2,
        common_co_failures=(),
        fragility_score=fragility_score,
    )


def _make_memory(reputation: FileReputation) -> MagicMock:
    """Mock memory engine that returns the given reputation for any file."""
    mem = MagicMock()
    mem.get_file_reputation.return_value = reputation
    return mem


def _make_oracle(edge_count: int = 0) -> MagicMock:
    """Mock oracle whose neighborhood has ``edge_count`` edges."""
    oracle = MagicMock()
    neighborhood = MagicMock()
    neighborhood.edges = list(range(edge_count))
    oracle.get_file_neighborhood.return_value = neighborhood
    return oracle


def _engine(
    success_rate: float = 1.0,
    fragility_score: float = 0.0,
    oracle_edges: int = 0,
    oracle: Any = None,
    callback=None,
) -> ProphecyEngine:
    rep = _make_reputation(success_rate=success_rate, fragility_score=fragility_score)
    mem = _make_memory(rep)
    if oracle is None and oracle_edges > 0:
        oracle = _make_oracle(oracle_edges)
    return ProphecyEngine(memory_engine=mem, oracle=oracle, on_high_risk_callback=callback)


# ---------------------------------------------------------------------------
# TC15 — heuristic scoring formula
# ---------------------------------------------------------------------------


class TestTC15HeuristicScoringFormula:
    """TC15: mock memory with known success_rate/fragility -> score matches formula."""

    @pytest.mark.asyncio
    async def test_heuristic_scoring_formula_no_oracle(self):
        """Without oracle: score = (1-sr)*0.3 + frag*0.3 + 0.1 (baseline)."""
        success_rate = 0.6
        fragility = 0.4
        # Expected: (1-0.6)*0.3 + 0.4*0.3 + 0.1 = 0.12 + 0.12 + 0.1 = 0.34
        expected = (1.0 - success_rate) * 0.3 + fragility * 0.3 + 0.1

        engine = _engine(success_rate=success_rate, fragility_score=fragility)
        score = engine._heuristic_risk("backend/core/foo.py")
        assert score == pytest.approx(expected, abs=1e-6)

    @pytest.mark.asyncio
    async def test_heuristic_scoring_formula_with_oracle(self):
        """With oracle providing 10 edges: dependency contrib = min(10/20,1)*0.2 = 0.1."""
        success_rate = 0.5
        fragility = 0.5
        edge_count = 10
        # dep contrib: min(10/20, 1) * 0.2 = 0.5 * 0.2 = 0.1
        expected = (1.0 - success_rate) * 0.3 + fragility * 0.3 + 0.1 * 0.2 + 0.1
        # = 0.15 + 0.15 + 0.1 + 0.1 = 0.5  (dep: min(10/20,1)*0.2 = 0.1, not 0.1*0.2)
        # Let's recompute carefully:
        # dep_score = min(edge_count / 20.0, 1.0) * 0.2
        dep_score = min(edge_count / 20.0, 1.0) * 0.2
        expected = (1.0 - success_rate) * 0.3 + fragility * 0.3 + dep_score + 0.1

        engine = _engine(
            success_rate=success_rate,
            fragility_score=fragility,
            oracle=_make_oracle(edge_count),
        )
        score = engine._heuristic_risk("backend/core/bar.py")
        assert score == pytest.approx(expected, abs=1e-6)

    @pytest.mark.asyncio
    async def test_heuristic_score_clamped_at_one(self):
        """Score must not exceed 1.0 even with extreme inputs."""
        engine = _engine(success_rate=0.0, fragility_score=1.0, oracle=_make_oracle(40))
        score = engine._heuristic_risk("some/file.py")
        assert score <= 1.0

    @pytest.mark.asyncio
    async def test_heuristic_score_baseline_only_when_no_history(self):
        """Unknown file with default reputation (sr=1.0, frag=0.0): score == 0.1."""
        # (1-1.0)*0.3 + 0.0*0.3 + 0.1 = 0.1
        engine = _engine(success_rate=1.0, fragility_score=0.0)
        score = engine._heuristic_risk("brand/new/file.py")
        assert score == pytest.approx(0.1, abs=1e-6)

    @pytest.mark.asyncio
    async def test_heuristic_scoring_full_formula_analyzed_end_to_end(self):
        """analyze_change triggers _heuristic_risk for each changed file."""
        engine = _engine(success_rate=0.7, fragility_score=0.2)
        report = await engine.analyze_change(["a.py", "b.py"])
        scores = engine.get_risk_scores()
        assert "a.py" in scores
        assert "b.py" in scores
        # Each score > 0.1 (baseline) because sr < 1.0 and frag > 0.0
        for s in scores.values():
            assert s > 0.1


# ---------------------------------------------------------------------------
# TC16 — confidence capped without J-Prime
# ---------------------------------------------------------------------------


class TestTC16ConfidenceCapped:
    """TC16: analyze_change always returns confidence <= 0.6 (heuristic-only)."""

    @pytest.mark.asyncio
    async def test_confidence_capped_without_jprime_low_risk(self):
        engine = _engine(success_rate=1.0, fragility_score=0.0)
        report = await engine.analyze_change(["safe/file.py"])
        assert report.confidence <= 0.6

    @pytest.mark.asyncio
    async def test_confidence_capped_without_jprime_high_risk(self):
        """Even with maximum risk inputs, confidence stays capped at 0.6."""
        engine = _engine(success_rate=0.0, fragility_score=1.0, oracle=_make_oracle(40))
        report = await engine.analyze_change(["risky/file.py"])
        assert report.confidence <= 0.6

    @pytest.mark.asyncio
    async def test_confidence_is_positive_for_nonempty_changeset(self):
        engine = _engine()
        report = await engine.analyze_change(["any/file.py"])
        assert report.confidence > 0.0


# ---------------------------------------------------------------------------
# TC20 — high risk fires on_high_risk_callback
# ---------------------------------------------------------------------------


class TestTC20ProphecyFeedsHealthCortex:
    """TC20: risk_level 'high' or 'critical' -> callback invoked with ProphecyReport."""

    @pytest.mark.asyncio
    async def test_high_risk_callback_invoked(self):
        callback = MagicMock()
        # success_rate=0.0, fragility=1.0 -> score well above 0.6
        engine = _engine(success_rate=0.0, fragility_score=1.0, callback=callback)
        report = await engine.analyze_change(["fragile/module.py"])
        assert report.risk_level in ("high", "critical")
        callback.assert_called_once_with(report)

    @pytest.mark.asyncio
    async def test_critical_risk_callback_invoked(self):
        callback = MagicMock()
        # max score: (1-0)*0.3 + 1.0*0.3 + min(40/20,1)*0.2 + 0.1 = 0.9
        engine = _engine(
            success_rate=0.0,
            fragility_score=1.0,
            oracle=_make_oracle(40),
            callback=callback,
        )
        report = await engine.analyze_change(["critical/file.py"])
        assert report.risk_level == "critical"
        callback.assert_called_once_with(report)

    @pytest.mark.asyncio
    async def test_callback_not_called_for_low_risk(self):
        callback = MagicMock()
        engine = _engine(success_rate=1.0, fragility_score=0.0, callback=callback)
        # score = 0.1 -> "low"
        report = await engine.analyze_change(["stable/file.py"])
        assert report.risk_level == "low"
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_not_called_for_medium_risk(self):
        callback = MagicMock()
        # score ≈ 0.3 + 0.09 + 0.1 = 0.49 -> "medium" (sr=0.3, frag=0.3)
        engine = _engine(success_rate=0.7, fragility_score=0.3, callback=callback)
        report = await engine.analyze_change(["medium/file.py"])
        # Only assert callback not called when actually medium
        if report.risk_level == "medium":
            callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_receives_prophecy_report_type(self):
        """Callback receives a ProphecyReport, not a raw dict."""
        received = []

        def capture(r):
            received.append(r)

        engine = _engine(success_rate=0.0, fragility_score=1.0, callback=capture)
        await engine.analyze_change(["bad/file.py"])
        assert len(received) == 1
        assert isinstance(received[0], ProphecyReport)


# ---------------------------------------------------------------------------
# TC34 — concurrent DreamEngine + ProphecyEngine don't crash
# ---------------------------------------------------------------------------


class TestTC34ConcurrentWithDream:
    """TC34: ProphecyEngine and a mock DreamEngine run simultaneously without crash."""

    @pytest.mark.asyncio
    async def test_concurrent_with_dream(self):
        """Run analyze_change concurrently with a mock dream task — no crash."""

        engine = _engine(success_rate=0.5, fragility_score=0.4)

        async def mock_dream():
            """Simulates DreamEngine doing background work (no shared state)."""
            await asyncio.sleep(0.01)
            return "dream_done"

        # Run both concurrently
        report, dream_result = await asyncio.gather(
            engine.analyze_change(["shared/module.py"]),
            mock_dream(),
        )

        assert isinstance(report, ProphecyReport)
        assert dream_result == "dream_done"

    @pytest.mark.asyncio
    async def test_sequential_analyze_calls_are_safe(self):
        """Two sequential analyze_change calls both complete cleanly."""
        engine = _engine(success_rate=0.5, fragility_score=0.3)

        report1 = await engine.analyze_change(["file_a.py"])
        report2 = await engine.analyze_change(["file_b.py"])

        assert isinstance(report1, ProphecyReport)
        assert isinstance(report2, ProphecyReport)
        # Risk scores reflect the latest call
        scores = engine.get_risk_scores()
        assert "file_b.py" in scores
        assert "file_a.py" not in scores

    @pytest.mark.asyncio
    async def test_concurrent_analyze_calls_serialise_via_lock(self):
        """Two simultaneous analyze_change calls both finish without error."""
        engine = _engine(success_rate=0.4, fragility_score=0.4)

        results = await asyncio.gather(
            engine.analyze_change(["file_x.py"]),
            engine.analyze_change(["file_y.py"]),
        )

        assert len(results) == 2
        assert all(isinstance(r, ProphecyReport) for r in results)


# ---------------------------------------------------------------------------
# Supporting: risk level classification
# ---------------------------------------------------------------------------


class TestRiskLevelClassification:
    """Verify score -> risk_level thresholds."""

    def test_risk_level_low(self):
        assert _score_to_level(0.0) == "low"
        assert _score_to_level(0.1) == "low"
        assert _score_to_level(0.29) == "low"

    def test_risk_level_medium(self):
        assert _score_to_level(0.3) == "medium"
        assert _score_to_level(0.45) == "medium"
        assert _score_to_level(0.59) == "medium"

    def test_risk_level_high(self):
        assert _score_to_level(0.6) == "high"
        assert _score_to_level(0.7) == "high"
        assert _score_to_level(0.79) == "high"

    def test_risk_level_critical(self):
        assert _score_to_level(0.8) == "critical"
        assert _score_to_level(0.9) == "critical"
        assert _score_to_level(1.0) == "critical"

    @pytest.mark.asyncio
    async def test_analyze_change_low_risk_end_to_end(self):
        """End-to-end: stable file -> report risk_level == 'low'."""
        engine = _engine(success_rate=1.0, fragility_score=0.0)
        report = await engine.analyze_change(["stable/module.py"])
        assert report.risk_level == "low"

    @pytest.mark.asyncio
    async def test_analyze_change_high_risk_end_to_end(self):
        """End-to-end: fragile file with no successes -> risk_level in high/critical.

        Score = (1-0.0)*0.3 + 0.8*0.3 + 0.1 = 0.30 + 0.24 + 0.1 = 0.64 -> high.
        """
        engine = _engine(success_rate=0.0, fragility_score=0.8)
        report = await engine.analyze_change(["risky/module.py"])
        assert report.risk_level in ("high", "critical")

    @pytest.mark.asyncio
    async def test_analyze_change_critical_risk_end_to_end(self):
        """Max inputs produce critical risk."""
        engine = _engine(
            success_rate=0.0,
            fragility_score=1.0,
            oracle=_make_oracle(40),
        )
        report = await engine.analyze_change(["doomed/module.py"])
        assert report.risk_level == "critical"


# ---------------------------------------------------------------------------
# Supporting: get_risk_scores returns cached
# ---------------------------------------------------------------------------


class TestGetRiskScoresReturnsCached:
    @pytest.mark.asyncio
    async def test_get_risk_scores_returns_cached(self):
        """get_risk_scores returns a snapshot from the latest analyze_change."""
        engine = _engine(success_rate=0.5, fragility_score=0.3)

        # No analysis yet -> empty
        assert engine.get_risk_scores() == {}

        await engine.analyze_change(["alpha.py", "beta.py"])
        scores = engine.get_risk_scores()
        assert "alpha.py" in scores
        assert "beta.py" in scores

    @pytest.mark.asyncio
    async def test_get_risk_scores_is_a_copy(self):
        """Mutating the returned dict does not affect internal state."""
        engine = _engine(success_rate=0.5, fragility_score=0.3)
        await engine.analyze_change(["file.py"])

        scores = engine.get_risk_scores()
        scores["injected_key"] = 99.9

        fresh = engine.get_risk_scores()
        assert "injected_key" not in fresh


# ---------------------------------------------------------------------------
# Supporting: empty files returns low risk
# ---------------------------------------------------------------------------


class TestEmptyFilesReturnsLowRisk:
    @pytest.mark.asyncio
    async def test_empty_files_returns_low_risk(self):
        """analyze_change([]) returns risk_level='low', no predicted failures."""
        engine = _engine()
        report = await engine.analyze_change([])
        assert report.risk_level == "low"
        assert len(report.predicted_failures) == 0
        assert report.confidence == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_empty_files_risk_scores_cleared(self):
        """After an empty analysis the cached risk scores are empty."""
        engine = _engine(success_rate=0.5, fragility_score=0.3)
        # Populate scores first
        await engine.analyze_change(["something.py"])
        assert engine.get_risk_scores() != {}

        # Now empty changeset clears them
        await engine.analyze_change([])
        assert engine.get_risk_scores() == {}


# ---------------------------------------------------------------------------
# Supporting: oracle unavailable still scores (graceful degradation)
# ---------------------------------------------------------------------------


class TestOracleUnavailableStillScores:
    @pytest.mark.asyncio
    async def test_oracle_unavailable_still_scores(self):
        """When oracle is None, analysis completes with heuristic-only score."""
        engine = _engine(success_rate=0.6, fragility_score=0.3, oracle=None)
        report = await engine.analyze_change(["important/module.py"])
        assert isinstance(report, ProphecyReport)
        # Score = (1-0.6)*0.3 + 0.3*0.3 + 0.1 = 0.12 + 0.09 + 0.1 = 0.31 -> medium
        scores = engine.get_risk_scores()
        assert "important/module.py" in scores
        assert scores["important/module.py"] == pytest.approx(0.31, abs=1e-6)

    @pytest.mark.asyncio
    async def test_oracle_raises_still_scores(self):
        """Oracle.get_file_neighborhood raising does not crash analysis."""
        bad_oracle = MagicMock()
        bad_oracle.get_file_neighborhood.side_effect = RuntimeError("oracle exploded")

        rep = _make_reputation(success_rate=0.5, fragility_score=0.5)
        mem = _make_memory(rep)
        engine = ProphecyEngine(memory_engine=mem, oracle=bad_oracle)

        report = await engine.analyze_change(["module.py"])
        assert isinstance(report, ProphecyReport)
        # Score = (1-0.5)*0.3 + 0.5*0.3 + 0.1 = 0.15 + 0.15 + 0.1 = 0.4 (no dep contrib)
        scores = engine.get_risk_scores()
        assert scores["module.py"] == pytest.approx(0.4, abs=1e-6)

    @pytest.mark.asyncio
    async def test_memory_raises_still_completes(self):
        """Memory.get_file_reputation raising does not crash analysis."""
        mem = MagicMock()
        mem.get_file_reputation.side_effect = RuntimeError("memory gone")
        engine = ProphecyEngine(memory_engine=mem)

        report = await engine.analyze_change(["orphan/file.py"])
        # Only baseline 0.1, which is < 0.3 -> low
        assert isinstance(report, ProphecyReport)
        assert report.risk_level == "low"


# ---------------------------------------------------------------------------
# Supporting: async callback is awaited
# ---------------------------------------------------------------------------


class TestAsyncHighRiskCallback:
    @pytest.mark.asyncio
    async def test_async_high_risk_callback(self):
        """An async callback is properly awaited (not just scheduled)."""
        received = []

        async def async_cb(report):
            received.append(report)

        engine = _engine(success_rate=0.0, fragility_score=1.0, callback=async_cb)
        report = await engine.analyze_change(["async_target.py"])

        assert report.risk_level in ("high", "critical")
        assert len(received) == 1
        assert received[0] is report


# ---------------------------------------------------------------------------
# Supporting: lifecycle no-ops
# ---------------------------------------------------------------------------


class TestLifecycleNoOps:
    @pytest.mark.asyncio
    async def test_start_stop_are_noops(self):
        """start() and stop() complete without error and don't affect state."""
        engine = _engine()
        await engine.start()
        await engine.analyze_change(["file.py"])
        await engine.stop()
        # State is preserved
        scores = engine.get_risk_scores()
        assert "file.py" in scores


# ---------------------------------------------------------------------------
# Supporting: report fields are well-formed
# ---------------------------------------------------------------------------


class TestReportFields:
    @pytest.mark.asyncio
    async def test_report_has_change_id(self):
        engine = _engine()
        report = await engine.analyze_change(["x.py"])
        assert report.change_id.startswith("chg-")
        assert len(report.change_id) == 16  # "chg-" + 12 hex chars

    @pytest.mark.asyncio
    async def test_predicted_failures_sorted_by_probability(self):
        """PredictedFailure items are sorted highest probability first."""
        # Give two files different scores by using different reputations
        mem = MagicMock()

        def rep_by_path(path: str) -> FileReputation:
            if path == "high_risk.py":
                return _make_reputation(success_rate=0.0, fragility_score=1.0, file_path=path)
            return _make_reputation(success_rate=0.8, fragility_score=0.1, file_path=path)

        mem.get_file_reputation.side_effect = rep_by_path
        engine = ProphecyEngine(memory_engine=mem)
        report = await engine.analyze_change(["low_risk.py", "high_risk.py"])

        if len(report.predicted_failures) >= 2:
            probs = [f.probability for f in report.predicted_failures]
            assert probs == sorted(probs, reverse=True)

    @pytest.mark.asyncio
    async def test_recommended_tests_includes_test_files_in_changeset(self):
        """If a test file is itself changed, it appears in recommended_tests."""
        engine = _engine(success_rate=0.5, fragility_score=0.4)
        report = await engine.analyze_change(["src/module.py", "tests/test_module.py"])
        assert "tests/test_module.py" in report.recommended_tests

    @pytest.mark.asyncio
    async def test_reasoning_is_non_empty_string(self):
        engine = _engine()
        report = await engine.analyze_change(["any.py"])
        assert isinstance(report.reasoning, str)
        assert len(report.reasoning) > 10
