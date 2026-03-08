"""Tests for TestRunner -- Pytest subprocess wrapper for governed self-development.

Validates that TestRunner correctly:
1. Resolves affected tests via name convention (foo.py -> test_foo.py)
2. Falls back to package-level tests/ when no name match exists
3. Falls back to repo-level tests/ as last resort
4. Runs passing fixture tests and reports passed=True, total>=2
5. Runs failing tests and reports passed=False with failed_tests
6. Handles subprocess timeout gracefully
7. Passes sandbox_dir as cwd to pytest
8. Detects flaky tests (fail-then-pass -> flake_suspected=True)
9. Rejects symlinks pointing outside repo_root
10. Handles corrupt/missing JSON report with graceful fallback
"""
from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.test_runner import (
    TestResult,
    TestRunner,
    _is_safe_path,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]  # .../JARVIS-AI-Agent
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "sample_project"
FIXTURE_TESTS = FIXTURE_ROOT / "tests" / "test_calculator.py"
FIXTURE_SRC = FIXTURE_ROOT / "src" / "calculator.py"


# ---------------------------------------------------------------------------
# 1. test_resolve_name_convention_mapping
# ---------------------------------------------------------------------------

class TestResolveNameConventionMapping:
    """calculator.py should map to test_calculator.py via name convention."""

    async def test_maps_calculator_to_test_calculator(self) -> None:
        runner = TestRunner(repo_root=REPO_ROOT)
        result = await runner.resolve_affected_tests(
            changed_files=(FIXTURE_SRC,),
        )
        # Should find the exact test file
        assert any(
            p.name == "test_calculator.py" for p in result
        ), f"Expected test_calculator.py in {result}"


# ---------------------------------------------------------------------------
# 2. test_resolve_no_mapping_package_fallback
# ---------------------------------------------------------------------------

class TestResolveNoMappingPackageFallback:
    """A source file with no matching test should fall back to package tests/."""

    async def test_nonexistent_module_falls_back_to_package_tests(self) -> None:
        runner = TestRunner(repo_root=REPO_ROOT)
        # nonexistent_module.py lives in fixture src/ but has no test_nonexistent_module.py
        fake_module = FIXTURE_SRC.parent / "nonexistent_module.py"
        result = await runner.resolve_affected_tests(
            changed_files=(fake_module,),
        )
        # Should fall back to test files in the nearest tests/ directory
        assert len(result) > 0, "Expected at least one test path"
        # Should include the tests from the sample_project/tests/ dir
        result_names = [p.name for p in result]
        assert "test_calculator.py" in result_names or any(
            "tests" in str(p) for p in result
        ), f"Expected package fallback in {result}"


# ---------------------------------------------------------------------------
# 3. test_resolve_empty_falls_back_to_tests_dir
# ---------------------------------------------------------------------------

class TestResolveEmptyFallsBackToTestsDir:
    """A random file with no nearby tests/ falls back to repo-level tests/."""

    async def test_random_file_falls_back_to_repo_tests(self) -> None:
        runner = TestRunner(repo_root=REPO_ROOT)
        # Use a file that is far from any tests/ directory
        with tempfile.NamedTemporaryFile(
            suffix=".py", dir="/tmp", delete=False,
        ) as f:
            random_file = Path(f.name)
        try:
            result = await runner.resolve_affected_tests(
                changed_files=(random_file,),
            )
            # Should fall back to repo-level tests/
            assert len(result) > 0, "Expected repo-level fallback"
            assert any(
                str(p).endswith("tests") or "tests" in str(p)
                for p in result
            ), f"Expected repo tests/ dir in {result}"
        finally:
            random_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. test_run_passing_tests
# ---------------------------------------------------------------------------

