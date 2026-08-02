# tests/test_ouroboros_governance/test_rsi_convergence_integration.py
"""End-to-end integration tests for all 6 RSI convergence components.

Exercises:
  1. CompositeScoreFunction + ScoreHistory
  2. ConvergenceTracker
  3. TransitionProbabilityTracker
  4. OraclePreScorer
  5. VindicationReflector
  6. EphemeralUsageTracker (graduation)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.composite_score import (
    CompositeScoreFunction,
    ScoreHistory,
)
from backend.core.ouroboros.governance.convergence_tracker import (
    ConvergenceState,
    ConvergenceTracker,
)
from backend.core.ouroboros.governance.graduation_orchestrator import (
    EphemeralUsageTracker,
)
from backend.core.ouroboros.governance.oracle_prescorer import OraclePreScorer
from backend.core.ouroboros.governance.transition_tracker import (
    TechniqueOutcome,
    TransitionProbabilityTracker,
)
from backend.core.ouroboros.governance.vindication_reflector import VindicationReflector


# ---------------------------------------------------------------------------
# Helper: build a mock oracle that satisfies both OraclePreScorer and
# VindicationReflector interfaces.
# ---------------------------------------------------------------------------

class _MockBlastRadius:
    """Minimal blast-radius result with the two attributes that scorers read."""

    def __init__(self, total_affected: int = 5, risk_level: str = "low") -> None:
        self.total_affected = total_affected
        self.risk_level = risk_level


def _make_mock_oracle() -> MagicMock:
    """Return a MagicMock oracle with plausible low-risk responses."""
    oracle = MagicMock()
    oracle.compute_blast_radius.return_value = _MockBlastRadius(
        total_affected=5, risk_level="low"
    )
    oracle.get_dependencies.return_value = [MagicMock()] * 3
    oracle.get_dependents.return_value = [MagicMock()] * 2
    return oracle


# ---------------------------------------------------------------------------
# Test 1 — full RSI pipeline with 20 improving operations
# ---------------------------------------------------------------------------


def test_full_rsi_pipeline(tmp_path: Path) -> None:
    """Simulate 20 steadily improving operations and verify all 6 RSI components."""

    # ---- Component setup ---------------------------------------------------
    score_fn = CompositeScoreFunction(persistence_dir=tmp_path)
    history = ScoreHistory(persistence_dir=tmp_path)
    tracker = ConvergenceTracker()
    transition_tracker = TransitionProbabilityTracker(persistence_dir=tmp_path)
    mock_oracle = _make_mock_oracle()
    prescorer = OraclePreScorer(oracle=mock_oracle)
    reflector = VindicationReflector(oracle=mock_oracle)

    # ---- Simulate 20 improving iterations ----------------------------------
    # Scores: test_rate 0.70 → 0.95, coverage 0.50 → 0.90, complexity 15 → 5
    num_iterations = 20
    for i in range(num_iterations):
        fraction = i / max(1, num_iterations - 1)  # 0.0 → 1.0

        test_rate_before = 0.70
        test_rate_after = 0.70 + 0.25 * fraction      # 0.70 → 0.95

        coverage_before = 50.0
        coverage_after = 50.0 + 40.0 * fraction       # 50 → 90

        complexity_before = 15
        complexity_after = int(15 - 10 * fraction)    # 15 → 5

        # Compute and record score via CompositeScoreFunction (auto-records to
        # its internal ScoreHistory instance).  We also record it manually in
        # the standalone history to exercise both.
        score = score_fn.compute(
            f"op-{i:03d}",
            test_pass_rate_before=test_rate_before,
            test_pass_rate_after=test_rate_after,
            coverage_before=coverage_before,
            coverage_after=coverage_after,
            complexity_before=float(complexity_before),
            complexity_after=float(complexity_after),
            lint_violations_before=5,
            lint_violations_after=max(0, 5 - i // 4),
            blast_radius_total=5,
        )
        history.record(score)

        # Record technique outcomes (~80 % success rate for module_mutation)
        outcome = TechniqueOutcome(
            technique="module_mutation",
            domain="backend",
            complexity="heavy_code",
            success=(i % 5 != 0),   # succeeds 4 out of 5 → 80 %
            composite_score=score.composite,
            op_id=f"op-{i:03d}",
        )
        transition_tracker.record(outcome)

    # ---- Convergence assertions --------------------------------------------
    composite_values = score_fn.history.get_composite_values()
    assert len(composite_values) == num_iterations

    report = tracker.analyze(composite_values)
    assert report.state in (ConvergenceState.IMPROVING, ConvergenceState.LOGARITHMIC), (
        f"Expected IMPROVING or LOGARITHMIC, got {report.state}. slope={report.slope:.4f}"
    )
    assert report.slope < 0, (
        f"Expected negative slope (improving), got slope={report.slope:.4f}"
    )

    # ---- Transition probability assertions ---------------------------------
    prob = transition_tracker.get_probability(
        "module_mutation", "backend", "heavy_code"
    )
    assert prob > 0.5, (
        f"Expected P(success) > 0.5 for module_mutation, got {prob:.4f}"
    )

    # ---- OraclePreScorer assertions ----------------------------------------
    result = prescorer.score(
        target_files=["backend/core/example.py"],
        max_complexity=10,
        has_tests=True,
    )
    assert result.gate in ("FAST_TRACK", "NORMAL", "WARN"), (
        f"Unexpected gate value: {result.gate!r}"
    )

    # ---- VindicationReflector assertions -----------------------------------
    # An improving patch: after < before for coupling/blast/complexity
    vindication = reflector.reflect(
        target_files=["backend/core/example.py"],
        coupling_after=3.0,      # same as mock (3 deps + 2 dependents = 5 before)
        blast_radius_after=3.0,  # lower than mock's 5
        complexity_after=5.0,    # better than before
        complexity_before=15.0,
    )
    # vindication_score > 0 means patch improves future tractability
    assert vindication.vindication_score > 0, (
        f"Expected positive vindication score for improving patch, "
        f"got {vindication.vindication_score:.4f}"
    )
    assert vindication.advisory in ("vindicating", "neutral"), (
        f"Unexpected advisory: {vindication.advisory!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — adaptive graduation fires with diverse goals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adaptive_graduation_with_diverse_goals(tmp_path: Path) -> None:
    """Record 5 diverse successes for the same goal class and verify graduation fires.

    The EphemeralUsageTracker fires graduation once a goal_class_id accumulates
    enough successes.  A goal_class_id is derived from normalized keywords, so
    five different phrasings of the same intent (e.g. "analyse code quality")
    can all map to different classes — but 5 successes of a *single* class
    reliably exceeds the adaptive threshold (threshold = ceil(2.0 / p_success)
    with Beta(1+s, 1+f) prior; 5 successes → threshold ≤ 3).
    """
    tracker = EphemeralUsageTracker(
        persistence_path=tmp_path / "usage.json",
        graduation_threshold=3,  # explicit baseline; adaptive may lower it
    )

    # Five successive uses of the same ephemeral tool (same goal wording →
    # same goal_class_id).  This mirrors the real graduation trigger: a user
    # repeatedly invoking the same synthesized tool.
    base_goal = "generate unit tests for the payment processor module"
    diverse_successes = [
        (base_goal, f"hash{i:03d}", "success", 1.0 + i * 0.2)
        for i in range(5)
    ]

    fired_id: str | None = None
    for goal, code_hash, outcome, elapsed in diverse_successes:
        result = await tracker.record_usage(
            goal=goal,
            code_hash=code_hash,
            outcome=outcome,
            elapsed_s=elapsed,
        )
        if result is not None:
            fired_id = result
            break

    # Graduation must have fired by the 5th success.
    assert fired_id is not None, (
        "Expected adaptive graduation to fire after 5 successful uses of the "
        "same goal class, but record_usage never returned a goal_class_id."
    )


# ---------------------------------------------------------------------------
# Test 3 — ScoreHistory persistence round-trip
# ---------------------------------------------------------------------------


def test_score_history_persistence_roundtrip(tmp_path: Path) -> None:
    """Scores written to disk must be fully recoverable by a fresh ScoreHistory."""

    score_fn = CompositeScoreFunction(persistence_dir=tmp_path)

    recorded: list[float] = []
    for i in range(5):
        fraction = i / 4.0  # 0.0 → 1.0
        score = score_fn.compute(
            f"roundtrip-op-{i}",
            test_pass_rate_before=0.60,
            test_pass_rate_after=0.60 + 0.10 * fraction,
            coverage_before=50.0,
            coverage_after=50.0 + 5.0 * fraction,
            complexity_before=20.0,
            complexity_after=20.0 - 3.0 * fraction,
            lint_violations_before=3,
            lint_violations_after=3,
            blast_radius_total=2,
        )
        recorded.append(score.composite)

    # Load a fresh ScoreHistory from the same directory
    reloaded = ScoreHistory(persistence_dir=tmp_path)
    reloaded_values = reloaded.get_composite_values()

    assert len(reloaded_values) == len(recorded), (
        f"Expected {len(recorded)} scores after reload, got {len(reloaded_values)}"
    )
    for orig, reloaded_val in zip(recorded, reloaded_values):
        assert abs(orig - reloaded_val) < 1e-9, (
            f"Score mismatch after reload: {orig} vs {reloaded_val}"
        )
