"""Tests for TestWatcher — pytest polling and stable failure detection.

Validates that the TestWatcher correctly:
1. Parses pytest output for FAILED lines
2. Returns empty list on exit_code == 0
3. Requires two consecutive failures for stability
4. Resets streak on passing tests
5. Runs pytest as a subprocess
6. Extracts file paths from test IDs
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intent.test_watcher import (
    TestFailure,
    TestWatcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_PYTEST_OUTPUT = textwrap.dedent("""\
    tests/test_utils.py::test_edge_case PASSED
    tests/test_utils.py::test_boundary PASSED
    FAILED tests/test_core.py::test_parse - AssertionError: expected 3, got 4
    FAILED tests/test_net.py::test_timeout - TimeoutError: connection timed out
    2 failed, 2 passed in 1.23s
""")


# ---------------------------------------------------------------------------
# 1. test_parse_pytest_output_detects_failures
# ---------------------------------------------------------------------------

class TestParsePytestOutputDetectsFailures:
    """Parsing real FAILED lines should yield TestFailure objects."""

    def test_detects_two_failures(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        failures = watcher.parse_pytest_output(_SAMPLE_PYTEST_OUTPUT, exit_code=1)
        assert len(failures) == 2

        ids = {f.test_id for f in failures}
        assert "tests/test_core.py::test_parse" in ids
        assert "tests/test_net.py::test_timeout" in ids

    def test_failure_fields_populated(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        failures = watcher.parse_pytest_output(_SAMPLE_PYTEST_OUTPUT, exit_code=1)
        core_fail = next(f for f in failures if "test_core" in f.test_id)
        assert core_fail.file_path == "tests/test_core.py"
        assert "AssertionError" in core_fail.error_text


# ---------------------------------------------------------------------------
# 2. test_parse_pytest_output_no_failures
# ---------------------------------------------------------------------------

class TestParsePytestOutputNoFailures:
    """exit_code == 0 should always produce an empty failure list."""

    def test_returns_empty_on_exit_code_zero(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        # Even if the output *looks* like failures, exit_code=0 overrides.
        failures = watcher.parse_pytest_output(_SAMPLE_PYTEST_OUTPUT, exit_code=0)
        assert failures == []


# ---------------------------------------------------------------------------
# 3. test_stability_requires_two_consecutive_failures
# ---------------------------------------------------------------------------

class TestStabilityRequiresTwoConsecutiveFailures:
    """A test must fail in two consecutive runs to be declared stable."""

    def test_first_failure_not_stable(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        failures = [
            TestFailure(
                test_id="tests/test_core.py::test_parse",
                file_path="tests/test_core.py",
                error_text="AssertionError: bad",
            ),
        ]
        signals = watcher.process_failures(failures)
        assert len(signals) == 0

    def test_second_consecutive_failure_is_stable(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        failures = [
            TestFailure(
                test_id="tests/test_core.py::test_parse",
                file_path="tests/test_core.py",
                error_text="AssertionError: bad",
            ),
        ]
        # First run — not stable yet
        watcher.process_failures(failures)
        # Second run — same test fails again → stable
        signals = watcher.process_failures(failures)
        assert len(signals) == 1
        assert signals[0].stable is True
        assert signals[0].source == "intent:test_failure"


# ---------------------------------------------------------------------------
# 4. test_stability_resets_on_pass
# ---------------------------------------------------------------------------

class TestStabilityResetsOnPass:
    """Streak resets when a test passes, so subsequent failure is not stable."""

    def test_pass_resets_streak(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        fail = [
            TestFailure(
                test_id="tests/test_core.py::test_parse",
                file_path="tests/test_core.py",
                error_text="AssertionError: bad",
            ),
        ]
        # Run 1: fail → streak = 1
        watcher.process_failures(fail)
        # Run 2: pass (empty list) → streak resets
        watcher.process_failures([])
        # Run 3: fail again → streak = 1 (not 2), so NOT stable
        signals = watcher.process_failures(fail)
        assert len(signals) == 0


# ---------------------------------------------------------------------------
# 5. test_run_pytest_subprocess
# ---------------------------------------------------------------------------

class TestRunPytestSubprocess:
    """Actually invoke pytest on a trivial test file and verify exit_code."""

    @pytest.mark.asyncio
    async def test_trivial_passing_test(self, tmp_path: Path) -> None:
        # Create a minimal test file that passes
        test_file = tmp_path / "test_trivial.py"
        test_file.write_text("def test_one():\n    assert 1 + 1 == 2\n")

        watcher = TestWatcher(
            repo="jarvis",
            test_dir=str(tmp_path),
            repo_path=str(tmp_path),
        )
        output, exit_code = await watcher.run_pytest()
        assert exit_code == 0
        assert "passed" in output.lower()


# ---------------------------------------------------------------------------
# 6. test_extracts_file_path_from_test_id
# ---------------------------------------------------------------------------

class TestExtractsFilePathFromTestId:
    """extract_file() splits on :: and returns the first component."""

    def test_simple_test_id(self) -> None:
        result = TestWatcher.extract_file("tests/test_utils.py::test_edge_case")
        assert result == "tests/test_utils.py"

    def test_class_method_test_id(self) -> None:
        result = TestWatcher.extract_file(
            "tests/test_utils.py::TestClass::test_method"
        )
        assert result == "tests/test_utils.py"
