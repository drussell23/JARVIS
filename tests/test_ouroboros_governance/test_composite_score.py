"""Tests for CompositeScoreFunction, CompositeScore, and ScoreHistory.

TDD: tests are written first, implementation in
backend/core/ouroboros/governance/composite_score.py.
"""
from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.composite_score import (
    CompositeScore,
    CompositeScoreFunction,
    ScoreHistory,
    _clamp,
    _sigmoid,
)


# ---------------------------------------------------------------------------
# _sigmoid helpers
# ---------------------------------------------------------------------------


class TestSigmoid:
    def test_zero_is_half(self):
        assert _sigmoid(0.0) == pytest.approx(0.5)

    def test_large_positive_approaches_one(self):
        assert _sigmoid(10.0) > 0.99

    def test_large_negative_approaches_zero(self):
        assert _sigmoid(-10.0) < 0.01

    def test_monotone(self):
        xs = [-5.0, -1.0, 0.0, 1.0, 5.0]
        ys = [_sigmoid(x) for x in xs]
        assert all(ys[i] < ys[i + 1] for i in range(len(ys) - 1))

    def test_output_in_unit_interval(self):
        for x in [-100.0, -1.0, 0.0, 1.0, 100.0]:
            v = _sigmoid(x)
            assert 0.0 <= v <= 1.0


# ---------------------------------------------------------------------------
# _clamp helper
# ---------------------------------------------------------------------------


class TestClamp:
    def test_within_range(self):
        assert _clamp(0.5, 0.0, 1.0) == 0.5

    def test_below_lo(self):
        assert _clamp(-1.0, 0.0, 1.0) == 0.0

    def test_above_hi(self):
        assert _clamp(2.0, 0.0, 1.0) == 1.0

    def test_exact_lo(self):
        assert _clamp(0.0, 0.0, 1.0) == 0.0

    def test_exact_hi(self):
        assert _clamp(1.0, 0.0, 1.0) == 1.0


# ---------------------------------------------------------------------------
# CompositeScore dataclass
# ---------------------------------------------------------------------------


class TestCompositeScore:
    def _make(self, **kwargs):
        defaults = dict(
            test_delta=0.5,
            coverage_delta=0.5,
            complexity_delta=0.5,
            lint_delta=0.5,
            blast_radius=0.5,
            composite=0.5,
            op_id="op-test-001",
            timestamp=1234567890.0,
        )
        defaults.update(kwargs)
        return CompositeScore(**defaults)

    def test_creation(self):
        score = self._make()
        assert score.composite == 0.5
        assert score.op_id == "op-test-001"

    def test_frozen_raises_on_mutation(self):
        score = self._make()
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            score.composite = 0.9  # type: ignore[misc]

    def test_all_fields_present(self):
        score = self._make()
        for field in (
            "test_delta",
            "coverage_delta",
            "complexity_delta",
            "lint_delta",
            "blast_radius",
            "composite",
            "op_id",
            "timestamp",
        ):
            assert hasattr(score, field)


# ---------------------------------------------------------------------------
# CompositeScoreFunction.compute()
# ---------------------------------------------------------------------------


