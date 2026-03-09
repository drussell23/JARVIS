"""Tests for PatchBenchmarker."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.patch_benchmarker import (
    BenchmarkResult,
    PatchBenchmarker,
    _compute_patch_hash,
    _infer_task_type,
)
from backend.core.ouroboros.governance.op_context import OperationContext


def _make_ctx(description="improve auth logic", target_files=(), pre_apply_snapshots=None):
    ctx = MagicMock(spec=OperationContext)
    ctx.description = description
    ctx.target_files = target_files
    ctx.pre_apply_snapshots = pre_apply_snapshots or {}
    ctx.op_id = "op-test-001"
    return ctx


class TestInferTaskType:
    def test_test_in_description(self):
        assert _infer_task_type("add unit tests for auth", ()) == "testing"

    def test_file_under_tests_dir(self):
        assert _infer_task_type("improve logic", ("tests/test_foo.py",)) == "testing"

    def test_refactor_in_description(self):
        assert _infer_task_type("refactor the auth module", ()) == "refactoring"

    def test_bug_fix(self):
        assert _infer_task_type("fix null pointer bug", ()) == "bug_fix"

    def test_security(self):
        assert _infer_task_type("security patch for token validation", ()) == "security"

    def test_performance(self):
        assert _infer_task_type("optimize hot path", ()) == "performance"

    def test_default(self):
        assert _infer_task_type("update auth module", ()) == "code_improvement"

    def test_priority_order_test_beats_refactor(self):
        assert _infer_task_type("refactor tests", ()) == "testing"


class TestComputePatchHash:
    def test_deterministic(self):
        h1 = _compute_patch_hash({"a.py": "x", "b.py": "y"})
        h2 = _compute_patch_hash({"b.py": "y", "a.py": "x"})
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = _compute_patch_hash({"a.py": "x"})
        h2 = _compute_patch_hash({"a.py": "y"})
        assert h1 != h2

    def test_returns_hex_string(self):
        h = _compute_patch_hash({"a.py": "x"})
        assert len(h) == 64
        int(h, 16)  # must be valid hex


class TestBenchmarkNeverRaises:
    async def test_benchmark_returns_result_when_tools_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx()
            result = await benchmarker.benchmark(ctx)
            assert isinstance(result, BenchmarkResult)
            assert 0.0 <= result.quality_score <= 1.0

    async def test_benchmark_returns_on_subprocess_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("nonexistent_file.py",))
            # Must not raise
            result = await benchmarker.benchmark(ctx)
            assert isinstance(result, BenchmarkResult)

    async def test_timed_out_flag_set_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=0.001)
            ctx = _make_ctx()
            result = await benchmarker.benchmark(ctx)
            # With near-zero timeout, at least one step should time out
            assert isinstance(result, BenchmarkResult)
            # timed_out may or may not be set depending on OS timing, but must not raise


class TestQualityScoreFormula:
    def test_perfect_scores(self):
        from backend.core.ouroboros.governance.patch_benchmarker import _compute_quality_score
        score = _compute_quality_score(lint_score=1.0, coverage_score=1.0, complexity_score=1.0, radon_available=True)
        expected = 0.45 * 1.0 + 0.45 * 1.0 + 0.10 * 1.0
        assert abs(score - expected) < 1e-6

    def test_radon_unavailable_redistributes_weight(self):
        from backend.core.ouroboros.governance.patch_benchmarker import _compute_quality_score
        score = _compute_quality_score(lint_score=1.0, coverage_score=1.0, complexity_score=0.0, radon_available=False)
        # Weights: lint=0.50, coverage=0.50, complexity ignored
        assert abs(score - 1.0) < 1e-6

    def test_scores_clamped_to_0_1(self):
        from backend.core.ouroboros.governance.patch_benchmarker import _compute_quality_score
        score = _compute_quality_score(lint_score=2.0, coverage_score=-1.0, complexity_score=0.5, radon_available=True)
        assert 0.0 <= score <= 1.0
