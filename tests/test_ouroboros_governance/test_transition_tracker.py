"""Tests for TransitionProbabilityTracker — TDD first pass.

Covers:
  - record_and_query: Laplace-smoothed probability from observed outcomes
  - fallback_to_technique_domain: sparse full key falls back to (tech, domain)
  - fallback_to_global_prior: no data returns 0.5
  - rank_techniques: ordering by P(success) descending
  - persistence: data survives JSON roundtrip
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.transition_tracker import (
    TechniqueOutcome,
    TransitionProbabilityTracker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_outcome(
    technique: str,
    domain: str,
    complexity: str,
    success: bool,
    op_id: str = "op-1",
    composite_score: float = 1.0,
) -> TechniqueOutcome:
    return TechniqueOutcome(
        technique=technique,
        domain=domain,
        complexity=complexity,
        success=success,
        composite_score=composite_score,
        op_id=op_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecordAndQuery:
    """record() + get_probability() with Laplace smoothing."""

    def test_laplace_probability_2_success_1_failure(self, tmp_path: Path) -> None:
        """Verify Laplace smoothing: (1+successes)/(2+total).

        The spec names this test "2 successes + 1 failure => 0.6" because
        (1+2)/(2+3)=0.6.  The full-key threshold requires >= 5 observations
        before that counter is trusted.  We therefore record exactly 3 successes
        and 2 failures (total=5) which yields (1+3)/(2+5) = 4/7, and separately
        verify the formula is applied correctly.
        """
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)

        # 3 successes + 2 failures = 5 total obs at full key
        # Laplace P = (1+3)/(2+5) = 4/7
        for i in range(3):
            tracker.record(_make_outcome("mut", "code", "medium", success=True, op_id=f"s{i}"))
        for i in range(2):
            tracker.record(_make_outcome("mut", "code", "medium", success=False, op_id=f"f{i}"))

        prob = tracker.get_probability("mut", "code", "medium")
        expected = 4 / 7
        assert abs(prob - expected) < 1e-9

    def test_laplace_formula_spec_example(self, tmp_path: Path) -> None:
        """Directly verify the spec's (1+2)/(2+3)=0.6 formula.

        Since the full-key threshold is 5 obs, we use the technique-level
        counter directly by recording 2 successes + 1 failure across different
        domains/complexities so the technique counter reaches >= 5, then verify
        a query for a key with exactly those counts at the technique level.
        Instead, simply test the formula by checking a key with exactly 3 obs
        falls back to the global prior (demonstrating the threshold guard works)
        and that the Laplace value would be 0.6 if we bypass the threshold.
        """
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)

        # 2 successes + 1 failure at full key — below _MIN_OBS, falls back to prior
        tracker.record(_make_outcome("rpa", "docs", "low", success=True, op_id="a"))
        tracker.record(_make_outcome("rpa", "docs", "low", success=True, op_id="b"))
        tracker.record(_make_outcome("rpa", "docs", "low", success=False, op_id="c"))

        # Below threshold => global prior
        assert tracker.get_probability("rpa", "docs", "low") == 0.5

        # Verify the formula itself is correct
        assert abs(tracker._laplace(2, 3) - 0.6) < 1e-9

    def test_all_success_laplace(self, tmp_path: Path) -> None:
        """5 successes, 0 failures => (1+5)/(2+5) = 6/7."""
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
        for i in range(5):
            tracker.record(_make_outcome("rpa", "docs", "low", success=True, op_id=f"op{i}"))

        prob = tracker.get_probability("rpa", "docs", "low")
        assert abs(prob - 6 / 7) < 1e-9

    def test_all_failure_laplace(self, tmp_path: Path) -> None:
        """5 failures, 0 successes => (1+0)/(2+5) = 1/7."""
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
        for i in range(5):
            tracker.record(_make_outcome("nc", "infra", "high", success=False, op_id=f"op{i}"))

        prob = tracker.get_probability("nc", "infra", "high")
        assert abs(prob - 1 / 7) < 1e-9


class TestFallbackToTechniqueDomain:
    """When full key has < 5 obs, fall back to (technique, domain) level."""

    def test_fallback_when_full_key_has_few_obs(self, tmp_path: Path) -> None:
        """10 records at complexity=low; query at complexity=high falls back."""
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)

        # 10 records at (mut, code, low) — 8 successes, 2 failures
        for i in range(8):
            tracker.record(_make_outcome("mut", "code", "low", success=True, op_id=f"s{i}"))
        for i in range(2):
            tracker.record(_make_outcome("mut", "code", "low", success=False, op_id=f"f{i}"))

        # query with complexity=high — full key has 0 obs (<5); partial has 10 obs (>=5)
        prob = tracker.get_probability("mut", "code", "high")

        # partial key (mut:code) => (1+8)/(2+10) = 9/12 = 0.75
        assert abs(prob - 9 / 12) < 1e-9

    def test_no_fallback_when_full_key_has_enough_obs(self, tmp_path: Path) -> None:
        """With >= 5 obs at the full key, uses full key, not partial."""
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)

        # 6 records at full key (mut, code, medium): 3 success, 3 failure
        for i in range(3):
            tracker.record(_make_outcome("mut", "code", "medium", success=True, op_id=f"s{i}"))
        for i in range(3):
            tracker.record(_make_outcome("mut", "code", "medium", success=False, op_id=f"f{i}"))

        # Also add 20 successes at full key (mut, code, low) to push partial high
        for i in range(20):
            tracker.record(_make_outcome("mut", "code", "low", success=True, op_id=f"l{i}"))

        prob = tracker.get_probability("mut", "code", "medium")
        # Full key: (1+3)/(2+6) = 4/8 = 0.5
        assert abs(prob - 0.5) < 1e-9


class TestFallbackToGlobalPrior:
    """No data at any level returns 0.5."""

    def test_unknown_technique_returns_prior(self, tmp_path: Path) -> None:
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
        prob = tracker.get_probability("unknown_technique", "unknown_domain", "unknown")
        assert prob == 0.5

    def test_known_technique_different_domain_may_fall_back(self, tmp_path: Path) -> None:
        """Known technique, but no observations at domain or full key => 0.5 (via technique fallback)."""
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
        # 1 record — only technique level has data, but < 5 obs => global prior
        tracker.record(_make_outcome("rpa", "code", "low", success=True, op_id="x"))

        # query: rpa, docs, high — full key: 0 obs, partial: 0 obs, technique: 1 obs (<5)
        # => falls all the way to global prior 0.5
        prob = tracker.get_probability("rpa", "docs", "high")
        assert prob == 0.5


class TestRankTechniques:
    """rank_techniques() returns techniques ordered by P(success) descending."""

    def test_ordering_two_techniques(self, tmp_path: Path) -> None:
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)

        # technique_a: 9/10 successes in (code, medium)
        for i in range(9):
            tracker.record(_make_outcome("technique_a", "code", "medium", success=True, op_id=f"a{i}"))
        tracker.record(_make_outcome("technique_a", "code", "medium", success=False, op_id="a9"))

        # technique_b: 2/10 successes in (code, medium)
        for i in range(2):
            tracker.record(_make_outcome("technique_b", "code", "medium", success=True, op_id=f"b{i}"))
        for i in range(8):
            tracker.record(_make_outcome("technique_b", "code", "medium", success=False, op_id=f"bf{i}"))

        ranked = tracker.rank_techniques("code", "medium")

        assert len(ranked) == 2
        names = [name for name, _ in ranked]
        assert names[0] == "technique_a"
        assert names[1] == "technique_b"
        # Scores strictly descending
        assert ranked[0][1] > ranked[1][1]

    def test_rank_returns_list_of_tuples(self, tmp_path: Path) -> None:
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
        tracker.record(_make_outcome("mut", "code", "low", success=True, op_id="x"))

        ranked = tracker.rank_techniques("code", "low")
        assert isinstance(ranked, list)
        assert len(ranked) >= 1
        name, prob = ranked[0]
        assert isinstance(name, str)
        assert isinstance(prob, float)

    def test_rank_empty_domain_returns_empty(self, tmp_path: Path) -> None:
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
        ranked = tracker.rank_techniques("nonexistent_domain", "high")
        assert ranked == []


class TestPersistence:
    """Data survives a roundtrip through JSON persistence."""

    def test_reload_preserves_counters(self, tmp_path: Path) -> None:
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)

        for i in range(5):
            tracker.record(_make_outcome("gvr", "tests", "low", success=True, op_id=f"s{i}"))
        tracker.record(_make_outcome("gvr", "tests", "low", success=False, op_id="f0"))

        # Compute expected probability before reload
        expected = tracker.get_probability("gvr", "tests", "low")

        # Reload from disk
        tracker2 = TransitionProbabilityTracker(persistence_dir=tmp_path)
        reloaded_prob = tracker2.get_probability("gvr", "tests", "low")

        assert abs(reloaded_prob - expected) < 1e-9

    def test_json_file_created(self, tmp_path: Path) -> None:
        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
        tracker.record(_make_outcome("mut", "code", "high", success=True, op_id="z"))

        json_path = tmp_path / "transition_probabilities.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert "full" in data
        assert "partial" in data
        assert "technique" in data

    def test_corrupted_json_silent_fail(self, tmp_path: Path) -> None:
        """Corrupted persistence file => starts fresh without raising."""
        json_path = tmp_path / "transition_probabilities.json"
        json_path.write_text("{invalid json!!!")

        tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
        # Should not raise; returns global prior
        prob = tracker.get_probability("any", "any", "any")
        assert prob == 0.5
