"""TestRunner — Pytest subprocess wrapper for the governed self-development pipeline.

Provides deterministic test scoping and async pytest execution with:
- Name-convention mapping (foo.py -> test_foo.py)
- Package and repo-level fallbacks
- Flake detection via single retry
- Structured JSON output parsing via pytest-json-report
- Security: symlinks pointing outside repo_root are rejected
- Graceful fallback when JSON report is missing or corrupt
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestResult:
    """Immutable result of a pytest invocation."""

    passed: bool
    total: int
    failed: int
    failed_tests: Tuple[str, ...]
    duration_seconds: float
    stdout: str
    flake_suspected: bool


# ---------------------------------------------------------------------------
# Language adapter types
# ---------------------------------------------------------------------------


class BlockedPathError(Exception):
    """Raised when a changed file resolves outside the repo root.
    Pipeline must be CANCELLED — this is a security gate.
    """


@dataclass(frozen=True)
class AdapterResult:
    """Result from a single LanguageAdapter run."""

    adapter: str  # "python" | "cpp"
    passed: bool
    failure_class: Literal["none", "test", "build", "infra"]
    # "none"  = success
    # "test"  = test assertion failure
    # "build" = compile error or ABI drift
    # "infra" = toolchain missing / configure-stage failure
    test_result: TestResult
    duration_s: float


@dataclass(frozen=True)
class MultiAdapterResult:
    """Combined result from all adapters for one operation."""

    passed: bool  # True only if ALL adapters passed
    adapter_results: Tuple[AdapterResult, ...]
    dominant_failure: Optional[AdapterResult]  # first failing adapter
    total_duration_s: float

    @property
    def failure_class(self) -> str:
        return self.dominant_failure.failure_class if self.dominant_failure else "none"


# ---------------------------------------------------------------------------
# Declarative routing table — no if-chains
# ---------------------------------------------------------------------------

import re as _re


@dataclass(frozen=True)
class _AdapterRule:
    pattern: _re.Pattern[str]
    adapters: Tuple[str, ...]
    reason: str


_ADAPTER_RULES: Tuple[_AdapterRule, ...] = (
    _AdapterRule(
        pattern=_re.compile(r"^(mlforge|bindings)/"),
        adapters=("python", "cpp"),
        reason="native sublayer: mlforge/bindings require dual verification",
    ),
    _AdapterRule(
        pattern=_re.compile(r"^(reactor_core|tests)/"),
        adapters=("python",),
        reason="pure python layer",
    ),
    _AdapterRule(
        pattern=_re.compile(r".*"),  # catch-all
        adapters=("python",),
        reason="default: python adapter",
    ),
)


def _normalize(path: Path, repo_root: Path) -> str:
    """Resolve path to repo-relative POSIX string.

    Raises BlockedPathError if path resolves outside repo_root.
    This is for routing decisions only (not the _is_safe_path test security check).
    """
    try:
        resolved = path.resolve()
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        raise BlockedPathError(
            f"Path {path} resolves outside repo root {repo_root}. "
            "Pipeline CANCELLED — security gate."
        )


def _route(changed_files: Tuple[Path, ...], repo_root: Path) -> FrozenSet[str]:
    """Return union of required adapters across all changed files.

    Uses first-matching rule per file (table order), union across all files.
    Raises BlockedPathError if any file is outside repo_root.
    """
    required: set[str] = set()
    for path in changed_files:
        norm = _normalize(path, repo_root)
        for rule in _ADAPTER_RULES:
            if rule.pattern.match(norm):
                required.update(rule.adapters)
                break
    return frozenset(required)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALLOWED_SANDBOX_PREFIXES: Tuple[str, ...] = (
    "/tmp", "/var", "/private/tmp", "/private/var",
)


def _is_safe_path(path: Path, repo_root: Path) -> bool:
    """Return True if *path* is inside *repo_root* or an allowed sandbox prefix.

    Symlinks that resolve outside repo_root (and outside sandbox prefixes)
    are rejected.
    """
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return False

    repo_resolved = repo_root.resolve()

    # Inside repo -- always OK
    try:
        resolved.relative_to(repo_resolved)
        return True
    except ValueError:
        pass

    # Inside allowed sandbox prefixes -- OK for test isolation
    resolved_str = str(resolved)
    for prefix in _ALLOWED_SANDBOX_PREFIXES:
        if resolved_str.startswith(prefix):
            return True

    return False


def _find_sibling_tests_dir(source_file: Path) -> Optional[Path]:
    """Walk up from *source_file* looking for a sibling ``tests/`` directory."""
    current = source_file.parent
    while current != current.parent:
        candidate = current / "tests"
        if candidate.is_dir():
            return candidate
        current = current.parent
    return None


# ---------------------------------------------------------------------------
# TestRunner
# ---------------------------------------------------------------------------

class TestRunner:
    """Async pytest subprocess wrapper with flake detection.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    timeout:
        Per-invocation timeout in seconds (default 120).
    """

    def __init__(self, repo_root: Path, timeout: float = 120.0) -> None:
        self._repo_root = repo_root.resolve()
        self._timeout = timeout

    # -- public API ---------------------------------------------------------

    async def resolve_affected_tests(
        self,
        changed_files: Tuple[Path, ...],
    ) -> Tuple[Path, ...]:
        """Deterministically scope which test files to run.

        Strategy (evaluated per changed file):
        1. **Name convention**: ``foo.py`` -> ``test_foo.py`` in nearest
           sibling ``tests/`` directory.
        2. **Package fallback**: if no name match, run *all* tests in the
           nearest ``tests/`` directory.
        3. **Repo fallback**: if still empty, return the repo-level
           ``tests/`` directory.

        Symlinks that resolve outside *repo_root* (and outside /tmp, /var)
        are silently filtered.
        """
        matched: List[Path] = []
        seen: set = set()

        for changed in changed_files:
            # Security: reject symlinks outside repo
            if not _is_safe_path(changed, self._repo_root):
                logger.warning(
                    "Skipping path outside repo_root: %s", changed,
                )
                continue

            # Strategy 1: name convention
            test_name = "test_" + changed.name
            tests_dir = _find_sibling_tests_dir(changed)

            if tests_dir is not None:
                candidate = tests_dir / test_name
                if candidate.is_file() and candidate not in seen:
                    seen.add(candidate)
                    matched.append(candidate)
                    continue

                # Strategy 2: package fallback -- all test files in tests_dir
                if tests_dir not in seen:
                    test_files = sorted(tests_dir.glob("test_*.py"))
                    for tf in test_files:
                        if tf not in seen:
                            seen.add(tf)
                            matched.append(tf)
                    if test_files:
                        continue

            # Strategy 3: repo fallback
            repo_tests = self._repo_root / "tests"
            if repo_tests.is_dir() and repo_tests not in seen:
                seen.add(repo_tests)
                matched.append(repo_tests)

        # If nothing matched at all, fall back to repo tests/
        if not matched:
            repo_tests = self._repo_root / "tests"
            if repo_tests.is_dir():
                matched.append(repo_tests)

        return tuple(matched)

    async def run(
        self,
        test_files: Tuple[Path, ...],
        sandbox_dir: Optional[Path] = None,
    ) -> TestResult:
        """Run pytest on *test_files*, retrying once on failure for flake detection.

        Parameters
        ----------
        test_files:
            Paths to test files or directories.
        sandbox_dir:
            If provided, pytest ``cwd`` is set to this directory.

        Returns
        -------
        TestResult
            Aggregated result.  ``flake_suspected`` is True when the first
            run fails but the retry passes.
        """
        safe_files: list[str] = []
        for tf in test_files:
            if _is_safe_path(tf, self._repo_root):
                safe_files.append(str(tf))
            else:
                logger.warning("Skipping test path outside repo root: %s", tf)

        if not safe_files:
            return TestResult(
                passed=True,
                total=0,
                failed=0,
                failed_tests=(),
                duration_seconds=0.0,
                stdout="no safe test files to run",
                flake_suspected=False,
            )

        paths = safe_files
        cwd = sandbox_dir

        first = await self._run_pytest(paths, cwd=cwd)
        if first.passed:
            return first

        # Retry once for flake detection
        logger.info(
            "First run failed (%d/%d). Retrying for flake detection...",
            first.failed, first.total,
        )
        retry = await self._run_pytest(paths, cwd=cwd)
        if retry.passed:
            return TestResult(
                passed=True,
                total=retry.total,
                failed=0,
                failed_tests=(),
                duration_seconds=first.duration_seconds + retry.duration_seconds,
                stdout=first.stdout + "\n--- RETRY ---\n" + retry.stdout,
                flake_suspected=True,
            )

        # Both runs failed -- genuine failure
        return TestResult(
            passed=False,
            total=retry.total,
            failed=retry.failed,
            failed_tests=retry.failed_tests,
            duration_seconds=first.duration_seconds + retry.duration_seconds,
            stdout=first.stdout + "\n--- RETRY ---\n" + retry.stdout,
            flake_suspected=False,
        )

    # -- private ------------------------------------------------------------

    async def _run_pytest(
        self,
        test_paths: List[str],
        cwd: Optional[Path] = None,
    ) -> TestResult:
        """Execute a single pytest invocation as a subprocess.

        Uses ``pytest-json-report`` for structured output.  Falls back to
        exit-code-only heuristics when the JSON report is missing or corrupt.
        """
        # Temp file for JSON report
        fd, report_path = tempfile.mkstemp(
            suffix=".json", prefix="pytest_report_",
        )
        os.close(fd)

        cmd = [
            "python3", "-m", "pytest",
            "--json-report",
            "--json-report-file=" + report_path,
            "-q",
            "--tb=short",
            "--no-header",
        ] + test_paths

        effective_cwd = str(cwd) if cwd else str(self._repo_root)
        start = time.monotonic()

        try:
            result = await self._exec_with_timeout(cmd, effective_cwd, report_path)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            return TestResult(
                passed=False,
                total=0,
                failed=0,
                failed_tests=(),
                duration_seconds=elapsed,
                stdout="pytest timed out after {:.1f}s".format(self._timeout),
                flake_suspected=False,
            )
        finally:
            self._cleanup_report(report_path)

        elapsed = time.monotonic() - start
        stdout_text: str = str(result.get("stdout", ""))
        raw_returncode = result.get("returncode")
        returncode: Optional[int] = int(raw_returncode) if isinstance(raw_returncode, (int, float, str)) else None
        raw_report = result.get("report_data")

        # Try to parse JSON report
        if isinstance(raw_report, dict):
            return self._parse_json_report(raw_report, elapsed, stdout_text)

        # Fallback: exit-code heuristic
        return self._fallback_parse(returncode, elapsed, stdout_text)

    async def _exec_with_timeout(
        self,
        cmd: List[str],
        cwd: str,
        report_path: str,
    ) -> Dict[str, object]:
        """Run subprocess with timeout.

        Returns dict with keys: stdout, returncode, report_data.
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )

        try:
            raw_stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            # Kill the process on timeout
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            # Wait for process to actually terminate
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            raise

        stdout_text = (
            raw_stdout.decode("utf-8", errors="replace") if raw_stdout else ""
        )

        # Try to load the JSON report
        report_data = None
        if os.path.isfile(report_path):
            try:
                with open(report_path, "r") as f:
                    report_data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to parse pytest JSON report: %s", exc)

        return {
            "stdout": stdout_text,
            "returncode": proc.returncode,
            "report_data": report_data,
        }

    @staticmethod
    def _parse_json_report(
        data: dict,
        duration: float,
        stdout: str,
    ) -> TestResult:
        """Parse pytest-json-report output into a TestResult."""
        summary = data.get("summary", {})
        total = summary.get("total", 0)
        failed_count = summary.get("failed", 0)
        error_count = summary.get("error", 0)
        effective_failed = failed_count + error_count

        # Collect failed test node IDs
        failed_tests: List[str] = []
        for test in data.get("tests", []):
            outcome = test.get("outcome", "")
            if outcome in ("failed", "error"):
                failed_tests.append(test.get("nodeid", "<unknown>"))

        passed = effective_failed == 0 and total > 0
        return TestResult(
            passed=passed,
            total=total,
            failed=effective_failed,
            failed_tests=tuple(sorted(failed_tests)),
            duration_seconds=duration,
            stdout=stdout,
            flake_suspected=False,
        )

    @staticmethod
    def _fallback_parse(
        returncode: Optional[int],
        duration: float,
        stdout: str,
    ) -> TestResult:
        """Best-effort parse when JSON report is unavailable."""
        passed = returncode == 0
        total = 0
        failed = 0
        for line in stdout.splitlines():
            stripped = line.strip()
            if "passed" in stripped or "failed" in stripped:
                m_passed = re.search(r"(\d+)\s+passed", stripped)
                m_failed = re.search(r"(\d+)\s+failed", stripped)
                m_error = re.search(r"(\d+)\s+error", stripped)
                p = int(m_passed.group(1)) if m_passed else 0
                f = int(m_failed.group(1)) if m_failed else 0
                e = int(m_error.group(1)) if m_error else 0
                total = p + f + e
                failed = f + e

        return TestResult(
            passed=passed,
            total=total,
            failed=failed,
            failed_tests=(),
            duration_seconds=duration,
            stdout=stdout,
            flake_suspected=False,
        )

    @staticmethod
    def _cleanup_report(report_path: str) -> None:
        """Remove the temporary JSON report file."""
        try:
            os.unlink(report_path)
        except OSError:
            pass
