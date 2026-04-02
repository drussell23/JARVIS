"""Tests for Ouroboros VERIFY regression gate."""
import pytest
from pathlib import Path


def _make_benchmark_result(**overrides):
    """Build a BenchmarkResult with sensible defaults."""
    from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
    defaults = dict(
        pass_rate=1.0,
        lint_violations=0,
        coverage_pct=85.0,
        complexity_delta=0.0,
        patch_hash="abc123",
        quality_score=0.9,
        task_type="code_improvement",
        timed_out=False,
        error=None,
    )
    defaults.update(overrides)
    return BenchmarkResult(**defaults)


def test_verify_all_pass():
    """All metrics within thresholds → None (continue to COMPLETE)."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result()
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is None


def test_verify_pass_rate_failure():
    """pass_rate < 1.0 → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(pass_rate=0.85)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "pass_rate" in error


def test_verify_coverage_regression():
    """coverage drops > 5% from baseline → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(coverage_pct=75.0)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "coverage" in error.lower()


def test_verify_coverage_no_baseline():
    """No baseline coverage → skip coverage check, pass."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(coverage_pct=10.0)
    error = enforce_verify_thresholds(result, baseline_coverage=None)
    assert error is None


def test_verify_complexity_spike():
    """complexity_delta > 2.0 → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(complexity_delta=3.5)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "complexity" in error.lower()


def test_verify_lint_cap():
    """lint_violations > 5 → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(lint_violations=8)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "lint" in error.lower()


def test_verify_timed_out():
    """timed_out=True → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(timed_out=True)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "timed" in error.lower()


def test_verify_error_set():
    """error field set → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(error="pytest crashed")
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "error" in error.lower() or "pytest" in error.lower()


def test_verify_zero_tests_passes():
    """0 tests collected (pass_rate=1.0) → pass."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(pass_rate=1.0)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is None


def test_rollback_restores_files(tmp_path):
    """rollback_files restores from snapshots and deletes new files."""
    from backend.core.ouroboros.governance.verify_gate import rollback_files

    existing = tmp_path / "existing.py"
    existing.write_text("modified content")
    snapshots = {"existing.py": "original content"}

    new_file = tmp_path / "new_module.py"
    new_file.write_text("new code")

    target_files = ["existing.py", "new_module.py"]

    rollback_files(
        pre_apply_snapshots=snapshots,
        target_files=target_files,
        repo_root=tmp_path,
    )

    assert existing.read_text() == "original content"
    assert not new_file.exists(), "New file should be deleted on rollback"