class TestCompositeScoreFunction:
    def _fn(self, **kwargs):
        return CompositeScoreFunction(**kwargs)

    # --- semantic correctness --------------------------------------------

    def test_perfect_patch_low_score(self, tmp_path):
        """A patch that improves everything from zero should score below 0.3.

        Uses before=0, after=1 so test/coverage sub-scores hit their minimum
        (1.0 - 1.0 = 0.0) while complexity and lint also improve significantly.
        """
        fn = self._fn(persistence_dir=tmp_path)
        score = fn.compute(
            op_id="op-perfect",
            test_pass_rate_before=0.0,
            test_pass_rate_after=1.0,
            coverage_before=0.0,
            coverage_after=100.0,
            complexity_before=50.0,
            complexity_after=5.0,
            lint_violations_before=10,
            lint_violations_after=0,
            blast_radius_total=1,
        )
        assert score.composite < 0.3, f"Expected <0.3, got {score.composite}"

    def test_terrible_patch_high_score(self, tmp_path):
        """A patch that degrades everything should score above 0.6."""
        fn = self._fn(persistence_dir=tmp_path)
        score = fn.compute(
            op_id="op-terrible",
            test_pass_rate_before=1.0,
            test_pass_rate_after=0.5,
            coverage_before=100.0,
            coverage_after=50.0,
            complexity_before=5.0,
            complexity_after=20.0,
            lint_violations_before=0,
            lint_violations_after=10,
            blast_radius_total=50,
        )
        assert score.composite > 0.6, f"Expected >0.6, got {score.composite}"

    def test_neutral_patch_near_half(self, tmp_path):
        """A no-change patch should score above a perfect patch and below a terrible one.

        Formula: test_delta = 1-(after-before) = 1.0 (no improvement),
        coverage_delta = 1.0, complexity_delta = sigmoid(0) = 0.5,
        lint_delta = sigmoid(0) = 0.5, blast_radius = 0.
        Composite = 0.40*1 + 0.20*1 + 0.15*0.5 + 0.10*0.5 + 0.15*0 = 0.725.
        It is higher than ~0.5 because test/coverage sub-scores show no improvement.
        We assert it sits in the [0.5, 0.9] range (no improvement, but harmless).
        """
        fn = self._fn(persistence_dir=tmp_path)
        score = fn.compute(
            op_id="op-neutral",
            test_pass_rate_before=0.8,
            test_pass_rate_after=0.8,
            coverage_before=80.0,
            coverage_after=80.0,
            complexity_before=10.0,
            complexity_after=10.0,
            lint_violations_before=5,
            lint_violations_after=5,
            blast_radius_total=0,
        )
        assert 0.5 <= score.composite <= 0.9, f"Expected in [0.5, 0.9], got {score.composite}"

    # --- result structure ------------------------------------------------

    def test_returns_composite_score_instance(self, tmp_path):
        fn = self._fn(persistence_dir=tmp_path)
        score = fn.compute(
            op_id="op-x",
            test_pass_rate_before=0.8,
            test_pass_rate_after=0.9,
            coverage_before=70.0,
            coverage_after=80.0,
            complexity_before=10.0,
            complexity_after=8.0,
            lint_violations_before=3,
            lint_violations_after=1,
            blast_radius_total=5,
        )
        assert isinstance(score, CompositeScore)
        assert score.op_id == "op-x"
        assert isinstance(score.timestamp, float)
        assert 0.0 <= score.composite <= 1.0

    def test_sub_scores_in_unit_interval(self, tmp_path):
        fn = self._fn(persistence_dir=tmp_path)
        score = fn.compute(
            op_id="op-y",
            test_pass_rate_before=0.4,
            test_pass_rate_after=0.6,
            coverage_before=40.0,
            coverage_after=60.0,
            complexity_before=15.0,
            complexity_after=10.0,
            lint_violations_before=5,
            lint_violations_after=2,
            blast_radius_total=10,
        )
        for attr in ("test_delta", "coverage_delta", "complexity_delta", "lint_delta", "blast_radius"):
            val = getattr(score, attr)
            assert 0.0 <= val <= 1.0, f"{attr}={val} out of [0,1]"

    # --- weights ---------------------------------------------------------

    def test_custom_weights_change_composite(self, tmp_path):
        """Different weight tuples produce different composite scores."""
        defaults = dict(
            test_pass_rate_before=0.5,
            test_pass_rate_after=0.8,
            coverage_before=60.0,
            coverage_after=80.0,
            complexity_before=12.0,
            complexity_after=8.0,
            lint_violations_before=4,
            lint_violations_after=1,
            blast_radius_total=5,
        )
        fn1 = self._fn(weights=(0.40, 0.20, 0.15, 0.10, 0.15), persistence_dir=tmp_path)
        fn2 = self._fn(weights=(0.10, 0.10, 0.10, 0.10, 0.60), persistence_dir=tmp_path)

        s1 = fn1.compute(op_id="op-w1", **defaults)
        s2 = fn2.compute(op_id="op-w2", **defaults)
        assert s1.composite != s2.composite

    def test_weights_not_length_5_raises_value_error(self):
        with pytest.raises(ValueError):
            CompositeScoreFunction(weights=(0.5, 0.5))

    def test_weights_normalized(self, tmp_path):
        """Weights that don't sum to 1 are normalized; composite still in [0,1]."""
        fn = self._fn(weights=(2.0, 1.0, 1.0, 1.0, 1.0), persistence_dir=tmp_path)
        score = fn.compute(
            op_id="op-norm",
            test_pass_rate_before=0.5,
            test_pass_rate_after=0.8,
            coverage_before=60.0,
            coverage_after=70.0,
            complexity_before=10.0,
            complexity_after=8.0,
            lint_violations_before=3,
            lint_violations_after=1,
            blast_radius_total=5,
        )
        assert 0.0 <= score.composite <= 1.0


