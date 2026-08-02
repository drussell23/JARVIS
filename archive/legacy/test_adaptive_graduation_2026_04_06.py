"""Tests for Bayesian adaptive graduation threshold (Tasks 5-6)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.graduation_orchestrator import (
    AdaptiveThresholdResult,
    EphemeralUsageTracker,
    compute_adaptive_threshold,
)


# ---------------------------------------------------------------------------
# Unit tests for compute_adaptive_threshold
# ---------------------------------------------------------------------------

def test_all_successes_low_threshold():
    """3 successes, 0 failures, 3 unique goals of 3 total → threshold == 3."""
    result = compute_adaptive_threshold(
        successes=3, failures=0, unique_goals=3, total_uses=3
    )
    assert result.threshold == 3


def test_mixed_results_higher_threshold():
    """2 successes, 1 failure → lower p_success → threshold >= 4."""
    result = compute_adaptive_threshold(
        successes=2, failures=1, unique_goals=2, total_uses=3
    )
    assert result.threshold >= 4


def test_low_success_rate_much_higher_threshold():
    """1 success, 2 failures → even lower p → threshold >= 5."""
    result = compute_adaptive_threshold(
        successes=1, failures=2, unique_goals=1, total_uses=3
    )
    assert result.threshold >= 5


def test_diversity_bonus_same_goal():
    """3 successes with only 1 unique goal of 3 total → diversity < 0.5."""
    result = compute_adaptive_threshold(
        successes=3, failures=0, unique_goals=1, total_uses=3
    )
    assert result.diversity < 0.5


def test_diversity_bonus_diverse_goals():
    """3 successes, 3 unique goals of 3 total → diversity >= 0.9."""
    result = compute_adaptive_threshold(
        successes=3, failures=0, unique_goals=3, total_uses=3
    )
    assert result.diversity >= 0.9


def test_minimum_threshold_enforced():
    """Even with 100 successes the threshold must be at least _ADAPTIVE_MIN_THRESHOLD (2)."""
    result = compute_adaptive_threshold(
        successes=100, failures=0, unique_goals=100, total_uses=100
    )
    assert result.threshold >= 2


def test_zero_uses_returns_high_threshold():
    """Zero uses → diversity=0, effective_p falls back to 0.1 floor → threshold >= 4."""
    result = compute_adaptive_threshold(
        successes=0, failures=0, unique_goals=0, total_uses=0
    )
    assert result.threshold >= 4


def test_adaptive_threshold_result_fields():
    """AdaptiveThresholdResult is the right type and all field ranges are valid."""
    result = compute_adaptive_threshold(
        successes=5, failures=2, unique_goals=4, total_uses=7
    )
    assert isinstance(result, AdaptiveThresholdResult)
    assert isinstance(result.threshold, int)
    assert 0.0 <= result.p_success <= 1.0
    assert 0.0 <= result.diversity <= 1.0
    assert 0.0 <= result.effective_p <= 1.0


# ---------------------------------------------------------------------------
# Integration tests for EphemeralUsageTracker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tracker_uses_adaptive_threshold(tmp_path):
    """Record 3 successes for the SAME exact goal string (low diversity) → no graduation fires.

    With unique_goals=1 and total_uses=3, diversity=0.33, effective_p is reduced
    which raises the adaptive threshold above 3.
    """
    tracker = EphemeralUsageTracker(persistence_path=tmp_path / "usage.json")
    results = []
    for _ in range(3):
        r = await tracker.record_usage(
            goal="send email to bob",
            code_hash="abc123",
            outcome="success",
            elapsed_s=0.1,
        )
        results.append(r)

    assert all(r is None for r in results), (
        f"Expected all None (adaptive threshold > 3 due to low diversity), got {results}"
    )


@pytest.mark.asyncio
async def test_tracker_fires_with_diverse_goals(tmp_path):
    """Record 3 successes for DIFFERENT goal phrasings that normalize to the same class.

    When goal strings vary (unique goal_hashes) but normalize to the same gcid,
    diversity approaches 1.0, which lowers the adaptive threshold enough to fire at 3.

    These three phrasings all normalize to key 'email notification send' (gcid 26d939b0c4c2)
    but have different raw goal strings → different goal_hash values stored → diversity=1.0.
    """
    tracker = EphemeralUsageTracker(persistence_path=tmp_path / "usage.json")

    # All three normalize to same gcid but have distinct raw goal hashes
    diverse_goals = [
        "send email notification",
        "please send email notification",
        "send the email notification",
    ]

    fired = None
    for goal in diverse_goals:
        r = await tracker.record_usage(
            goal=goal,
            code_hash="hashX",
            outcome="success",
            elapsed_s=0.1,
        )
        if r is not None:
            fired = r
            break

    assert fired is not None, (
        "Expected graduation to fire after 3 successes with diverse goal phrasings"
    )
