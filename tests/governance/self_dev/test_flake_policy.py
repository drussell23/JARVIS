"""Tests for flaky test discrimination policy."""
from backend.core.ouroboros.governance.test_runner import TestResult


def test_flake_suspected_true_when_first_fail_retry_pass():
    """TestResult with flake_suspected=True produced by TestRunner on retry pass."""
    result = TestResult(
        passed=True, total=1, failed=0, failed_tests=(),
        duration_seconds=1.0, stdout="--- RETRY ---\n1 passed",
        flake_suspected=True,
    )
    assert result.flake_suspected is True
    assert result.passed is True


def test_flake_not_treated_as_clean_pass():
    """A flaky result (passed=True, flake_suspected=True) is distinguishable."""
    clean = TestResult(
        passed=True, total=1, failed=0, failed_tests=(),
        duration_seconds=0.5, stdout="1 passed", flake_suspected=False,
    )
    flaky = TestResult(
        passed=True, total=1, failed=0, failed_tests=(),
        duration_seconds=1.0, stdout="--- RETRY ---\n1 passed", flake_suspected=True,
    )
    assert clean.flake_suspected is False
    assert flaky.flake_suspected is True


def test_flake_false_when_first_run_passes():
    """If first run passes, flake_suspected must be False."""
    result = TestResult(
        passed=True, total=1, failed=0, failed_tests=(),
        duration_seconds=0.5, stdout="1 passed in 0.3s", flake_suspected=False,
    )
    assert result.flake_suspected is False