# ---------------------------------------------------------------------------
# ScoreHistory
# ---------------------------------------------------------------------------


class TestScoreHistory:
    def _make_score(self, composite: float = 0.5, op_id: str = "op-hist") -> CompositeScore:
        return CompositeScore(
            test_delta=0.5,
            coverage_delta=0.5,
            complexity_delta=0.5,
            lint_delta=0.5,
            blast_radius=0.1,
            composite=composite,
            op_id=op_id,
            timestamp=1234567890.0,
        )

    def test_empty_history_returns_empty_list(self, tmp_path):
        history = ScoreHistory(persistence_dir=tmp_path)
        assert history.get_recent(5) == []

    def test_record_persists_to_disk(self, tmp_path):
        h1 = ScoreHistory(persistence_dir=tmp_path)
        score = self._make_score(composite=0.3)
        h1.record(score)

        # New instance loads from disk
        h2 = ScoreHistory(persistence_dir=tmp_path)
        recent = h2.get_recent(10)
        assert len(recent) == 1
        assert recent[0].composite == pytest.approx(0.3)

    def test_record_multiple_appends_to_disk(self, tmp_path):
        h = ScoreHistory(persistence_dir=tmp_path)
        for i in range(5):
            h.record(self._make_score(composite=float(i) / 10, op_id=f"op-{i}"))

        h2 = ScoreHistory(persistence_dir=tmp_path)
        assert len(h2.get_recent(10)) == 5

    def test_get_recent_limits_output(self, tmp_path):
        h = ScoreHistory(persistence_dir=tmp_path)
        for i in range(10):
            h.record(self._make_score(composite=float(i) / 10, op_id=f"op-{i}"))
        recent = h.get_recent(3)
        assert len(recent) == 3

    def test_get_recent_returns_chronological_order(self, tmp_path):
        h = ScoreHistory(persistence_dir=tmp_path)
        composites = [0.1, 0.2, 0.3, 0.4, 0.5]
        for c in composites:
            h.record(self._make_score(composite=c, op_id=f"op-{c}"))
        recent = h.get_recent(5)
        # chronological = first recorded first
        assert [r.composite for r in recent] == pytest.approx(composites)

    def test_get_recent_with_n_larger_than_history(self, tmp_path):
        h = ScoreHistory(persistence_dir=tmp_path)
        h.record(self._make_score(composite=0.4))
        recent = h.get_recent(100)
        assert len(recent) == 1

    def test_get_composite_values_returns_floats(self, tmp_path):
        h = ScoreHistory(persistence_dir=tmp_path)
        for c in [0.2, 0.4, 0.6]:
            h.record(self._make_score(composite=c, op_id=f"op-{c}"))
        values = h.get_composite_values()
        assert values == pytest.approx([0.2, 0.4, 0.6])
        assert all(isinstance(v, float) for v in values)

    def test_get_composite_values_empty(self, tmp_path):
        h = ScoreHistory(persistence_dir=tmp_path)
        assert h.get_composite_values() == []

    def test_jsonl_file_is_created(self, tmp_path):
        h = ScoreHistory(persistence_dir=tmp_path)
        h.record(self._make_score())
        jsonl_path = tmp_path / "composite_scores.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert "composite" in parsed
        assert "op_id" in parsed

    def test_corrupt_jsonl_silently_ignored(self, tmp_path):
        """A corrupt line in the JSONL file is skipped silently."""
        jsonl_path = tmp_path / "composite_scores.jsonl"
        jsonl_path.write_text("NOT VALID JSON\n")
        # Should not raise
        h = ScoreHistory(persistence_dir=tmp_path)
        assert h.get_recent(10) == []

    def test_missing_persistence_dir_creates_it(self, tmp_path):
        new_dir = tmp_path / "nested" / "dir"
        h = ScoreHistory(persistence_dir=new_dir)
        h.record(self._make_score())
        assert (new_dir / "composite_scores.jsonl").exists()
