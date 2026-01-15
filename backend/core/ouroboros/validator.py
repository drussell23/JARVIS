"""
Test Validator Module for Ouroboros
===================================

Provides comprehensive test validation:
- pytest integration
- Coverage tracking
- Mutation testing
- Performance benchmarking

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.engine import OuroborosConfig, ValidationStatus


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class TestResult:
    """Result of a single test."""
    name: str
    passed: bool
    duration: float = 0.0
    error: str = ""
    output: str = ""


@dataclass
class CoverageReport:
    """Code coverage report."""
    total_lines: int = 0
    covered_lines: int = 0
    missing_lines: List[int] = field(default_factory=list)
    coverage_percent: float = 0.0
    branch_coverage: float = 0.0
    file_coverages: Dict[str, float] = field(default_factory=dict)


@dataclass
class MutationResult:
    """Result of mutation testing."""
    total_mutants: int = 0
    killed_mutants: int = 0
    survived_mutants: int = 0
    timeout_mutants: int = 0
    mutation_score: float = 0.0
    surviving_mutations: List[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Complete validation result."""
    status: ValidationStatus
    test_output: str = ""
    error_message: str = ""
    coverage_percent: float = 0.0
    execution_time: float = 0.0
    passed_tests: int = 0
    failed_tests: int = 0
    test_results: List[TestResult] = field(default_factory=list)
    coverage_report: Optional[CoverageReport] = None
    mutation_result: Optional[MutationResult] = None

    @property
    def is_success(self) -> bool:
        return self.status == ValidationStatus.PASSED


# =============================================================================
# TEST VALIDATOR
# =============================================================================