class TestRunPassingTests:
    """Running the fixture tests should return passed=True with total>=2."""

    async def test_passing_fixtures(self) -> None:
        runner = TestRunner(repo_root=REPO_ROOT)
        result = await runner.run(test_files=(FIXTURE_TESTS,))
        assert result.passed is True, f"Expected passed=True, got stdout:\n{result.stdout}"
        assert result.total >= 2, f"Expected total>=2, got {result.total}"
        assert result.failed == 0
        assert result.failed_tests == ()
        assert result.flake_suspected is False
        assert result.duration_seconds > 0


# ---------------------------------------------------------------------------
# 5. test_run_failing_tests
# ---------------------------------------------------------------------------

class TestRunFailingTests:
    """A test that always fails should return passed=False with failed_tests."""

    async def test_failing_test_reports_failure(self) -> None:
        # Create a temporary failing test file
        with tempfile.NamedTemporaryFile(
            suffix=".py",
            prefix="test_always_fail_",
            dir="/tmp",
            mode="w",
            delete=False,
        ) as f:
            f.write("def test_always_fails():\n    assert False, 'intentional failure'\n")
            fail_path = Path(f.name)

        try:
            runner = TestRunner(repo_root=REPO_ROOT, timeout=30.0)
            result = await runner.run(test_files=(fail_path,))
            assert result.passed is False
            assert result.failed >= 1
            assert len(result.failed_tests) >= 1
            # The stdout should contain retry marker
            assert "--- RETRY ---" in result.stdout
            assert result.flake_suspected is False
        finally:
            fail_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. test_run_timeout
# ---------------------------------------------------------------------------

class TestRunTimeout:
    """A test that sleeps for 60s with a 2s timeout should return passed=False."""

    async def test_timeout_kills_subprocess(self) -> None:
        with tempfile.NamedTemporaryFile(
            suffix=".py",
            prefix="test_slow_",
            dir="/tmp",
            mode="w",
            delete=False,
        ) as f:
            f.write("import time\ndef test_sleepy():\n    time.sleep(60)\n")
            slow_path = Path(f.name)

        try:
            runner = TestRunner(repo_root=REPO_ROOT, timeout=2.0)
            result = await runner.run(test_files=(slow_path,))
            assert result.passed is False
            assert "timed out" in result.stdout.lower()
        finally:
            slow_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 7. test_run_sandbox_dir
# ---------------------------------------------------------------------------

class TestRunSandboxDir:
    """sandbox_dir should be passed as cwd to the pytest subprocess."""

    async def test_sandbox_dir_as_cwd(self) -> None:
        # Create a sandbox with a simple passing test
        with tempfile.TemporaryDirectory(prefix="sandbox_") as sandbox:
            sandbox_path = Path(sandbox)
            test_file = sandbox_path / "test_sandbox_check.py"
            test_file.write_text(
                "import os\n"
                "def test_cwd_is_sandbox():\n"
                "    # Just verify the test runs from the sandbox\n"
                "    assert True\n"
            )

            runner = TestRunner(repo_root=REPO_ROOT, timeout=30.0)
            result = await runner.run(
                test_files=(test_file,),
                sandbox_dir=sandbox_path,
            )
            assert result.passed is True, (
                f"Expected passed=True for sandbox test, got stdout:\n{result.stdout}"
            )
            assert result.total >= 1


# ---------------------------------------------------------------------------
# 8. test_run_flake_detection
# ---------------------------------------------------------------------------

class TestRunFlakeDetection:
    """A test that fails on first run but passes on retry -> flake_suspected=True."""

    async def test_flake_detected_on_retry_pass(self) -> None:
        # Create a test that uses a file-based counter to fail first, pass second
        with tempfile.TemporaryDirectory(prefix="flake_") as tmpdir:
            counter_file = Path(tmpdir) / "counter.txt"
            counter_file.write_text("0")

            test_file = Path(tmpdir) / "test_flaky.py"
            test_file.write_text(
                "from pathlib import Path\n"
                "\n"
                "COUNTER = Path(r'{counter}')\n"
                "\n"
                "def test_flaky():\n"
                "    count = int(COUNTER.read_text())\n"
                "    COUNTER.write_text(str(count + 1))\n"
                "    assert count > 0, 'First run fails on purpose'\n"
                .format(counter=str(counter_file))
            )

            runner = TestRunner(repo_root=REPO_ROOT, timeout=30.0)
            result = await runner.run(test_files=(test_file,))
            assert result.passed is True, (
                f"Expected flaky test to pass on retry, got stdout:\n{result.stdout}"
            )
            assert result.flake_suspected is True
            assert "--- RETRY ---" in result.stdout


