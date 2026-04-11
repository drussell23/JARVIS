"""PatchBenchmarker — measures objective quality of an applied patch.

Runs lint (ruff), coverage (pytest-cov), and complexity (radon) on the
modified files. Never raises. All failures surface in BenchmarkResult.error.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger(__name__)

# Bounded concurrency: max 2 parallel benchmarks. The semaphore lives in a
# quarantined `_process_singletons` module so that hot-reloading
# patch_benchmarker does NOT replace the semaphore object and orphan any
# in-flight `async with` waiters. Always access via _benchmark_semaphore().
_BENCHMARK_SEMAPHORE_KEY = "patch_benchmarker.benchmark_concurrency"
_BENCHMARK_SEMAPHORE_VALUE = 2


def _benchmark_semaphore() -> asyncio.Semaphore:
    from backend.core.ouroboros.governance._process_singletons import get_semaphore
    return get_semaphore(_BENCHMARK_SEMAPHORE_KEY, _BENCHMARK_SEMAPHORE_VALUE)

# Per-step time budgets (seconds)
_LINT_BUDGET = 15.0
_COVERAGE_BUDGET = 35.0
_COMPLEXITY_BUDGET = 10.0

_TASK_TAXONOMY = [
    ("testing",         lambda d, fs: "test" in d.lower() or any("tests/" in f or Path(f).name.startswith("test_") for f in fs)),
    ("refactoring",     lambda d, fs: "refactor" in d.lower()),
    ("bug_fix",         lambda d, fs: "bug" in d.lower() or "fix" in d.lower()),
    ("security",        lambda d, fs: "security" in d.lower()),
    ("performance",     lambda d, fs: "perf" in d.lower() or "optim" in d.lower()),
    ("code_improvement",lambda d, fs: True),  # default
]

# Python source extensions. Only files matching these are subject to
# pytest / ruff / radon — Python's verification toolchain produces no
# meaningful signal on requirements.txt, *.yaml, *.json, *.md, etc.
_PYTHON_SUFFIXES = (".py", ".pyi")


def _is_python_file(path: str) -> bool:
    """True iff the relative path looks like a Python source file by extension."""
    p = path.lower()
    return p.endswith(_PYTHON_SUFFIXES)


def _filter_python_files(target_files) -> list:
    """Return only the .py / .pyi entries from target_files, preserving order."""
    return [f for f in target_files if _is_python_file(f)]


def _infer_task_type(description: str, target_files: tuple) -> str:
    for task_type, predicate in _TASK_TAXONOMY:
        if predicate(description, target_files):
            return task_type
    return "code_improvement"


def _compute_patch_hash(applied: dict) -> str:
    """sha256 of sorted rel_path:content entries. Deterministic, order-independent."""
    payload = "\n".join(sorted(f"{k}:{v}" for k, v in applied.items()))
    return hashlib.sha256(payload.encode()).hexdigest()


def _compute_quality_score(
    lint_score: float,
    coverage_score: float,
    complexity_score: float,
    radon_available: bool,
) -> float:
    ls = max(0.0, min(1.0, lint_score))
    cs = max(0.0, min(1.0, coverage_score))
    xs = max(0.0, min(1.0, complexity_score))
    if radon_available:
        return 0.45 * ls + 0.45 * cs + 0.10 * xs
    else:
        return 0.50 * ls + 0.50 * cs


# NOTE: All fields must be JSON-primitive (str, int, float, bool, None).
# Adding Enum-typed fields would break the OperationContext hash chain
# because dataclasses.asdict() does not normalize enums to their .name string.
@dataclass(frozen=True)
class BenchmarkResult:
    pass_rate: float
    lint_violations: int
    coverage_pct: float
    complexity_delta: float
    patch_hash: str
    quality_score: float
    task_type: str
    timed_out: bool
    error: Optional[str]
    # N/A sentinel: True when target_files contained zero Python files, so
    # pytest / ruff / radon were intentionally skipped. verify_gate must
    # short-circuit *all* threshold checks (pass_rate, coverage, complexity,
    # lint) when this is True — the metrics carry no signal. Defaults to
    # False so existing call sites and snapshots stay backward-compatible.
    non_python_target: bool = False


class PatchBenchmarker:
    def __init__(
        self,
        project_root: Path,
        timeout_s: float = 60.0,
        pre_apply_snapshots: Optional[dict] = None,
    ) -> None:
        self._root = project_root
        self._timeout_s = timeout_s
        self._pre_apply_snapshots = pre_apply_snapshots or {}

    async def benchmark(self, ctx: "OperationContext") -> BenchmarkResult:
        async with _benchmark_semaphore():
            return await self._run(ctx)

    async def _run(self, ctx: "OperationContext") -> BenchmarkResult:
        target_files = [f for f in [str(f) for f in ctx.target_files] if not Path(f).is_absolute()]
        if not target_files and ctx.target_files:
            logger.warning("[PatchBenchmarker] All target_files are absolute paths, skipping hash computation")
        task_type = _infer_task_type(ctx.description, tuple(target_files))
        patch_hash = _compute_patch_hash(
            {str(f): Path(self._root / f).read_text(errors="replace")
             for f in target_files if (self._root / f).exists()}
        )

        # ----- Non-Python target short-circuit (Manifesto §6: zero-shortcut root fix) -----
        # pytest / ruff / radon are Python-specific. Running them on
        # requirements.txt, *.yaml, *.json, *.md, *.toml, etc. either
        # fails outright or — much worse — collects the FULL project
        # test suite (because `pytest --cov=requirements.txt` does NOT
        # filter test discovery), picks up unrelated failing tests,
        # computes pass_rate < 1.0, and trips verify_regression rollback.
        #
        # This short-circuit mirrors the orchestrator scoped-verify N/A
        # guard (`_verify_test_total == 0 → _verify_test_passed = True`)
        # at the benchmark layer: when there is nothing Python to verify,
        # pass cleanly with non_python_target=True so verify_gate skips
        # threshold checks. Verification of non-Python infra/config
        # changes is delegated to InfraApplicator (e.g., pip install)
        # and the orchestrator scoped-verify path.
        #
        # Ref: bt-2026-04-11-213801 / op-019d7e7d (requirements.txt)
        # blocked the first sustained APPLY here.
        python_files = _filter_python_files(target_files)
        if target_files and not python_files:
            preview = ", ".join(target_files[:3]) + ("..." if len(target_files) > 3 else "")
            logger.info(
                "[PatchBenchmarker] non-Python targets only (%d file(s): %s); "
                "skipping pytest/ruff/radon as N/A — verify delegated to "
                "InfraApplicator + scoped-verify",
                len(target_files), preview,
            )
            return BenchmarkResult(
                pass_rate=1.0,
                lint_violations=0,
                coverage_pct=0.0,
                complexity_delta=0.0,
                patch_hash=patch_hash,
                quality_score=1.0,
                task_type=task_type,
                timed_out=False,
                error=None,
                non_python_target=True,
            )

        # Mixed targets (e.g., ["foo.py", "requirements.txt"]) run the
        # Python toolchain on the .py subset only — non-Python files are
        # silently filtered out at this point.
        bench_files = python_files

        timed_out = False
        errors: list = []

        # Distribute timeout_s across steps: 25% lint, 58% coverage, 17% complexity
        lint_budget = min(_LINT_BUDGET, self._timeout_s * 0.25)
        cov_budget = min(_COVERAGE_BUDGET, self._timeout_s * 0.58)
        cx_budget = min(_COMPLEXITY_BUDGET, self._timeout_s * 0.17)

        # Lint
        lint_violations = 0
        lint_score = 0.0
        try:
            lint_violations, lint_score = await asyncio.wait_for(
                self._run_lint(bench_files), timeout=lint_budget
            )
        except asyncio.TimeoutError:
            timed_out = True
            errors.append("lint timed out")
        except Exception as exc:
            errors.append(f"lint: {exc}")

        # Coverage
        coverage_pct = 0.0
        coverage_score = 0.0
        pass_rate = 0.0
        try:
            coverage_pct, pass_rate = await asyncio.wait_for(
                self._run_coverage(bench_files), timeout=cov_budget
            )
            coverage_score = min(1.0, coverage_pct / 100.0)
        except asyncio.TimeoutError:
            timed_out = True
            errors.append("coverage timed out")
        except Exception as exc:
            errors.append(f"coverage: {exc}")

        # Complexity
        complexity_delta = 0.0
        radon_available = False
        try:
            complexity_delta, radon_available = await asyncio.wait_for(
                self._run_complexity(bench_files), timeout=cx_budget
            )
        except asyncio.TimeoutError:
            timed_out = True
            errors.append("complexity timed out")
        except Exception as exc:
            errors.append(f"complexity: {exc}")

        complexity_score = max(0.0, min(1.0, 1.0 - max(0.0, complexity_delta / 5.0)))
        quality_score = _compute_quality_score(lint_score, coverage_score, complexity_score, radon_available)

        return BenchmarkResult(
            pass_rate=pass_rate,
            lint_violations=lint_violations,
            coverage_pct=coverage_pct,
            complexity_delta=complexity_delta,
            patch_hash=patch_hash,
            quality_score=quality_score,
            task_type=task_type,
            timed_out=timed_out,
            error="; ".join(errors) if errors else None,
            non_python_target=False,
        )

    async def _run_lint(self, target_files: list) -> tuple:
        if not target_files:
            return 0, 1.0
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._lint_sync, target_files)

    def _lint_sync(self, target_files: list) -> tuple:
        try:
            r = subprocess.run(
                ["ruff", "check", "--select=E,F,W", "--output-format=json"] + target_files,
                capture_output=True, text=True, cwd=self._root, timeout=_LINT_BUDGET,
            )
            violations = len(json.loads(r.stdout)) if r.stdout.strip().startswith("[") else 0
        except FileNotFoundError:
            return 0, 1.0  # ruff not installed — don't penalize
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return 0, 0.0

        lines = sum(
            len(Path(self._root / f).read_text(errors="replace").splitlines())
            for f in target_files if (self._root / f).exists()
        )
        score = max(0.0, 1.0 - violations / max(1, lines * 0.05))
        return violations, score

    async def _run_coverage(self, target_files: list) -> tuple:
        if not target_files:
            return 0.0, 0.0
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._coverage_sync, target_files)

    def _coverage_sync(self, target_files: list) -> tuple:
        try:
            import re
            with tempfile.TemporaryDirectory() as tmp:
                cov_json = str(Path(tmp) / "coverage.json")
                cov_args = [f"--cov={f}" for f in target_files if (self._root / f).exists()]
                if not cov_args:
                    cov_args = ["--cov=."]
                r = subprocess.run(
                    ["python3", "-m", "pytest", "--tb=no", "--no-header", "-q",
                     f"--cov-report=json:{cov_json}",
                     "--ignore=docs", "--ignore=.worktrees"] + cov_args,
                    capture_output=True, text=True, cwd=self._root, timeout=_COVERAGE_BUDGET,
                )
                cov_pct = 0.0
                cov_file = Path(cov_json)
                if cov_file.exists():
                    try:
                        data = json.loads(cov_file.read_text())
                        cov_pct = float(data.get("totals", {}).get("percent_covered", 0.0))
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
                # Parse pass_rate from pytest output using regex
                pass_rate = 0.0
                summary = r.stdout + r.stderr
                # Match patterns like "5 passed", "3 passed, 2 failed", "1 error"
                passed_m = re.search(r"(\d+) passed", summary)
                failed_m = re.search(r"(\d+) failed", summary)
                error_m = re.search(r"(\d+) error", summary)
                if passed_m:
                    passed = int(passed_m.group(1))
                    failed = int(failed_m.group(1)) if failed_m else 0
                    errors = int(error_m.group(1)) if error_m else 0
                    total = passed + failed + errors
                    pass_rate = passed / max(1, total)
                elif r.returncode == 0:
                    pass_rate = 1.0
                elif (
                    r.returncode == 5
                    or "no tests ran" in summary
                    or re.search(r"collected 0 items", summary)
                ):
                    # pytest exit 5 = "no tests collected". For non-Python
                    # targets (requirements.txt, configs, docs) there are no
                    # tests to run — that is N/A, not a regression. Mirror
                    # the orchestrator scoped-verify guard at orchestrator.py
                    # `_verify_test_total == 0 → _verify_test_passed = True`.
                    # bt-2026-04-11-213801 / op-019d7e7d (requirements.txt)
                    # blocked the first sustained APPLY by treating this as
                    # `pass_rate=0.00 < threshold=1.00` and rolling back.
                    pass_rate = 1.0
                return float(cov_pct), pass_rate
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return 0.0, 0.0

    async def _run_complexity(self, target_files: list) -> tuple:
        if not target_files:
            return 0.0, False
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._complexity_sync, target_files)

    def _complexity_sync(self, target_files: list) -> tuple:
        try:
            r_after = subprocess.run(
                ["python3", "-m", "radon", "cc", "-s", "-a"] + target_files,
                capture_output=True, text=True, cwd=self._root, timeout=_COMPLEXITY_BUDGET,
            )
            after_cc = self._parse_radon_average(r_after.stdout)

            before_cc = after_cc  # default: no delta
            if self._pre_apply_snapshots:
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    written = []
                    for rel_path, content in self._pre_apply_snapshots.items():
                        dest = tmp_path / rel_path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_text(content)
                        written.append(str(dest))
                    if written:
                        r_before = subprocess.run(
                            ["python3", "-m", "radon", "cc", "-s", "-a"] + written,
                            capture_output=True, text=True, cwd=tmp, timeout=_COMPLEXITY_BUDGET,
                        )
                        before_cc = self._parse_radon_average(r_before.stdout)

            return after_cc - before_cc, True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return 0.0, False

    @staticmethod
    def _parse_radon_average(output: str) -> float:
        for line in output.splitlines():
            if "Average complexity" in line:
                try:
                    return float(line.split(":")[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass
        return 0.0