class TestValidator:
    """
    Validates code changes by running tests.

    Features:
    - pytest integration
    - Coverage measurement
    - Parallel test execution
    - Failure analysis
    """

    def __init__(
        self,
        working_dir: Optional[Path] = None,
        timeout: float = OuroborosConfig.TEST_TIMEOUT,
        coverage_enabled: bool = True,
        parallel_enabled: bool = True,
    ):
        self.working_dir = working_dir or Path.cwd()
        self.timeout = timeout
        self.coverage_enabled = coverage_enabled
        self.parallel_enabled = parallel_enabled

    async def validate(
        self,
        test_command: Optional[str] = None,
        test_file: Optional[Path] = None,
        target_file: Optional[Path] = None,
    ) -> ValidationResult:
        """
        Run tests and return validation result.

        Args:
            test_command: Custom test command
            test_file: Specific test file to run
            target_file: Source file being validated (for coverage)

        Returns:
            ValidationResult
        """
        start_time = time.time()

        # Build test command
        cmd = self._build_test_command(test_command, test_file, target_file)

        try:
            result = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
                env=self._get_env(),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                result.kill()
                return ValidationResult(
                    status=ValidationStatus.TIMEOUT,
                    error_message=f"Test timeout after {self.timeout}s",
                    execution_time=time.time() - start_time,
                )

            output = stdout.decode() + stderr.decode()
            execution_time = time.time() - start_time

            # Parse results
            passed = result.returncode == 0
            passed_tests, failed_tests, test_results = self._parse_pytest_output(output)

            # Parse coverage if enabled
            coverage_report = None
            coverage_percent = 0.0
            if self.coverage_enabled:
                coverage_report = self._parse_coverage(output)
                if coverage_report:
                    coverage_percent = coverage_report.coverage_percent

            return ValidationResult(
                status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
                test_output=output,
                error_message="" if passed else self._extract_error(output),
                coverage_percent=coverage_percent,
                execution_time=execution_time,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                test_results=test_results,
                coverage_report=coverage_report,
            )

        except Exception as e:
            return ValidationResult(
                status=ValidationStatus.ERROR,
                error_message=str(e),
                execution_time=time.time() - start_time,
            )

    def _build_test_command(
        self,
        test_command: Optional[str],
        test_file: Optional[Path],
        target_file: Optional[Path],
    ) -> str:
        """Build the test command."""
        if test_command:
            return test_command

        parts = ["pytest"]

        # Add test file or auto-discover
        if test_file:
            parts.append(str(test_file))

        # Add common options
        parts.extend([
            "-v",
            "--tb=short",
            "-q",
        ])

        # Add parallel execution
        if self.parallel_enabled:
            parts.extend(["-n", "auto"])

        # Add coverage
        if self.coverage_enabled:
            parts.append("--cov")
            if target_file:
                parts.extend(["--cov=" + str(target_file.parent)])
            parts.append("--cov-report=term-missing")

        return " ".join(parts)

    def _get_env(self) -> Dict[str, str]:
        """Get environment for test execution."""
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _parse_pytest_output(self, output: str) -> Tuple[int, int, List[TestResult]]:
        """Parse pytest output to extract test results."""
        passed = 0
        failed = 0
        results = []

        # Parse summary line
        summary_match = re.search(r"(\d+) passed", output)
        if summary_match:
            passed = int(summary_match.group(1))

        failed_match = re.search(r"(\d+) failed", output)
        if failed_match:
            failed = int(failed_match.group(1))

        # Parse individual test results
        test_pattern = re.compile(r"([\w/]+\.py::[\w_]+)\s+(PASSED|FAILED|ERROR|SKIPPED)")
        for match in test_pattern.finditer(output):
            test_name = match.group(1)
            status = match.group(2)

            results.append(TestResult(
                name=test_name,
                passed=(status == "PASSED"),
                error="" if status == "PASSED" else status,
            ))

        return passed, failed, results

    def _parse_coverage(self, output: str) -> Optional[CoverageReport]:
        """Parse coverage information from output."""
        # Look for coverage summary
        coverage_match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
        if not coverage_match:
            return None

        total_coverage = int(coverage_match.group(1))

        # Parse file-specific coverage
        file_coverages = {}
        file_pattern = re.compile(r"([\w/]+\.py)\s+\d+\s+\d+\s+(\d+)%")
        for match in file_pattern.finditer(output):
            file_coverages[match.group(1)] = int(match.group(2))

        # Parse missing lines
        missing_pattern = re.compile(r"Missing\s+([\d,\s-]+)")
        missing_match = missing_pattern.search(output)
        missing_lines = []
        if missing_match:
            ranges = missing_match.group(1).split(",")
            for r in ranges:
                r = r.strip()
                if "-" in r:
                    start, end = map(int, r.split("-"))
                    missing_lines.extend(range(start, end + 1))
                elif r.isdigit():
                    missing_lines.append(int(r))

        return CoverageReport(
            coverage_percent=total_coverage,
            missing_lines=missing_lines,
            file_coverages=file_coverages,
        )

    def _extract_error(self, output: str) -> str:
        """Extract the most relevant error from output."""
        # Look for assertion errors
        assertion_match = re.search(r"AssertionError:.*?(?=\n\n|\Z)", output, re.DOTALL)
        if assertion_match:
            return assertion_match.group(0)[:500]

        # Look for other errors
        error_match = re.search(r"(Error|Exception):.*?(?=\n\n|\Z)", output, re.DOTALL)
        if error_match:
            return error_match.group(0)[:500]

        # Fall back to last 500 chars
        return output[-500:] if len(output) > 500 else output


# =============================================================================
# COVERAGE TRACKER
# =============================================================================

class CoverageTracker:
    """
    Tracks code coverage over time.

    Features:
    - Coverage history
    - Trend analysis
    - Coverage diff
    """

    def __init__(self, history_dir: Optional[Path] = None):
        self.history_dir = history_dir or OuroborosConfig.LEARNING_MEMORY_PATH / "coverage"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._history: List[CoverageReport] = []

    async def record(self, report: CoverageReport, label: str = "") -> None:
        """Record a coverage report."""
        self._history.append(report)

        # Save to disk
        history_file = self.history_dir / f"coverage_{int(time.time())}.json"
        data = {
            "timestamp": time.time(),
            "label": label,
            "coverage_percent": report.coverage_percent,
            "total_lines": report.total_lines,
            "covered_lines": report.covered_lines,
        }
        await asyncio.to_thread(
            history_file.write_text,
            json.dumps(data, indent=2)
        )

    def get_trend(self, n: int = 10) -> List[float]:
        """Get recent coverage trend."""
        recent = self._history[-n:] if self._history else []
        return [r.coverage_percent for r in recent]

    def is_improving(self) -> bool:
        """Check if coverage is trending upward."""
        trend = self.get_trend(5)
        if len(trend) < 2:
            return True

        # Simple linear trend
        return trend[-1] >= trend[0]