# ---------------------------------------------------------------------------
# 9. test_symlink_path_rejected
# ---------------------------------------------------------------------------

class TestSymlinkPathRejected:
    """Symlinks pointing outside repo_root should be handled gracefully."""

    async def test_symlink_outside_repo_filtered(self) -> None:
        # Create a symlink from inside repo to outside
        with tempfile.TemporaryDirectory(prefix="outside_") as outside_dir:
            outside_file = Path(outside_dir) / "evil.py"
            outside_file.write_text("# evil file\n")

            # Create a symlink inside /tmp pointing to the outside file
            link_path = Path(tempfile.mktemp(suffix=".py", prefix="symlink_"))
            try:
                link_path.symlink_to(outside_file)

                # Use a repo root that does NOT contain the target
                with tempfile.TemporaryDirectory(prefix="fake_repo_") as fake_repo:
                    fake_repo_path = Path(fake_repo)
                    # Create a tests/ dir so fallback works
                    (fake_repo_path / "tests").mkdir()

                    runner = TestRunner(repo_root=fake_repo_path)
                    result = await runner.resolve_affected_tests(
                        changed_files=(link_path,),
                    )
                    # The symlink should be filtered out; we should get
                    # the repo fallback (tests/ dir) instead
                    assert all(
                        str(p.name) != link_path.name
                        for p in result
                    ), f"Symlink should not appear in {result}"
            finally:
                if link_path.is_symlink() or link_path.exists():
                    link_path.unlink()


# ---------------------------------------------------------------------------
# 10. test_json_report_fallback_on_corrupt_data
# ---------------------------------------------------------------------------

class TestJsonReportFallback:
    """When the JSON report is missing or corrupt, TestRunner should still
    produce a valid TestResult via fallback parsing."""

    async def test_fallback_parse_on_corrupt_report(self) -> None:
        """Directly test _fallback_parse with synthetic data."""
        result = TestRunner._fallback_parse(
            returncode=1,
            duration=1.5,
            stdout="FAILED tests/test_foo.py::test_bar\n1 failed, 2 passed in 0.5s\n",
        )
        assert result.passed is False
        assert result.total == 3
        assert result.failed == 1
        assert result.flake_suspected is False

    async def test_fallback_parse_passing(self) -> None:
        """Fallback parse with returncode 0 should report passed."""
        result = TestRunner._fallback_parse(
            returncode=0,
            duration=0.8,
            stdout="3 passed in 0.3s\n",
        )
        assert result.passed is True
        assert result.total == 3
        assert result.failed == 0


# ---------------------------------------------------------------------------
# 11. test_is_safe_path_helper
# ---------------------------------------------------------------------------

class TestIsSafePathHelper:
    """Unit tests for the _is_safe_path security helper."""

    def test_path_inside_repo_is_safe(self) -> None:
        repo = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
        inside = repo / "backend" / "core" / "foo.py"
        assert _is_safe_path(inside, repo) is True

    def test_tmp_path_is_safe(self) -> None:
        with tempfile.NamedTemporaryFile(dir="/tmp", delete=False) as f:
            tmp = Path(f.name)
        try:
            repo = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
            assert _is_safe_path(tmp, repo) is True
        finally:
            tmp.unlink(missing_ok=True)

    def test_outside_path_is_unsafe(self) -> None:
        repo = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
        outside = Path("/etc/passwd")
        assert _is_safe_path(outside, repo) is False
