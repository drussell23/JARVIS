"""SWE-Bench-Pro scorer - Phase C (PRD section 40.7.10-c).

Pure-data scoring layer: takes a Phase B.2.2 EvaluationResult plus
the originating ProblemSpec and produces a deterministic
pass/partial/fail score by:

  1. Master flag gate (swe_bench_pro_enabled)
  2. Skip if the evaluation did not resolve (outcome != RESOLVED)
  3. Skip if the captured patch is missing or empty
  4. Canonical SWE-Bench cheat-detection: patches that modify test
     files are rejected outright (operator-flippable via env)
  5. Re-prepare the problem to a fresh isolated worktree (composes
     Phase B.1's prepare_problem - same code path; the scorer
     is reproducible end-to-end from (captured_patch, problem)
     without needing access to the original worktree)
  6. Apply the captured patch via canonical safe git-apply
     subprocess (same primitive Phase B.1 plus v3.4 GitApplyDiffApplier
     compose; never shell=True)
  7. Run pytest scoped to the test files added by problem.test_patch
     - NOT the whole repo's test suite. Composes canonical
     TestRunner (the same surface the orchestrator's
     VALIDATE / Treefinement use).
  8. Classify outcome from TestResult:
       - all relevant tests pass -> PASS
       - some pass, some fail    -> PARTIAL
       - all fail / runner error -> FAIL
       - infra / apply failure   -> SCORING_ERROR
  9. cleanup_prepared in a finally block (worktree hygiene)

Composition discipline (mandate compliance)
-------------------------------------------

  * Composes canonical surfaces only (no parallel implementations):
      - swe_bench_pro_enabled / ProblemSpec (Phase A)
      - prepare_problem / cleanup_prepared / PreparedProblem (B.1)
      - extract_diff_targets (Treefinement v3.4 - the single source
        of truth for unified-diff path parsing)
      - TestRunner (canonical pytest invocation plus flake retry)
      - EvaluationOutcome / EvaluationResult (B.2.2)

  * No reinvention of git invocation: composes the same
    asyncio.create_subprocess_exec shape Phase B.1's _run_git uses
    (program plus args list, NEVER shell=True). Phase A authority
    asymmetry forbids importing the B.1 private primitive but the
    shape is identical so behavior stays in lock-step.

  * No parallel diff parsing: extract_diff_targets is the only
    diff parser the scorer uses. AST pin in the spine asserts the
    symbol's presence.

  * Canonical SWE-Bench cheat-detection ON by default: real
    benchmarks disqualify patches that modify test files. The
    behavior is operator-flippable via env for operators evaluating
    rubric variants but the default honors the upstream contract.

  * Reproducible from (captured_patch, problem) alone - the scorer
    does NOT require access to the evaluation's original worktree.

Section 7 fail-closed contract
------------------------------

Every code path produces a ScoringResult rather than raising,
except asyncio.CancelledError which propagates per orchestrator
POSTMORTEM convention (cleanup still runs in finally).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.repair_tree_production import (
    extract_diff_targets,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
    swe_bench_pro_enabled,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EvaluationOutcome,
    EvaluationResult,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
    EvaluatorPhase,
    task_phase,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    HarnessOutcome,
    PreparedProblem,
    cleanup_prepared,
    prepare_problem,
)
from backend.core.ouroboros.governance.test_runner import TestRunner


logger = logging.getLogger("Ouroboros.SWEBenchPro.Scorer")


# ===========================================================================
# Schema plus env vocabulary
# ===========================================================================


SCORING_RESULT_SCHEMA_VERSION: str = "swe_bench_pro_scoring.v1"


SCORE_TEST_TIMEOUT_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_SCORE_TEST_TIMEOUT_S"
)
SCORE_REJECT_TEST_MODS_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_SCORE_REJECT_TEST_MODS"
)
SCORE_GIT_OP_TIMEOUT_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_SCORE_GIT_OP_TIMEOUT_S"
)


_DEFAULT_TEST_TIMEOUT_S: float = 600.0
_DEFAULT_GIT_OP_TIMEOUT_S: float = 60.0


_TEST_PATH_MARKERS: Tuple[str, ...] = (
    "/tests/",
    "/test/",
)
_TEST_NAME_PREFIXES: Tuple[str, ...] = ("test_",)
_TEST_NAME_SUFFIXES: Tuple[str, ...] = ("_test.py",)


# ===========================================================================
# Closed taxonomy - ScoreOutcome (5 values; AST-pinned)
# ===========================================================================


class ScoreOutcome(str, enum.Enum):
    """Five canonical outcomes for score_evaluation."""

    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    SCORING_ERROR = "scoring_error"
    SKIPPED = "skipped"


# ===========================================================================
# Frozen ScoringResult dataclass (symmetric to_dict / from_dict)
# ===========================================================================


@dataclass(frozen=True)
class ScoringResult:
    """Result of a single score_evaluation call."""

    outcome: ScoreOutcome
    problem_instance_id: str
    tests_passed: int = 0
    tests_failed: int = 0
    tests_total: int = 0
    pass_rate: float = 0.0
    diagnostic: str = ""
    elapsed_s: float = 0.0
    schema_version: str = SCORING_RESULT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome.value,
            "problem_instance_id": self.problem_instance_id,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "tests_total": self.tests_total,
            "pass_rate": self.pass_rate,
            "diagnostic": self.diagnostic,
            "elapsed_s": self.elapsed_s,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ScoringResult":
        return cls(
            schema_version=str(payload.get(
                "schema_version", SCORING_RESULT_SCHEMA_VERSION,
            )),
            outcome=ScoreOutcome(str(payload["outcome"])),
            problem_instance_id=str(payload["problem_instance_id"]),
            tests_passed=int(payload.get("tests_passed", 0)),
            tests_failed=int(payload.get("tests_failed", 0)),
            tests_total=int(payload.get("tests_total", 0)),
            pass_rate=float(payload.get("pass_rate", 0.0)),
            diagnostic=str(payload.get("diagnostic", "")),
            elapsed_s=float(payload.get("elapsed_s", 0.0)),
        )


# ===========================================================================
# Env loaders (NEVER raise)
# ===========================================================================


def _resolve_test_timeout_s(explicit: Optional[float]) -> float:
    if explicit is not None and explicit > 0:
        return float(explicit)
    raw = os.environ.get(SCORE_TEST_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_TEST_TIMEOUT_S
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError("must be > 0")
        return value
    except (ValueError, TypeError):
        logger.warning(
            "[SWEBenchPro.Scorer] invalid %s=%r - using default %.1fs",
            SCORE_TEST_TIMEOUT_ENV_VAR, raw, _DEFAULT_TEST_TIMEOUT_S,
        )
        return _DEFAULT_TEST_TIMEOUT_S


def _resolve_reject_test_mods(explicit: Optional[bool]) -> bool:
    if explicit is not None:
        return bool(explicit)
    raw = os.environ.get(SCORE_REJECT_TEST_MODS_ENV_VAR, "").strip().lower()
    if not raw:
        return True
    return raw not in ("false", "0", "no", "off")


def _resolve_git_op_timeout_s() -> float:
    raw = os.environ.get(SCORE_GIT_OP_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_GIT_OP_TIMEOUT_S
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
        return value
    except (ValueError, TypeError):
        return _DEFAULT_GIT_OP_TIMEOUT_S


# ===========================================================================
# Test-file classification
# ===========================================================================


def _is_test_file(path_str: str) -> bool:
    """Heuristic: is path_str a test file? Pure function; NEVER raises."""
    if not isinstance(path_str, str) or not path_str:
        return False
    norm = path_str.replace("\\", "/")
    name = norm.rsplit("/", 1)[-1]
    if any(marker in norm for marker in _TEST_PATH_MARKERS):
        return True
    if any(name.startswith(p) for p in _TEST_NAME_PREFIXES):
        return True
    if any(name.endswith(s) for s in _TEST_NAME_SUFFIXES):
        return True
    return False


def _patch_modifies_test_files(captured_patch: str) -> List[str]:
    """Return the test-file paths a captured patch touches.
    Empty list = patch does not touch any test file. Pure function;
    NEVER raises. Composes extract_diff_targets (canonical primitive)."""
    try:
        targets = extract_diff_targets(captured_patch or "")
    except Exception:  # noqa: BLE001
        logger.debug(
            "[SWEBenchPro.Scorer] extract_diff_targets raised",
            exc_info=True,
        )
        return []
    return [t.path for t in targets if _is_test_file(t.path)]


def _classify_test_outcome(
    passed: int, failed: int, total: int,
) -> ScoreOutcome:
    """Map TestResult counts to a ScoreOutcome. Pure function; NEVER raises.

    total == 0       -> FAIL
    passed == total  -> PASS
    0 < passed < total -> PARTIAL
    passed == 0 (total > 0) -> FAIL
    """
    if total <= 0:
        return ScoreOutcome.FAIL
    if passed == total:
        return ScoreOutcome.PASS
    if passed == 0:
        return ScoreOutcome.FAIL
    return ScoreOutcome.PARTIAL


def _finalize_score(
    test_result: Any, instance_id: str, started_at: float,
) -> "ScoringResult":
    """Shared classify + ScoringResult build from a test-result object.

    Slice 65 — extracted so BOTH execution backends compose it: the local
    TestRunner path and the containerized backend (container_engine
    ``ContainerScoreResult``). Both expose ``total`` / ``failed`` /
    ``failed_tests``, so this reads them via getattr — byte-identical to the
    pre-Slice-65 inline block for the local path. Pure; NEVER raises."""
    total = max(0, int(getattr(test_result, "total", 0) or 0))
    failed = max(0, int(getattr(test_result, "failed", 0) or 0))
    passed = max(0, total - failed)
    pass_rate = (passed / total) if total > 0 else 0.0
    outcome = _classify_test_outcome(passed, failed, total)

    diagnostic = ""
    if outcome != ScoreOutcome.PASS and total > 0:
        failed_tests = getattr(test_result, "failed_tests", ()) or ()
        if failed_tests:
            diagnostic = f"failed_tests={','.join(failed_tests[:5])}"

    return ScoringResult(
        outcome=outcome,
        problem_instance_id=instance_id,
        tests_passed=passed,
        tests_failed=failed,
        tests_total=total,
        pass_rate=round(pass_rate, 4),
        diagnostic=diagnostic,
        elapsed_s=time.monotonic() - started_at,
    )


# ===========================================================================
# Canonical safe git-apply subprocess (mirrors B.1 shape)
# ===========================================================================


async def _git_apply_patch(
    worktree_path: Path,
    patch_text: str,
    *,
    timeout_s: Optional[float] = None,
) -> Tuple[bool, str]:
    """Apply patch_text to worktree_path via canonical safe git apply.

    Composes asyncio.create_subprocess_exec (program plus args list,
    NEVER shell=True) - the SAME shape Phase B.1's _run_git uses.
    Returns (success, stderr_tail). NEVER raises except CancelledError.
    """
    if not patch_text.strip():
        return False, "empty_patch"
    timeout = (
        timeout_s if timeout_s is not None and timeout_s > 0
        else _resolve_git_op_timeout_s()
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "apply", "--index", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree_path),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
    # Slice 3 subprocess wiring — the evaluator_trace_observer reads
    # this contextvar via Task.get_context to surface "which subprocess
    # is this task currently blocked on" in trace frames. Defensive:
    # trace_subprocess NEVER raises. AST-pinned at the test layer.
    from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
        trace_subprocess,
    )
    with trace_subprocess(
        proc.pid if proc.pid else 0,
        f"git apply --index cwd={worktree_path}",
    ):
        try:
            _stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=patch_text.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            return False, f"git_apply_timeout_after_{timeout:.0f}s"
        except asyncio.CancelledError:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            raise
    rc = proc.returncode if proc.returncode is not None else -1
    if rc == 0:
        return True, ""
    stderr_text = (
        stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    )
    return False, stderr_text.strip()[:300]


# ===========================================================================
# Test-file selection from PreparedProblem.target_paths
# ===========================================================================


def _resolve_test_files_under_worktree(
    prepared: PreparedProblem,
) -> Tuple[Path, ...]:
    """Filter prepared.target_paths to test files plus resolve to
    absolute paths under the worktree. Pure function; NEVER raises.

    SWE-Bench-Pro scores ONLY the failing tests added by the
    problem's test_patch - NOT the whole repo's test suite.
    """
    out: List[Path] = []
    seen: set = set()
    for rel in prepared.target_paths:
        if not _is_test_file(rel):
            continue
        abs_path = prepared.worktree_path / rel
        if abs_path in seen:
            continue
        seen.add(abs_path)
        out.append(abs_path)
    return tuple(out)


# ===========================================================================
# Public API - score_evaluation
# ===========================================================================


async def score_evaluation(
    result: EvaluationResult,
    problem: ProblemSpec,
    *,
    test_timeout_s: Optional[float] = None,
    reject_test_modifications: Optional[bool] = None,
) -> ScoringResult:
    """Score a single SWE-Bench-Pro EvaluationResult.

    Pure-data composition: reproduces the fix in a fresh worktree
    from (captured_patch, problem) alone, runs the failing tests
    added by problem.test_patch, and classifies the outcome.

    Returns a populated ScoringResult; NEVER raises except
    asyncio.CancelledError (cooperative cancel; cleanup still runs
    in finally).
    """
    started_at = time.monotonic()
    instance_id = getattr(problem, "instance_id", "") or ""

    if not swe_bench_pro_enabled():
        return ScoringResult(
            outcome=ScoreOutcome.SKIPPED,
            problem_instance_id=instance_id,
            diagnostic="master_flag_off",
            elapsed_s=time.monotonic() - started_at,
        )

    if result.outcome != EvaluationOutcome.RESOLVED:
        return ScoringResult(
            outcome=ScoreOutcome.SKIPPED,
            problem_instance_id=instance_id,
            diagnostic=f"evaluation_outcome={result.outcome.value}",
            elapsed_s=time.monotonic() - started_at,
        )

    captured_patch = result.captured_patch or ""
    if not captured_patch.strip():
        return ScoringResult(
            outcome=ScoreOutcome.SCORING_ERROR,
            problem_instance_id=instance_id,
            diagnostic="no_patch",
            elapsed_s=time.monotonic() - started_at,
        )

    if _resolve_reject_test_mods(reject_test_modifications):
        cheat_files = _patch_modifies_test_files(captured_patch)
        if cheat_files:
            joined = ",".join(cheat_files[:3])
            return ScoringResult(
                outcome=ScoreOutcome.FAIL,
                problem_instance_id=instance_id,
                diagnostic=f"patch_modified_tests:{joined}",
                elapsed_s=time.monotonic() - started_at,
            )

    # Slice 6 — task-naming completeness: from this point the current
    # task is renamed to ``swe_bench_pro:score_evaluation:<instance_id>``
    # so the EvaluatorTraceObserver can isolate the scoring phase
    # (git apply + TestRunner.run) from the upstream evaluator path.
    # ``prepare_problem`` carries its own ``task_phase(PREPARE_PROBLEM,
    # ...)`` wrapper — it temporarily takes over the name during that
    # nested call and restores ``score_evaluation:<id>`` on return.
    async with task_phase(EvaluatorPhase.SCORE_EVALUATION, instance_id):
        # Slice 65 — containerized execution backend (gated, default-OFF).
        # When enabled AND the problem carries a Docker image tag, run
        # apply+test inside the prepared per-problem image (full repo env —
        # PyQt/Node/etc. the bare local env lacks) instead of the local
        # worktree path below. Composes the SAME _finalize_score classify.
        # NEVER raises into here — infra failures come back as result.error
        # and map to SCORING_ERROR (the local path stays byte-identical when
        # the flag is off). No prepare/cleanup: the container is ephemeral.
        from backend.core.ouroboros.governance.swe_bench_pro import (
            container_engine as _container,
        )
        if _container.should_use_container(problem):
            c_result = await _container.run_container_scoring(
                problem, captured_patch,
                timeout_s=_resolve_test_timeout_s(test_timeout_s),
            )
            if c_result.error:
                return ScoringResult(
                    outcome=ScoreOutcome.SCORING_ERROR,
                    problem_instance_id=instance_id,
                    diagnostic=f"container:{c_result.error}",
                    elapsed_s=time.monotonic() - started_at,
                )
            return _finalize_score(c_result, instance_id, started_at)

        prepared, harness_outcome = await prepare_problem(problem)
        if prepared is None or harness_outcome != HarnessOutcome.READY:
            return ScoringResult(
                outcome=ScoreOutcome.SCORING_ERROR,
                problem_instance_id=instance_id,
                diagnostic=(
                    f"prepare_failed:{getattr(harness_outcome, 'value', '')}"
                ),
                elapsed_s=time.monotonic() - started_at,
            )

        try:
            applied, apply_stderr = await _git_apply_patch(
                prepared.worktree_path, captured_patch,
            )
            if not applied:
                return ScoringResult(
                    outcome=ScoreOutcome.SCORING_ERROR,
                    problem_instance_id=instance_id,
                    diagnostic=f"apply_failed:{apply_stderr[:200]}",
                    elapsed_s=time.monotonic() - started_at,
                )

            test_files = _resolve_test_files_under_worktree(prepared)
            if not test_files:
                return ScoringResult(
                    outcome=ScoreOutcome.SCORING_ERROR,
                    problem_instance_id=instance_id,
                    diagnostic="no_test_files_in_test_patch",
                    elapsed_s=time.monotonic() - started_at,
                )

            timeout = _resolve_test_timeout_s(test_timeout_s)
            runner = TestRunner(prepared.worktree_path, timeout=timeout)
            try:
                test_result = await runner.run(test_files)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[SWEBenchPro.Scorer] TestRunner.run raised for "
                    "instance=%s", instance_id, exc_info=True,
                )
                return ScoringResult(
                    outcome=ScoreOutcome.SCORING_ERROR,
                    problem_instance_id=instance_id,
                    diagnostic="test_runner_raised",
                    elapsed_s=time.monotonic() - started_at,
                )

            return _finalize_score(test_result, instance_id, started_at)

        except asyncio.CancelledError:
            logger.info(
                "[SWEBenchPro.Scorer] score_evaluation cancelled for "
                "instance=%s after %.1fs (cleanup will run)",
                instance_id, time.monotonic() - started_at,
            )
            raise
        finally:
            try:
                await cleanup_prepared(prepared)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[SWEBenchPro.Scorer] cleanup_prepared raised for "
                    "instance=%s", instance_id, exc_info=True,
                )


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by section 33.3 walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Returns count successfully
    registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=SCORE_TEST_TIMEOUT_ENV_VAR,
            type=FlagType.INT,
            default=int(_DEFAULT_TEST_TIMEOUT_S),
            description=(
                "Bounded pytest invocation timeout (seconds) for the "
                "SWE-Bench-Pro Phase C scorer. Default 600s = 10 min. "
                "Caps the canonical TestRunner.run() timeout."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "scorer.py"
            ),
            example=str(int(_DEFAULT_TEST_TIMEOUT_S)),
            since="v3.7 Phase 2 Phase C (2026-05-12)",
        ),
        FlagSpec(
            name=SCORE_REJECT_TEST_MODS_ENV_VAR,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Canonical SWE-Bench rule: patches that modify test "
                "files are cheats and are scored FAIL outright with "
                "diagnostic 'patch_modified_tests:<path>'. Default "
                "TRUE mirrors the upstream benchmark contract."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "scorer.py"
            ),
            example="true",
            since="v3.7 Phase 2 Phase C (2026-05-12)",
        ),
        FlagSpec(
            name=SCORE_GIT_OP_TIMEOUT_ENV_VAR,
            type=FlagType.INT,
            default=int(_DEFAULT_GIT_OP_TIMEOUT_S),
            description=(
                "Subprocess timeout (seconds) for the scorer's "
                "canonical safe git-apply invocation. Default 60s. "
                "Mirrors Phase B.1's per-op git timeout discipline."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "scorer.py"
            ),
            example=str(int(_DEFAULT_GIT_OP_TIMEOUT_S)),
            since="v3.7 Phase 2 Phase C (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro.Scorer] flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "SCORE_GIT_OP_TIMEOUT_ENV_VAR",
    "SCORE_REJECT_TEST_MODS_ENV_VAR",
    "SCORE_TEST_TIMEOUT_ENV_VAR",
    "SCORING_RESULT_SCHEMA_VERSION",
    "ScoreOutcome",
    "ScoringResult",
    "register_flags",
    "score_evaluation",
]