# =============================================================================
# MUTATION TESTER
# =============================================================================

class MutationTester:
    """
    Implements mutation testing to verify test quality.

    Mutation testing introduces small bugs (mutants) and checks
    if tests catch them. High mutation score indicates strong tests.

    Mutation operators:
    - Arithmetic: + -> -, * -> /
    - Comparison: > -> <, == -> !=
    - Boolean: and -> or, True -> False
    - Return: return x -> return None
    """

    def __init__(
        self,
        timeout_per_mutant: float = 30.0,
        max_mutants: int = 50,
    ):
        self.timeout_per_mutant = timeout_per_mutant
        self.max_mutants = max_mutants

        # Mutation operators
        self._operators = [
            (r'\+', '-'),
            (r'-', '+'),
            (r'\*', '/'),
            (r'/', '*'),
            (r'==', '!='),
            (r'!=', '=='),
            (r'>', '<'),
            (r'<', '>'),
            (r'>=', '<='),
            (r'<=', '>='),
            (r'\band\b', 'or'),
            (r'\bor\b', 'and'),
            (r'\bTrue\b', 'False'),
            (r'\bFalse\b', 'True'),
            (r'return\s+(\w+)', r'return None'),
        ]

    async def run(
        self,
        source_file: Path,
        test_command: str,
        working_dir: Path,
    ) -> MutationResult:
        """
        Run mutation testing.

        Args:
            source_file: File to mutate
            test_command: Command to run tests
            working_dir: Working directory

        Returns:
            MutationResult
        """
        original_content = await asyncio.to_thread(source_file.read_text)

        # Generate mutants
        mutants = self._generate_mutants(original_content)
        mutants = mutants[:self.max_mutants]  # Limit mutants

        killed = 0
        survived = 0
        timeout = 0
        surviving = []

        try:
            for i, (mutant_code, mutation_desc) in enumerate(mutants):
                # Apply mutant
                await asyncio.to_thread(source_file.write_text, mutant_code)

                # Run tests
                result = await self._run_test(test_command, working_dir)

                if result == "killed":
                    killed += 1
                elif result == "survived":
                    survived += 1
                    surviving.append(mutation_desc)
                else:  # timeout
                    timeout += 1

        finally:
            # Restore original
            await asyncio.to_thread(source_file.write_text, original_content)

        total = killed + survived + timeout
        mutation_score = killed / total if total > 0 else 0.0

        return MutationResult(
            total_mutants=total,
            killed_mutants=killed,
            survived_mutants=survived,
            timeout_mutants=timeout,
            mutation_score=mutation_score,
            surviving_mutations=surviving[:10],  # Limit stored
        )

    def _generate_mutants(self, source: str) -> List[Tuple[str, str]]:
        """Generate mutant versions of the source."""
        mutants = []

        for pattern, replacement in self._operators:
            for match in re.finditer(pattern, source):
                mutant = source[:match.start()] + replacement + source[match.end():]
                desc = f"Line ~{source[:match.start()].count(chr(10))+1}: '{match.group()}' -> '{replacement}'"
                mutants.append((mutant, desc))

        return mutants

    async def _run_test(self, test_command: str, working_dir: Path) -> str:
        """Run test and determine result."""
        try:
            result = await asyncio.create_subprocess_shell(
                test_command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=working_dir,
            )

            try:
                await asyncio.wait_for(result.wait(), timeout=self.timeout_per_mutant)
            except asyncio.TimeoutError:
                result.kill()
                return "timeout"

            # If tests fail, mutant was killed
            return "killed" if result.returncode != 0 else "survived"

        except Exception:
            return "timeout"
