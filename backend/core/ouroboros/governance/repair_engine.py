"""L2 Iterative Self-Repair Loop Engine

Provides configuration, runtime budget tracking, and FSM-driven repair orchestration
for Ouroboros governance operations that fail validation.

The repair loop implements:
- Multi-iteration classification and fix generation
- Test-driven repair with failure class tracking
- Adaptive timeout and cost budgeting
- Flaky test detection and confirmation
- Progress tracking and early termination

This module is structured as:
1. **RepairBudget** - Immutable configuration loaded from environment
2. **L2State / L2Event** - FSM state and event enumerations
3. **RepairIterationRecord** - Per-iteration ledger payload
4. **RepairResult** - Terminal outcome returned to the orchestrator
5. **RepairEngine** - FSM executor and repair orchestration
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RepairBudget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairBudget:
    """Immutable repair loop resource and iteration budget.

    Loaded from environment variables at system startup. All fields are frozen
    and cannot be mutated after construction.

    Parameters
    ----------
    enabled : bool
        Whether L2 iterative repair is enabled. Set via ``JARVIS_L2_ENABLED``
        (default: ``True``). L2 closes the self-repair loop on validation
        failure — Manifesto §6 threshold-triggered neuroplasticity.
    max_iterations : int
        Maximum repair iterations before hard stop. Set via ``JARVIS_L2_MAX_ITERS``
        (default: ``5``).
    timebox_s : float
        Total wall-clock time budget for entire repair loop in seconds.
        Set via ``JARVIS_L2_TIMEBOX_S`` (default: ``120.0``).
    min_deadline_remaining_s : float
        Minimum remaining deadline before stopping repair. If operation deadline
        is less than this value, repair halts. Set via ``JARVIS_L2_MIN_DEADLINE_S``
        (default: ``10.0``).
    per_iteration_test_timeout_s : float
        Test execution timeout per iteration in seconds. Set via ``JARVIS_L2_ITER_TEST_TIMEOUT_S``
        (default: ``60.0``).
    max_diff_lines : int
        Maximum diff lines per candidate. Set via ``JARVIS_L2_MAX_DIFF_LINES``
        (default: ``150``).
    max_files_changed : int
        Maximum files changed per candidate. Set via ``JARVIS_L2_MAX_FILES_CHANGED``
        (default: ``3``).
    max_total_validation_runs : int
        Maximum total validation/test runs across all iterations.
        Set via ``JARVIS_L2_MAX_VALIDATION_RUNS`` (default: ``8``).
    no_progress_streak_kill : int
        Kill repair after N consecutive failures with no progress.
        Set via ``JARVIS_L2_NO_PROGRESS_KILL`` (default: ``2``).
    max_class_retries : Dict[str, int]
        Max retries per failure class. Keys: ``"syntax"``, ``"test"``, ``"flake"``, ``"env"``.
        Set via ``JARVIS_L2_CLASS_RETRIES_JSON`` (default: ``{"syntax":2,"test":3,"flake":2,"env":1}``).
    flake_confirm_reruns : int
        How many times to rerun a passing test to confirm it's not flaky.
        Set via ``JARVIS_L2_FLAKE_RERUNS`` (default: ``1``).
    """

    enabled: bool = True
    max_iterations: int = 5
    timebox_s: float = 120.0
    min_deadline_remaining_s: float = 10.0
    per_iteration_test_timeout_s: float = 60.0
    max_diff_lines: int = 150
    max_files_changed: int = 3
    max_total_validation_runs: int = 8
    no_progress_streak_kill: int = 2
    max_class_retries: Dict[str, int] = dataclasses.field(
        default_factory=lambda: {"syntax": 2, "test": 3, "flake": 2, "env": 1}
    )
    flake_confirm_reruns: int = 1

    @classmethod
    def from_env(cls) -> RepairBudget:
        """Load RepairBudget configuration from environment variables.

        All environment variables are optional. Missing variables fall back to
        defaults. For ``JARVIS_L2_CLASS_RETRIES_JSON``, parse errors log a
        warning and use the default dict.

        Returns
        -------
        RepairBudget
            Frozen budget instance with values read from environment.
        """
        # Boolean parsing: L2 defaults to enabled (Manifesto §6 — the
        # self-repair loop is load-bearing for the Ouroboros cycle).
        # Accept explicit "false" / "0" / "no" to opt out.
        enabled_str = os.environ.get("JARVIS_L2_ENABLED", "true").lower()
        enabled = enabled_str not in ("false", "0", "no", "off")

        # Integer parsing
        max_iterations = int(os.environ.get("JARVIS_L2_MAX_ITERS", "5"))
        max_diff_lines = int(os.environ.get("JARVIS_L2_MAX_DIFF_LINES", "150"))
        max_files_changed = int(os.environ.get("JARVIS_L2_MAX_FILES_CHANGED", "3"))
        max_total_validation_runs = int(os.environ.get("JARVIS_L2_MAX_VALIDATION_RUNS", "8"))
        no_progress_streak_kill = int(os.environ.get("JARVIS_L2_NO_PROGRESS_KILL", "2"))
        flake_confirm_reruns = int(os.environ.get("JARVIS_L2_FLAKE_RERUNS", "1"))

        # Float parsing
        timebox_s = float(os.environ.get("JARVIS_L2_TIMEBOX_S", "120.0"))
        min_deadline_remaining_s = float(os.environ.get("JARVIS_L2_MIN_DEADLINE_S", "10.0"))
        per_iteration_test_timeout_s = float(os.environ.get("JARVIS_L2_ITER_TEST_TIMEOUT_S", "60.0"))

        # JSON parsing with fallback to default
        max_class_retries_json = os.environ.get("JARVIS_L2_CLASS_RETRIES_JSON")
        if max_class_retries_json:
            try:
                max_class_retries = json.loads(max_class_retries_json)
            except (json.JSONDecodeError, ValueError) as e:
                _logger.warning(
                    "Failed to parse JARVIS_L2_CLASS_RETRIES_JSON: %s, using defaults",
                    e,
                )
                max_class_retries = cls.__dataclass_fields__["max_class_retries"].default_factory()
        else:
            max_class_retries = cls.__dataclass_fields__["max_class_retries"].default_factory()

        return cls(
            enabled=enabled,
            max_iterations=max_iterations,
            timebox_s=timebox_s,
            min_deadline_remaining_s=min_deadline_remaining_s,
            per_iteration_test_timeout_s=per_iteration_test_timeout_s,
            max_diff_lines=max_diff_lines,
            max_files_changed=max_files_changed,
            max_total_validation_runs=max_total_validation_runs,
            no_progress_streak_kill=no_progress_streak_kill,
            max_class_retries=max_class_retries,
            flake_confirm_reruns=flake_confirm_reruns,
        )


# ---------------------------------------------------------------------------
# L2 FSM enumerations
# ---------------------------------------------------------------------------


class L2State(str, enum.Enum):
    L2_INIT = "L2_INIT"
    L2_PREPARE_BASELINE = "L2_PREPARE_BASELINE"
    L2_GENERATE_PATCH = "L2_GENERATE_PATCH"
    L2_MATERIALIZE_CANDIDATE = "L2_MATERIALIZE_CANDIDATE"
    L2_RUN_VALIDATION = "L2_RUN_VALIDATION"
    L2_CLASSIFY_FAILURE = "L2_CLASSIFY_FAILURE"
    L2_EVALUATE_PROGRESS = "L2_EVALUATE_PROGRESS"
    L2_DECIDE_RETRY = "L2_DECIDE_RETRY"
    L2_BUILD_REPAIR_PROMPT = "L2_BUILD_REPAIR_PROMPT"
    L2_CONVERGED = "L2_CONVERGED"
    L2_STOPPED = "L2_STOPPED"
    L2_ABORTED = "L2_ABORTED"


class L2Event(str, enum.Enum):
    EV_START = "EV_START"
    EV_PATCH_GENERATED = "EV_PATCH_GENERATED"
    EV_PATCH_INVALID = "EV_PATCH_INVALID"
    EV_VALIDATION_PASS = "EV_VALIDATION_PASS"
    EV_VALIDATION_FAIL = "EV_VALIDATION_FAIL"
    EV_FAILURE_CLASSIFIED_SYNTAX = "EV_FAILURE_CLASSIFIED_SYNTAX"
    EV_FAILURE_CLASSIFIED_TEST = "EV_FAILURE_CLASSIFIED_TEST"
    EV_FAILURE_CLASSIFIED_ENV = "EV_FAILURE_CLASSIFIED_ENV"
    EV_FAILURE_CLASSIFIED_FLAKE = "EV_FAILURE_CLASSIFIED_FLAKE"
    EV_PROGRESS = "EV_PROGRESS"
    EV_NO_PROGRESS = "EV_NO_PROGRESS"
    EV_OSCILLATION_DETECTED = "EV_OSCILLATION_DETECTED"
    EV_BUDGET_EXHAUSTED = "EV_BUDGET_EXHAUSTED"
    EV_NON_RETRYABLE_ENV = "EV_NON_RETRYABLE_ENV"
    EV_RETRY_ALLOWED = "EV_RETRY_ALLOWED"
    EV_RETRY_DENIED = "EV_RETRY_DENIED"
    EV_CANCEL = "EV_CANCEL"
    EV_FATAL_INFRA = "EV_FATAL_INFRA"


# ---------------------------------------------------------------------------
# RepairIterationRecord and RepairResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairIterationRecord:
    """Ledger payload for one repair iteration. schema_version: repair.iter.v1"""

    schema_version: str = "repair.iter.v1"
    op_id: str = ""
    iteration: int = 0
    repair_state: str = ""
    failure_class: str = ""
    failure_signature_hash: str = ""
    patch_signature_hash: str = ""
    diff_lines: int = 0
    files_changed: int = 0
    validation_duration_s: float = 0.0
    outcome: str = ""    # "progress"|"no_progress"|"converged"|"stopped"|"aborted"
    stop_reason: Optional[str] = None
    model_id: str = ""
    provider_name: str = ""


@dataclass(frozen=True)
class RepairResult:
    """Terminal outcome returned by RepairEngine.run() to the orchestrator.

    Note: ``terminal`` is always ``"L2_CONVERGED"`` or ``"L2_STOPPED"``.
    ``"L2_ABORTED"`` is never returned — ``asyncio.CancelledError`` is
    re-raised directly so the orchestrator can handle POSTMORTEM itself.
    Non-CancelledError infra errors are returned as ``"L2_STOPPED"`` with a
    structured ``stop_reason`` (e.g. ``"sandbox_infra_error:OSError"``).
    """

    terminal: str                        # "L2_CONVERGED"|"L2_STOPPED"
    candidate: Optional[Dict[str, Any]]  # converged candidate dict, or None
    stop_reason: Optional[str]           # set when terminal=="L2_STOPPED"
    summary: Dict[str, Any]              # key metrics for ledger payload
    iterations: Tuple[RepairIterationRecord, ...]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _count_diff_lines(diff: str) -> int:
    """Count changed lines in a unified diff (+ and - lines, excluding +++ / ---)."""
    return sum(
        1 for ln in diff.splitlines()
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
    )


def _count_diff_files(diff: str) -> int:
    """Count distinct files changed in a unified diff.

    Counts ``+++ b/<path>`` headers; falls back to 1 for single-file
    diffs that lack file headers (schema 2b.1-diff normal case).
    """
    paths = {ln[6:].strip() for ln in diff.splitlines() if ln.startswith("+++ b/")}
    return len(paths) if paths else 1


def _patch_sig(diff: str) -> str:
    """SHA-256 hex digest of a unified diff (delegates to failure_classifier)."""
    from backend.core.ouroboros.governance.failure_classifier import patch_signature_hash
    return patch_signature_hash(diff)


# ---------------------------------------------------------------------------
# RepairEngine
# ---------------------------------------------------------------------------


class RepairEngine:
    """L2 iterative self-repair loop executor.

    Drives the bounded generate→sandbox-run→classify→revise loop.
    Called by the orchestrator after VALIDATE exhaustion when L2 is enabled.

    Parameters
    ----------
    budget:
        Frozen resource limits loaded from env (RepairBudget.from_env()).
    prime_provider:
        Provider with async generate(ctx, deadline, repair_context=None).
    repo_root:
        Path to the repository root (passed to sandbox_factory).
    sandbox_factory:
        Callable(repo_root, test_timeout_s) → async context manager.
        Defaults to RepairSandbox when None.
    ledger:
        Optional OperationLedger for audit trail.
    """

    def __init__(
        self,
        budget: RepairBudget,
        prime_provider: Any,
        repo_root: Any,
        sandbox_factory: Any = None,
        ledger: Any = None,
    ) -> None:
        self._budget = budget
        self._prime = prime_provider
        self._repo_root = repo_root
        self._ledger = ledger
        if sandbox_factory is None:
            from backend.core.ouroboros.governance.repair_sandbox import RepairSandbox
            self._sandbox_factory = RepairSandbox
        else:
            self._sandbox_factory = sandbox_factory
        self._classifier = _lazy_classifier()

    async def run(
        self,
        ctx: Any,
        _best_validation: Any,
        pipeline_deadline: datetime,
    ) -> RepairResult:
        """Execute the L2 repair loop.

        Parameters
        ----------
        ctx:
            OperationContext from the orchestrator.
        _best_validation:
            The best ValidationResult that failed (reserved for future use;
            first iteration uses ctx.generation.candidates[0] directly).
        pipeline_deadline:
            UTC datetime after which no new iteration should start.

        Returns
        -------
        RepairResult
            terminal == "L2_CONVERGED" if a passing candidate was found,
            terminal == "L2_STOPPED" otherwise.

        Raises
        ------
        asyncio.CancelledError
            Propagated immediately; never swallowed.
        """
        budget = self._budget
        iteration = 0
        repair_context = None
        seen_pairs: Set[Tuple[str, str]] = set()
        class_retry_counts: Dict[str, int] = {}
        no_progress_streak = 0
        prev_failing_count: Optional[int] = None
        prev_failure_class: Optional[str] = None
        total_validation_runs = 0
        t_start = time.monotonic()
        records: list = []
        model_id: str = getattr(ctx.generation, "model_id", "")
        provider_name: str = getattr(ctx.generation, "provider_name", "")

        def _stopped(reason: str) -> RepairResult:
            rec = RepairIterationRecord(
                op_id=ctx.op_id,
                iteration=iteration,
                repair_state=L2State.L2_STOPPED.value,
                outcome="stopped",
                stop_reason=reason,
            )
            self._emit_record(ctx.op_id, rec)
            # Note: `rec` is NOT appended to `records` because _stopped() always
            # returns immediately. The sentinel is included via `records + [rec]`
            # so that RepairResult.iterations captures it without mutating `records`.
            return RepairResult(
                terminal="L2_STOPPED",
                candidate=None,
                stop_reason=reason,
                summary={"iterations": iteration, "total_validation_runs": total_validation_runs},
                iterations=tuple(records + [rec]),
            )

        while True:
            # ----------------------------------------------------------------
            # Kill conditions (checked BEFORE every iteration)
            # ----------------------------------------------------------------
            now = datetime.now(timezone.utc)
            elapsed = time.monotonic() - t_start
            remaining_s = (pipeline_deadline - now).total_seconds()

            if remaining_s < budget.min_deadline_remaining_s:
                return _stopped("deadline_budget_exhausted")
            if elapsed > budget.timebox_s:
                return _stopped("timebox_exhausted")
            if iteration >= budget.max_iterations:
                return _stopped("max_iterations_exhausted")
            if total_validation_runs >= budget.max_total_validation_runs:
                return _stopped("max_validation_runs_exhausted")

            iteration += 1
            _logger.info(
                "\U0001f527 [L2 Repair] Iteration %d/%d starting (%.0fs elapsed, %.0fs remaining)",
                iteration, budget.max_iterations, elapsed, remaining_s,
            )

            # ----------------------------------------------------------------
            # GENERATE
            # ----------------------------------------------------------------
            if repair_context is not None:
                try:
                    gen_result = await self._prime.generate(
                        ctx, pipeline_deadline, repair_context=repair_context
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return _stopped(f"generate_error:{type(exc).__name__}")
                if not gen_result.candidates:
                    return _stopped("empty_candidates")
                current_candidate = dict(gen_result.candidates[0])
                model_id = getattr(gen_result, "model_id", model_id)
                provider_name = getattr(gen_result, "provider_name", provider_name)
            else:
                # First iteration: use candidate from failed L1 generation
                current_candidate = dict(ctx.generation.candidates[0])

            # ----------------------------------------------------------------
            # Diff budget check
            # ----------------------------------------------------------------
            diff = current_candidate.get("unified_diff", "")
            if _count_diff_lines(diff) > budget.max_diff_lines:
                return _stopped("diff_expansion_rejected")
            if _count_diff_files(diff) > budget.max_files_changed:
                return _stopped("diff_files_rejected")

            # ----------------------------------------------------------------
            # RUN in sandbox
            # ----------------------------------------------------------------
            total_validation_runs += 1
            file_path = current_candidate.get("file_path", "")
            sandbox_content = ""
            _patch_failed = False
            try:
                async with self._sandbox_factory(
                    self._repo_root, budget.per_iteration_test_timeout_s
                ) as sb:
                    try:
                        await sb.apply_patch(diff, file_path)
                    except RuntimeError as patch_exc:
                        # Patch application failure — the diff is malformed or
                        # doesn't match the file.  This is a candidate quality
                        # issue, not infra.  Treat as failed iteration so L2
                        # can retry with a new candidate.
                        _logger.info(
                            "[L2 Repair] Iteration %d: patch failed: %s",
                            iteration, patch_exc,
                        )
                        _patch_failed = True
                    if not _patch_failed:
                        target = sb.sandbox_root / file_path if file_path else None
                        if target is not None and hasattr(target, "exists") and target.exists():
                            try:
                                sandbox_content = target.read_text(encoding="utf-8", errors="replace")
                            except Exception:
                                sandbox_content = ""
                    svr = await sb.run_tests(
                        (), budget.per_iteration_test_timeout_s
                    ) if not _patch_failed else None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return _stopped(f"sandbox_infra_error:{type(exc).__name__}")

            # Patch application failure → treat as failed test (try next iteration)
            if _patch_failed or svr is None:
                from backend.core.ouroboros.governance.repair_sandbox import SandboxValidationResult
                svr = SandboxValidationResult(
                    passed=False,
                    stdout="patch application failed",
                    stderr="",
                    returncode=-1,
                    duration_s=0.0,
                )

            # ----------------------------------------------------------------
            # CONVERGED?
            # ----------------------------------------------------------------
            _test_status = "\u2705 PASSED" if svr.passed else f"\u274c FAILED ({getattr(svr, 'failure_class', 'unknown')})"
            _logger.info(
                "\U0001f527 [L2 Repair] Iteration %d/%d tests: %s",
                iteration, budget.max_iterations, _test_status,
            )
            if svr.passed:
                rec = RepairIterationRecord(
                    op_id=ctx.op_id,
                    iteration=iteration,
                    repair_state=L2State.L2_CONVERGED.value,
                    outcome="converged",
                    diff_lines=_count_diff_lines(diff),
                    files_changed=_count_diff_files(diff),
                    validation_duration_s=svr.duration_s,
                    model_id=model_id,
                    provider_name=provider_name,
                )
                self._emit_record(ctx.op_id, rec)
                records.append(rec)
                _logger.info(
                    "\U0001f527 [L2 Repair] \u2705 CONVERGED after %d iteration(s)! All tests pass.",
                    iteration,
                )
                return RepairResult(
                    terminal="L2_CONVERGED",
                    candidate=current_candidate,
                    stop_reason=None,
                    summary={
                        "iterations": iteration,
                        "total_validation_runs": total_validation_runs,
                    },
                    iterations=tuple(records),
                )

            # ----------------------------------------------------------------
            # CLASSIFY FAILURE
            # ----------------------------------------------------------------
            classification = self._classifier.classify(svr)
            if classification.is_non_retryable:
                return _stopped(f"non_retryable_env:{classification.env_subtype}")

            fail_class = classification.failure_class.value
            fail_sig = classification.failure_signature_hash
            patch_sig = _patch_sig(diff)

            # ----------------------------------------------------------------
            # EVALUATE PROGRESS
            # ----------------------------------------------------------------
            # Two of three progress conditions from the design doc are checked:
            #   1. Fewer failing tests than previous iteration.
            #   2. Failure severity improved (syntax/env → test).
            # Condition 3 (sig-hash set narrowing + diff_lines decrease) is
            # deferred to v1.1 per the implementation plan.
            current_failing_count = len(classification.failing_test_ids)
            is_progress = (
                prev_failing_count is None
                or current_failing_count < prev_failing_count
                or (
                    fail_class == "test"
                    and prev_failure_class is not None
                    and prev_failure_class in ("syntax", "env")
                )
            )
            prev_failing_count = current_failing_count
            prev_failure_class = fail_class

            # ----------------------------------------------------------------
            # OSCILLATION check
            # ----------------------------------------------------------------
            pair = (fail_sig, patch_sig)
            if pair in seen_pairs:
                return _stopped("oscillation_detected")
            seen_pairs.add(pair)

            # ----------------------------------------------------------------
            # NO-PROGRESS streak
            # ----------------------------------------------------------------
            if is_progress:
                no_progress_streak = 0
            else:
                no_progress_streak += 1
                if no_progress_streak >= budget.no_progress_streak_kill:
                    return _stopped("no_progress_streak")

            # ----------------------------------------------------------------
            # PER-CLASS retry cap
            # ----------------------------------------------------------------
            class_retry_counts[fail_class] = class_retry_counts.get(fail_class, 0) + 1
            if class_retry_counts[fail_class] > budget.max_class_retries.get(fail_class, 1):
                return _stopped(f"class_retries_exhausted:{fail_class}")

            # ----------------------------------------------------------------
            # BUILD REPAIR PROMPT (set repair_context for next iteration)
            # ----------------------------------------------------------------
            from backend.core.ouroboros.governance.op_context import RepairContext
            repair_context = RepairContext(
                iteration=iteration,
                max_iterations=budget.max_iterations,
                failure_class=fail_class,
                failure_signature_hash=fail_sig,
                failing_tests=classification.failing_test_ids,
                failure_summary=(svr.stdout + svr.stderr)[:300],
                current_candidate_content=sandbox_content,
                current_candidate_file_path=file_path,
            )

            outcome = "progress" if is_progress else "no_progress"
            rec = RepairIterationRecord(
                op_id=ctx.op_id,
                iteration=iteration,
                repair_state=L2State.L2_BUILD_REPAIR_PROMPT.value,
                failure_class=fail_class,
                failure_signature_hash=fail_sig,
                patch_signature_hash=patch_sig,
                diff_lines=_count_diff_lines(diff),
                files_changed=_count_diff_files(diff),
                validation_duration_s=svr.duration_s,
                outcome=outcome,
                model_id=model_id,
                provider_name=provider_name,
            )
            self._emit_record(ctx.op_id, rec)
            records.append(rec)

    def _emit_record(self, op_id: str, record: RepairIterationRecord) -> None:
        """Append a RepairIterationRecord to the ledger (if wired).

        ``OperationLedger.append`` is async, so we schedule it as a
        fire-and-forget task on the running event loop.  Called from both
        sync inner functions and the async ``run()`` body.
        """
        if self._ledger is None:
            return
        try:
            import asyncio as _asyncio
            from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
            entry = LedgerEntry(
                op_id=op_id,
                state=OperationState.SANDBOXING,
                data={"kind": "repair.iter.v1", **dataclasses.asdict(record)},
                entry_id=f"{op_id}:l2:iter:{record.iteration}",
            )
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(self._ledger.append(entry))
            except RuntimeError:
                # No running loop — silently drop (non-critical telemetry)
                pass
        except Exception:
            _logger.debug("repair_engine: failed to emit ledger record", exc_info=True)


# ---------------------------------------------------------------------------
# Lazy import helper (avoids circular import at module load time)
# ---------------------------------------------------------------------------


def _lazy_classifier():
    """Import and return a FailureClassifier instance."""
    from backend.core.ouroboros.governance.failure_classifier import FailureClassifier
    return FailureClassifier()
