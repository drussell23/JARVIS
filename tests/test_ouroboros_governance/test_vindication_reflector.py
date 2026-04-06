# tests/test_ouroboros_governance/test_vindication_reflector.py
"""Tests for VindicationReflector -- trajectory analysis for RSI patches.

Based on Fallenstein & Soares' Vingean reflection; measures whether a patch
makes future patches better or worse across coupling, blast radius, and
complexity dimensions.
"""

import pytest
from unittest.mock import MagicMock

from backend.core.ouroboros.governance.vindication_reflector import (
    VindicationReflector,
    VindicationResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockBlastRadius:
    """Minimal stand-in for BlastRadiusResult."""

    def __init__(self, total_affected: int):
        self.total_affected = total_affected


def _mock_oracle(deps_before: int = 5, br_before: int = 10):
    """Build a MagicMock oracle with preset dependency/blast-radius data."""
    oracle = MagicMock()
    oracle.get_dependencies.return_value = [MagicMock()] * deps_before
    oracle.get_dependents.return_value = [MagicMock()] * deps_before
    oracle.compute_blast_radius.return_value = _MockBlastRadius(
        total_affected=br_before
    )
    return oracle


# ---------------------------------------------------------------------------
# VindicationResult dataclass
# ---------------------------------------------------------------------------

class TestVindicationResultFields:
    """Verify VindicationResult is a frozen dataclass with required fields."""

    def test_all_fields_present(self):
        result = VindicationResult(
            vindication_score=0.5,
            coupling_delta=-0.2,
            blast_radius_delta=-0.3,
            entropy_delta=-0.1,
            advisory="vindicating",
        )
        assert result.vindication_score == 0.5
        assert result.coupling_delta == -0.2
        assert result.blast_radius_delta == -0.3
        assert result.entropy_delta == -0.1
        assert result.advisory == "vindicating"

    def test_frozen(self):
        result = VindicationResult(
            vindication_score=0.0,
            coupling_delta=0.0,
            blast_radius_delta=0.0,
            entropy_delta=0.0,
            advisory="neutral",
        )
        with pytest.raises((AttributeError, TypeError)):
            result.vindication_score = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Improving patch -> positive vindication
# ---------------------------------------------------------------------------

class TestImprovingPatchPositiveVindication:
    """A patch that reduces coupling, blast radius, and complexity is vindicating."""

    def test_positive_score(self):
        # deps_before=10, br_before=20 -- big initial footprint
        oracle = _mock_oracle(deps_before=10, br_before=20)
        reflector = VindicationReflector(oracle=oracle)

        # After: coupling_after=5 (was 20=10+10), br_after=5, complexity drops
        result = reflector.reflect(
            target_files=["backend/foo.py"],
            coupling_after=5,
            blast_radius_after=5,
            complexity_after=50,
            complexity_before=100,
        )

        assert result.vindication_score > 0.2, (
            f"Expected vindicating score > 0.2, got {result.vindication_score}"
        )
        assert result.advisory == "vindicating"

    def test_deltas_are_negative_for_improvement(self):
        oracle = _mock_oracle(deps_before=10, br_before=20)
        reflector = VindicationReflector(oracle=oracle)

        result = reflector.reflect(
            target_files=["backend/foo.py"],
            coupling_after=5,
            blast_radius_after=5,
            complexity_after=50,
            complexity_before=100,
        )

        # All deltas should be negative (things got smaller/better)
        assert result.coupling_delta < 0
        assert result.blast_radius_delta < 0
        assert result.entropy_delta < 0


# ---------------------------------------------------------------------------
# Degrading patch -> negative vindication
# ---------------------------------------------------------------------------

class TestDegradingPatchNegativeVindication:
    """A patch that increases coupling, blast radius, and complexity is bad."""

    def test_negative_score(self):
        oracle = _mock_oracle(deps_before=5, br_before=10)
        reflector = VindicationReflector(oracle=oracle)

        # After: coupling doubles (was 10=5+5), br triples, complexity grows
        result = reflector.reflect(
            target_files=["backend/bar.py"],
            coupling_after=20,
            blast_radius_after=30,
            complexity_after=200,
            complexity_before=100,
        )

        assert result.vindication_score < 0, (
            f"Expected negative score, got {result.vindication_score}"
        )
        assert result.advisory in ("concerning", "warning"), (
            f"Expected 'concerning' or 'warning', got {result.advisory!r}"
        )

    def test_warning_for_severely_degrading_patch(self):
        """Extreme degradation should trigger 'warning' advisory."""
        oracle = _mock_oracle(deps_before=1, br_before=1)
        reflector = VindicationReflector(oracle=oracle)

        result = reflector.reflect(
            target_files=["backend/critical.py"],
            coupling_after=100,   # massive coupling increase
            blast_radius_after=100,
            complexity_after=1000,
            complexity_before=10,
        )

        # Score should be deeply negative (clamped at -1)
        assert result.vindication_score <= -0.5, (
            f"Expected score <= -0.5, got {result.vindication_score}"
        )
        assert result.advisory == "warning"


# ---------------------------------------------------------------------------
# Neutral patch -> score near 0
# ---------------------------------------------------------------------------

class TestNeutralPatch:
    """No change in coupling, blast radius, or complexity -> score ~0."""

    def test_score_near_zero(self):
        oracle = _mock_oracle(deps_before=5, br_before=10)
        reflector = VindicationReflector(oracle=oracle)

        # coupling_before = 5+5 = 10; br_before = 10; complexity unchanged
        result = reflector.reflect(
            target_files=["backend/stable.py"],
            coupling_after=10,       # exactly matches before (5+5)
            blast_radius_after=10,   # exactly matches before
            complexity_after=100,
            complexity_before=100,
        )

        assert abs(result.vindication_score) < 1e-6, (
            f"Expected near-zero score, got {result.vindication_score}"
        )
        assert result.advisory == "neutral"


# ---------------------------------------------------------------------------
# Oracle failure -> neutral result
# ---------------------------------------------------------------------------

class TestOracleFailureReturnsNeutral:
    """Any oracle exception must produce a safe neutral result."""

    def test_get_dependencies_raises(self):
        oracle = MagicMock()
        oracle.get_dependencies.side_effect = RuntimeError("oracle unavailable")
        reflector = VindicationReflector(oracle=oracle)

        result = reflector.reflect(
            target_files=["backend/foo.py"],
            coupling_after=5,
            blast_radius_after=5,
            complexity_after=50,
            complexity_before=100,
        )

        assert result.vindication_score == 0.0
        assert result.advisory == "neutral"

    def test_compute_blast_radius_raises(self):
        oracle = MagicMock()
        oracle.get_dependencies.return_value = [MagicMock()] * 3
        oracle.get_dependents.return_value = [MagicMock()] * 3
        oracle.compute_blast_radius.side_effect = ValueError("blast radius broken")
        reflector = VindicationReflector(oracle=oracle)

        result = reflector.reflect(
            target_files=["backend/foo.py"],
            coupling_after=5,
            blast_radius_after=5,
            complexity_after=50,
            complexity_before=100,
        )

        assert result.vindication_score == 0.0
        assert result.advisory == "neutral"

    def test_neutral_zero_deltas(self):
        """Neutral result from failure has all-zero deltas."""
        oracle = MagicMock()
        oracle.get_dependencies.side_effect = Exception("broken")
        reflector = VindicationReflector(oracle=oracle)

        result = reflector.reflect(
            target_files=["backend/foo.py"],
            coupling_after=5,
            blast_radius_after=5,
            complexity_after=50,
            complexity_before=100,
        )

        assert result.coupling_delta == 0.0
        assert result.blast_radius_delta == 0.0
        assert result.entropy_delta == 0.0


# ---------------------------------------------------------------------------
# Score clamping and precision
# ---------------------------------------------------------------------------

class TestScoreClampingAndPrecision:
    """Score is clamped to [-1, 1] and rounded to 4 decimal places."""

    def test_score_within_bounds(self):
        oracle = _mock_oracle(deps_before=1, br_before=1)
        reflector = VindicationReflector(oracle=oracle)

        result = reflector.reflect(
            target_files=["backend/x.py"],
            coupling_after=0,
            blast_radius_after=0,
            complexity_after=0,
            complexity_before=1,
        )

        assert -1.0 <= result.vindication_score <= 1.0

    def test_score_rounded_to_4_decimal_places(self):
        oracle = _mock_oracle(deps_before=3, br_before=7)
        reflector = VindicationReflector(oracle=oracle)

        result = reflector.reflect(
            target_files=["backend/y.py"],
            coupling_after=4,
            blast_radius_after=9,
            complexity_after=110,
            complexity_before=100,
        )

        # Verify rounding: score should have at most 4 decimal places
        rounded = round(result.vindication_score, 4)
        assert result.vindication_score == rounded


# ---------------------------------------------------------------------------
# Multi-file target
# ---------------------------------------------------------------------------

class TestMultiFileTarget:
    """reflect() handles multiple target files."""

    def test_multi_file_aggregates_coupling_before(self):
        """coupling_before sums across all target files."""
        oracle = MagicMock()
        # Each call returns 3 deps + 3 dependents = 6 per file
        oracle.get_dependencies.return_value = [MagicMock()] * 3
        oracle.get_dependents.return_value = [MagicMock()] * 3
        oracle.compute_blast_radius.return_value = _MockBlastRadius(total_affected=5)
        reflector = VindicationReflector(oracle=oracle)

        # 2 target files -> coupling_before = 6+6 = 12
        result = reflector.reflect(
            target_files=["backend/a.py", "backend/b.py"],
            coupling_after=12,     # same as before -> delta=0
            blast_radius_after=5,  # same as before -> delta=0
            complexity_after=100,
            complexity_before=100,
        )

        # coupling_delta and br_delta should be ~0 -> score ~0
        assert abs(result.coupling_delta) < 1e-9
        assert result.advisory == "neutral"
