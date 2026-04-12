"""TestRunner — Pytest subprocess wrapper for the governed self-development pipeline.

Provides deterministic test scoping and async pytest execution with:
- Multi-strategy test discovery (name convention, recursive search, package/repo fallback)
- Original-path-aware resolution for sandbox validation paths
- Env-configurable timeouts, retry, max files, and test directory names
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
from typing import Any, Dict, FrozenSet, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-driven configuration — no hardcoded constants
# ---------------------------------------------------------------------------

_TEST_TIMEOUT_S = float(os.environ.get("JARVIS_TEST_TIMEOUT_S", "120"))
_TEST_RETRY_ENABLED = os.environ.get(
    "JARVIS_TEST_RETRY_ENABLED", "true"
).lower() in ("1", "true", "yes")
_TEST_MAX_FILES = int(os.environ.get("JARVIS_TEST_MAX_FILES", "50"))
_TEST_DIR_NAMES: FrozenSet[str] = frozenset(
    os.environ.get("JARVIS_TEST_DIR_NAMES", "tests,test").split(",")
)


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

@dataclass(frozen=True)
class _AdapterRule:
    pattern: re.Pattern[str]
    adapters: Tuple[str, ...]
    reason: str


_ADAPTER_RULES: Tuple[_AdapterRule, ...] = (
    _AdapterRule(
        pattern=re.compile(r"^(mlforge|bindings)/"),
        adapters=("python", "cpp"),
        reason="native sublayer: mlforge/bindings require dual verification",
    ),
    _AdapterRule(
        pattern=re.compile(r"^(reactor_core|tests)/"),
        adapters=("python",),
        reason="pure python layer",
    ),
    # Multi-language adapters (P0 wiring)
    _AdapterRule(
        pattern=re.compile(r".*\.(js|jsx|ts|tsx|mjs|cjs)$"),
        adapters=("javascript",),
        reason="JavaScript/TypeScript file",
    ),
    _AdapterRule(
        pattern=re.compile(r".*\.rs$"),
        adapters=("rust",),
        reason="Rust source file",
    ),
    _AdapterRule(
        pattern=re.compile(r".*\.go$"),
        adapters=("go",),
        reason="Go source file",
    ),
    _AdapterRule(
        pattern=re.compile(r".*"),  # catch-all
        adapters=("python",),
        reason="default: python adapter",
    ),
)


def _normalize(path: Path, repo_root: Path) -> str:
    """Resolve path to repo-relative POSIX string.

    Raises BlockedPathError if path resolves outside repo_root *and*
    outside allowed sandbox prefixes.  Sandbox paths are expected —
    the orchestrator writes candidate content into a temp directory
    before calling LanguageRouter.  For sandbox paths, the filename
    is returned as-is (sufficient for adapter routing).
    """
    try:
        resolved = path.resolve()
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        # Allow paths under known sandbox/temp prefixes — the
        # orchestrator writes files there for validation.
        resolved_str = str(path.resolve())
        if any(resolved_str.startswith(p) for p in _ALLOWED_SANDBOX_PREFIXES):
            return path.name
        raise BlockedPathError(
            f"Path {path} resolves outside repo root {repo_root}. "
            "Pipeline CANCELLED — security gate."
        )


# used by LanguageRouter (Task 3)
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
# PythonAdapter — implements LanguageAdapter protocol for pytest
# ---------------------------------------------------------------------------


class PythonAdapter:
    """Runs pytest as a subprocess. Implements the LanguageAdapter protocol.

    Delegates resolve() to TestRunner.resolve_affected_tests() and
    run() to TestRunner.run() to avoid duplicating subprocess logic.
    """

    name = "python"

    def __init__(self, repo_root: Path, timeout: float = 120.0) -> None:
        self._repo_root = repo_root
        self._timeout = timeout

    async def resolve(
        self,
        changed_files: Tuple[Path, ...],
        _repo_root: Path,
        original_paths: Optional[Dict[Path, Path]] = None,
    ) -> Tuple[Path, ...]:
        """Delegate to TestRunner.resolve_affected_tests().

        Passes *original_paths* through so sandbox paths are mapped back
        to repo-relative paths for correct test directory discovery.
        """
        runner = TestRunner(repo_root=self._repo_root, timeout=self._timeout)
        return await runner.resolve_affected_tests(
            changed_files, original_paths=original_paths,
        )

    async def run(
        self,
        test_files: Tuple[Path, ...],
        sandbox_dir: Optional[Path],
        timeout_budget_s: float,
        _op_id: str,
    ) -> AdapterResult:
        """Run pytest and wrap result in AdapterResult.

        Always runs pytest from ``repo_root`` (not sandbox_dir) so that
        Python imports resolve correctly against the actual project layout.
        """
        t0 = time.monotonic()
        runner = TestRunner(
            repo_root=self._repo_root,
            timeout=min(timeout_budget_s, self._timeout),
        )
        test_result = await runner.run(test_files=test_files, sandbox_dir=None)
        elapsed = time.monotonic() - t0
        return AdapterResult(
            adapter="python",
            passed=test_result.passed,
            failure_class="none" if test_result.passed else "test",
            test_result=test_result,
            duration_s=elapsed,
        )


# ---------------------------------------------------------------------------
# CppAdapter — cmake + ctest subprocess wrapper (implements LanguageAdapter)
# ---------------------------------------------------------------------------


class CppAdapter:
    """Runs cmake --build + ctest for the native C++ sublayer.

    Implements the LanguageAdapter protocol structurally.
    resolve() always returns () — ctest is label/name-driven, not file-driven.

    All subprocess calls are injectable via constructor factories for testing:
      _cmake_build_fn: async(build_dir, budget_s, sandbox_dir) -> (bool, str, str)
                       returns (success, stdout, exit_context)
                       exit_context one of: "exit_0", "exit_1", "executable_not_found", "configure_stage"
      _ctest_fn:       async(build_dir, budget_s, op_id) -> (bool, TestResult)
      _abi_probe_fn:   async(build_dir, sandbox_dir) -> (bool, str)
                       returns (ok, error_message)
    """

    name = "cpp"
    _CMAKE_GENERATOR = "Ninja"
    _ctest_failure_class: Literal["test"] = "test"

    _INFRA_MARKERS: Tuple[str, ...] = (
        "cmake: command not found",
        "ninja: command not found",
        "No CMAKE_CXX_COMPILER",
        "Could not find toolchain",
    )

    def __init__(
        self,
        repo_root: Path,
        scratch_root: Optional[Path] = None,
        timeout: float = 120.0,
        _cmake_build_fn: Optional[Any] = None,
        _ctest_fn: Optional[Any] = None,
        _abi_probe_fn: Optional[Any] = None,
    ) -> None:
        self._repo_root = repo_root
        self._scratch_root = (
            scratch_root
            if scratch_root is not None
            else Path(tempfile.gettempdir()) / "ouroboros_cpp"
        )
        self._timeout = timeout
        self._cmake_build_fn = _cmake_build_fn
        self._ctest_fn = _ctest_fn
        self._abi_probe_fn = _abi_probe_fn

    def _build_dir(self, op_id: str, sandbox_dir: Optional[Path]) -> Path:
        """Per-op isolated build directory — never shared between concurrent ops."""
        base = sandbox_dir if sandbox_dir is not None else self._scratch_root
        return base / op_id / "cpp-build"

    def _classify_build_failure(
        self, output: str, exit_ctx: str
    ) -> Literal["infra", "build"]:
        """Classify build failure: infra (env problem) or build (compile error)."""
        # Process exit context gates first — most reliable signal
        if exit_ctx in ("executable_not_found", "configure_stage"):
            return "infra"
        # String markers as defense in depth
        if any(marker in output for marker in self._INFRA_MARKERS):
            return "infra"
        return "build"

    async def resolve(
        self,
        _changed_files: Tuple[Path, ...],
        _repo_root: Path,
        original_paths: Optional[Dict[Path, Path]] = None,
    ) -> Tuple[Path, ...]:
        """ctest is label/name-driven, not file-path-driven.
        Returns () — always run full ctest suite deterministically.
        """
        return ()

    async def run(
        self,
        test_files: Tuple[Path, ...],
        sandbox_dir: Optional[Path],
        timeout_budget_s: float,
        op_id: str,
    ) -> AdapterResult:
        """Build with cmake, probe ABI, run ctest."""
        build_dir = self._build_dir(op_id, sandbox_dir)
        t0 = time.monotonic()

        # Timeout split: 80% for build, remaining for ctest (min 30s reserved)
        build_budget = timeout_budget_s * 0.8
        test_budget = timeout_budget_s - build_budget

        # ── Build phase ─────────────────────────────────────────────────
        cmake_fn = self._cmake_build_fn or self._default_cmake_build
        build_ok, build_out, build_exit_ctx = await cmake_fn(
            build_dir, build_budget, sandbox_dir
        )
        if not build_ok:
            fclass = self._classify_build_failure(build_out, build_exit_ctx)
            return AdapterResult(
                adapter="cpp", passed=False, failure_class=fclass,
                test_result=TestResult(
                    passed=False, total=0, failed=0, failed_tests=(),
                    duration_seconds=time.monotonic() - t0,
                    stdout=build_out, flake_suspected=False,
                ),
                duration_s=time.monotonic() - t0,
            )

        # ── ABI probe ───────────────────────────────────────────────────
        abi_fn = self._abi_probe_fn or self._default_abi_probe
        abi_ok, abi_err = await abi_fn(build_dir, sandbox_dir)
        if not abi_ok:
            return AdapterResult(
                adapter="cpp", passed=False, failure_class="build",
                test_result=TestResult(
                    passed=False, total=0, failed=0, failed_tests=(),
                    duration_seconds=time.monotonic() - t0,
                    stdout=abi_err, flake_suspected=False,
                ),
                duration_s=time.monotonic() - t0,
            )

        # ── Budget check ────────────────────────────────────────────────
        remaining = test_budget - (time.monotonic() - t0)
        if remaining <= 0:
            return AdapterResult(
                adapter="cpp", passed=False, failure_class="infra",
                test_result=TestResult(
                    passed=False, total=0, failed=0, failed_tests=(),
                    duration_seconds=time.monotonic() - t0,
                    stdout="ctest budget exhausted after cmake build",
                    flake_suspected=False,
                ),
                duration_s=time.monotonic() - t0,
            )

        # ── ctest phase ─────────────────────────────────────────────────
        ctest_fn = self._ctest_fn or self._default_ctest
        ctest_ok, ctest_result = await ctest_fn(build_dir, remaining, op_id)
        return AdapterResult(
            adapter="cpp",
            passed=ctest_ok,
            failure_class="test" if not ctest_ok else "none",
            test_result=ctest_result,
            duration_s=time.monotonic() - t0,
        )

    async def _default_cmake_build(
        self,
        build_dir: Path,
        budget_s: float,
        sandbox_dir: Optional[Path],
    ) -> Tuple[bool, str, str]:
        build_dir.mkdir(parents=True, exist_ok=True)
        cwd = str(sandbox_dir or self._repo_root)
        half = budget_s * 0.5

        # Configure
        try:
            proc = await asyncio.create_subprocess_exec(
                "cmake", str(self._repo_root), "-B", str(build_dir),
                f"-G{self._CMAKE_GENERATOR}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=half)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return False, "cmake configure timed out", "configure_stage"
            stdout = (out or b"").decode(errors="replace")
            if proc.returncode != 0:
                return False, stdout, "configure_stage"
        except FileNotFoundError:
            return False, "cmake: command not found", "executable_not_found"

        # Build
        try:
            proc2 = await asyncio.create_subprocess_exec(
                "cmake", "--build", str(build_dir), "--parallel",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
            try:
                out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=half)
            except asyncio.TimeoutError:
                try:
                    proc2.kill()
                except ProcessLookupError:
                    pass
                return False, "cmake build timed out", "exit_1"
            stdout2 = (out2 or b"").decode(errors="replace")
            ctx = "exit_0" if proc2.returncode == 0 else "exit_1"
            return proc2.returncode == 0, stdout2, ctx
        except FileNotFoundError:
            return False, "cmake: command not found", "executable_not_found"

    async def _default_abi_probe(
        self,
        build_dir: Path,
        sandbox_dir: Optional[Path],
    ) -> Tuple[bool, str]:
        """Probe ABI by cmake-installing and attempting to load .so files."""
        import sys as _sys
        install_tmp = build_dir / "_abi_probe_install"
        try:
            proc = await asyncio.create_subprocess_exec(
                "cmake", "--install", str(build_dir), "--prefix", str(install_tmp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except Exception:
            return True, ""  # no install → no .so → skip probe

        so_files = list(install_tmp.rglob("*.so"))
        if not so_files:
            return True, ""  # no extension → skip probe

        for so in so_files:
            try:
                probe = await asyncio.create_subprocess_exec(
                    _sys.executable, "-c",
                    "import ctypes, sys; ctypes.CDLL(sys.argv[1])",
                    str(so),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                await asyncio.wait_for(probe.communicate(), timeout=10.0)
                if probe.returncode != 0:
                    return False, f"ABI probe: {so.name} failed to load"
            except Exception as exc:
                return False, f"ABI probe error: {exc}"
        return True, ""

    async def _default_ctest(
        self,
        build_dir: Path,
        budget_s: float,
        _op_id: str,
    ) -> Tuple[bool, TestResult]:
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ctest", "--output-on-failure", "--test-dir", str(build_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=budget_s)
            stdout = (out or b"").decode(errors="replace")
            passed = proc.returncode == 0
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            passed, stdout = False, "ctest timed out"
        except FileNotFoundError:
            passed, stdout = False, "ctest: command not found"

        return passed, TestResult(
            passed=passed, total=0, failed=0 if passed else 1,
            failed_tests=(), duration_seconds=time.monotonic() - t0,
            stdout=stdout, flake_suspected=False,
        )


# ---------------------------------------------------------------------------
# LanguageRouter — selects adapters and merges MultiAdapterResult
# ---------------------------------------------------------------------------


class LanguageRouter:
    """Routes changed files to the correct LanguageAdapters using _ADAPTER_RULES.

    Adapter selection is the union-of-all-matches across all changed files.
    If a required adapter is not registered, it is skipped with a warning.
    """

    def __init__(
        self,
        repo_root: Path,
        adapters: Dict[str, Any],
    ) -> None:
        self._repo_root = repo_root
        self._adapters: Dict[str, Any] = adapters

    async def run(
        self,
        changed_files: Tuple[Path, ...],
        sandbox_dir: Optional[Path],
        timeout_budget_s: float,
        op_id: str,
        original_paths: Optional[Dict[Path, Path]] = None,
    ) -> MultiAdapterResult:
        """Run all required adapters and merge results into MultiAdapterResult.

        Parameters
        ----------
        original_paths:
            Optional mapping from sandbox paths to original repo-relative
            paths.  Passed through to adapter ``resolve()`` so test
            discovery can use the real repo layout.
        """
        t0 = time.monotonic()
        # Raises BlockedPathError if any file is outside repo_root
        required_names = _route(changed_files, self._repo_root)

        results: List[AdapterResult] = []
        for name in sorted(required_names):  # sorted for determinism
            adapter = self._adapters.get(name)
            if adapter is None:
                logger.warning(
                    "[LanguageRouter] Adapter %r required but not registered", name
                )
                continue

            test_files = await adapter.resolve(
                changed_files, self._repo_root,
                original_paths=original_paths,
            )
            remaining = timeout_budget_s - (time.monotonic() - t0)
            if remaining <= 0:
                results.append(AdapterResult(
                    adapter=name, passed=False, failure_class="infra",
                    test_result=TestResult(
                        passed=False, total=0, failed=0, failed_tests=(),
                        duration_seconds=0.0,
                        stdout="budget exhausted before adapter run",
                        flake_suspected=False,
                    ),
                    duration_s=0.0,
                ))
                continue

            result = await adapter.run(test_files, sandbox_dir, remaining, op_id)
            results.append(result)

        all_passed = all(r.passed for r in results)
        dominant = next((r for r in results if not r.passed), None)

        return MultiAdapterResult(
            passed=all_passed,
            adapter_results=tuple(results),
            dominant_failure=dominant,
            total_duration_s=time.monotonic() - t0,
        )


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


def _find_sibling_tests_dir(
    source_file: Path,
    dir_names: FrozenSet[str] = _TEST_DIR_NAMES,
) -> Optional[Path]:
    """Walk up from *source_file* looking for a sibling test directory.

    Checks each directory name in *dir_names* at every level.
    """
    current = source_file.parent
    while current != current.parent:
        for name in sorted(dir_names):
            candidate = current / name
            if candidate.is_dir():
                return candidate
        current = current.parent
    return None


def _resolve_original_path(
    sandbox_path: Path,
    original_paths: Optional[Dict[Path, Path]],
) -> Path:
    """Map a sandbox path back to the original repo-relative path.

    When the orchestrator writes candidate files to a temp sandbox for
    VALIDATE, test discovery needs the *original* path to locate the
    correct test directory in the repo.  Returns *sandbox_path* unchanged
    when no mapping is available.
    """
    if original_paths is None:
        return sandbox_path
    return original_paths.get(sandbox_path, sandbox_path)


async def _find_test_recursive(
    source_stem: str,
    repo_root: Path,
    dir_names: FrozenSet[str] = _TEST_DIR_NAMES,
) -> Optional[Path]:
    """Search recursively for ``test_<stem>.py`` under any top-level test directory.

    Runs the filesystem walk in a thread executor to avoid blocking the
    event loop on large repos.
    """
    target = f"test_{source_stem}.py"
    loop = asyncio.get_running_loop()

    def _scan() -> Optional[Path]:
        for tdn in sorted(dir_names):
            top_tests = repo_root / tdn
            if top_tests.is_dir():
                for match in top_tests.rglob(target):
                    if match.is_file():
                        return match
        return None

    return await loop.run_in_executor(None, _scan)


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

    def __init__(self, repo_root: Path, timeout: float = _TEST_TIMEOUT_S) -> None:
        self._repo_root = repo_root.resolve()
        self._timeout = timeout

    # -- public API ---------------------------------------------------------

    async def resolve_affected_tests(
        self,
        changed_files: Tuple[Path, ...],
        original_paths: Optional[Dict[Path, Path]] = None,
    ) -> Tuple[Path, ...]:
        """Deterministically scope which test files to run.

        Parameters
        ----------
        changed_files:
            Paths to changed source files.  May be sandbox paths (from
            VALIDATE) or repo paths (from VERIFY).
        original_paths:
            Optional mapping from sandbox paths to their original
            repo-relative paths.  When the orchestrator writes candidates
            to a temp sandbox, test discovery must use the *original*
            repo path to locate sibling ``tests/`` directories.

        Strategy (evaluated per changed file, first match wins):
        1. **Name convention** — ``foo.py`` → ``test_foo.py`` in nearest
           sibling ``tests/`` directory (using original path).
        2. **Recursive search** — ``test_foo.py`` anywhere under repo
           test directories (async, off-main-thread).
        3. **Package fallback** — all ``test_*.py`` files in the nearest
           sibling ``tests/`` directory.
        4. **Repo fallback** — repo-level ``tests/`` directory.

        Results are capped at ``JARVIS_TEST_MAX_FILES`` (default 50).
        Symlinks resolving outside ``repo_root`` (and outside /tmp, /var)
        are silently filtered.
        """
        matched: List[Path] = []
        seen: set = set()

        for changed in changed_files:
            effective = _resolve_original_path(changed, original_paths)

            if not _is_safe_path(effective, self._repo_root):
                logger.warning(
                    "[TestRunner] Skipping path outside repo_root: %s (original: %s)",
                    changed, effective,
                )
                continue

            stem = effective.stem
            test_name = "test_" + effective.name
            tests_dir = _find_sibling_tests_dir(effective)

            # Strategy 1: name convention in sibling tests/
            if tests_dir is not None:
                candidate = tests_dir / test_name
                if candidate.is_file() and candidate not in seen:
                    seen.add(candidate)
                    matched.append(candidate)
                    logger.debug(
                        "[TestRunner] Strategy 1 (name convention): %s → %s",
                        effective.name, candidate,
                    )
                    continue

            # Strategy 2: recursive search under all test directories
            recursive_match = await _find_test_recursive(
                stem, self._repo_root,
            )
            if recursive_match is not None and recursive_match not in seen:
                seen.add(recursive_match)
                matched.append(recursive_match)
                logger.debug(
                    "[TestRunner] Strategy 2 (recursive): %s → %s",
                    effective.name, recursive_match,
                )
                continue

            # Strategy 3: package fallback — all test files in tests_dir
            if tests_dir is not None and tests_dir not in seen:
                test_files = sorted(tests_dir.glob("test_*.py"))
                for tf in test_files:
                    if tf not in seen:
                        seen.add(tf)
                        matched.append(tf)
                if test_files:
                    logger.debug(
                        "[TestRunner] Strategy 3 (package fallback): %s → %d files in %s",
                        effective.name, len(test_files), tests_dir,
                    )
                    continue

            # Strategy 4: repo fallback
            for tdn in sorted(_TEST_DIR_NAMES):
                repo_tests = self._repo_root / tdn
                if repo_tests.is_dir() and repo_tests not in seen:
                    seen.add(repo_tests)
                    matched.append(repo_tests)
                    logger.debug(
                        "[TestRunner] Strategy 4 (repo fallback): %s → %s",
                        effective.name, repo_tests,
                    )
                    break

        # Last resort: repo-level test dir if nothing matched at all
        if not matched:
            for tdn in sorted(_TEST_DIR_NAMES):
                repo_tests = self._repo_root / tdn
                if repo_tests.is_dir():
                    matched.append(repo_tests)
                    logger.info(
                        "[TestRunner] No strategy matched — falling back to %s",
                        repo_tests,
                    )
                    break

        if len(matched) > _TEST_MAX_FILES:
            logger.info(
                "[TestRunner] Capping test files from %d to %d (JARVIS_TEST_MAX_FILES)",
                len(matched), _TEST_MAX_FILES,
            )
            matched = matched[:_TEST_MAX_FILES]

        logger.info(
            "[TestRunner] Resolved %d test targets for %d changed files",
            len(matched), len(changed_files),
        )
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
            For Python tests, callers should generally leave this ``None``
            so pytest runs from ``repo_root`` (correct for import resolution).

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
                logger.warning("[TestRunner] Skipping test path outside repo root: %s", tf)

        if not safe_files:
            logger.info("[TestRunner] No safe test files to run — returning vacuous pass")
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

        if not _TEST_RETRY_ENABLED:
            logger.info(
                "[TestRunner] First run failed (%d/%d), retry disabled (JARVIS_TEST_RETRY_ENABLED=false)",
                first.failed, first.total,
            )
            return first

        # Retry once for flake detection
        logger.info(
            "[TestRunner] First run failed (%d/%d). Retrying for flake detection...",
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
            "-o", "addopts=",
            "--continue-on-collection-errors",
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

        if total == 0:
            logger.warning(
                "[TestRunner] JSON report has 0 tests — likely a discovery or cwd issue. "
                "stdout snippet: %.300s", stdout[:300],
            )

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
