"""SWE-Bench-Pro parallel evaluation rig - Phase E (PRD section 40.7.10-e).

Async generator that drives N problems concurrently through the full
Phase B.2.2 -> Phase C -> Phase D pipeline:

    for problem in problems:
        async with bounded_semaphore:
            ev_result = await evaluate_problem(problem, ...)   # B.2.2
            sc_result = await score_evaluation(...)            # Phase C
            await store.record(ev_result, sc_result)           # Phase D
            yield EvaluationRecord(evaluation=..., scoring=...) as completed

Yields records in COMPLETION order (not submission order) so the
operator sees fast problems land first. Bounded concurrency via the
canonical hot-reload-safe `_process_singletons.get_semaphore`
primitive - no homegrown `asyncio.Semaphore` literal in this module.

Composition discipline
----------------------

  * Composes ONLY canonical surfaces:
      - `_process_singletons.get_semaphore` (the canonical hot-
        reload-safe concurrency primitive)
      - `evaluate_problem` (B.2.2 async facade)
      - `score_evaluation` (Phase C pure-data scorer)
      - `record_evaluation` / `get_default_store` (Phase D)
      - `EvaluationRecord` / `EvaluationResult` / `ScoringResult`
        canonical dataclasses

  * NO homegrown semaphore / lock / queue management beyond the
    canonical primitives + `asyncio.Queue` for the completion stream.
    AST pin in the spine forbids any `asyncio.Semaphore(` literal in
    the module body (the canonical `get_semaphore` is the single seam).

  * NO new master flag - composes Phase A's
    `JARVIS_SWE_BENCH_PRO_ENABLED` master via the underlying
    `evaluate_problem` gate. The rig's only knob is concurrency.

  * Cooperative cancel: `aclose()` on the async iterator cancels
    every in-flight task. Each task's `evaluate_problem` /
    `score_evaluation` / `cleanup_prepared` honors the cancel through
    the contracts they already publish.

  * Per-task fail-closed: a defensive try/except wraps each task's
    pipeline so an unexpected raise (contract violation by
    `evaluate_problem` or `score_evaluation`) yields a synthetic
    record carrying the exception class name in the diagnostic
    rather than tearing down the whole rig.

  * Stream-not-batch: results land on an `asyncio.Queue` as they
    complete; the iterator yields them in completion order. Operators
    see progress live, even when concurrency=1 (each result yields
    as it finishes, not at the end).

Section 7 fail-closed contract
------------------------------

Every code path produces an `EvaluationRecord` rather than raising,
except `asyncio.CancelledError` which propagates per the orchestrator
POSTMORTEM convention (in-flight tasks are cancelled in the same
cooperative pass).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Iterable,
    List,
    Optional,
    Set,
)

from backend.core.ouroboros.governance._process_singletons import (
    get_semaphore,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EvaluationOutcome,
    EvaluationResult,
    evaluate_problem,
)
from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
    EvaluationRecord,
    EvaluationResultStore,
    get_default_store,
)
from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
    ScoreOutcome,
    ScoringResult,
    score_evaluation,
)


logger = logging.getLogger("Ouroboros.SWEBenchPro.ParallelEval")


# ===========================================================================
# Env vocabulary
# ===========================================================================


PARALLEL_CONCURRENCY_ENV_VAR: str = (
    "JARVIS_SWE_BENCH_PRO_PARALLEL_CONCURRENCY"
)


_DEFAULT_CONCURRENCY: int = 4


# Canonical key for the process-wide hot-reload-safe semaphore.
# Distinct from L3 subagent / candidate-generator / patch-benchmark
# semaphore keys so Phase E concurrency does not steal slots from
# unrelated subsystems.
_SEMAPHORE_KEY: str = "swe_bench_pro_parallel_eval"


# ===========================================================================
# Frozen ParallelEvalProgress dataclass (optional progress observer)
# ===========================================================================


@dataclass(frozen=True)
class ParallelEvalProgress:
    """Snapshot of the rig's state, passed to ``progress_callback``
    on each completion. Pure data; immutable; never references the
    underlying task set."""

    total_submitted: int
    total_completed: int
    pending: int
    pass_count: int
    fail_count: int
    partial_count: int
    error_count: int
    skipped_count: int
    last_instance_id: str
    last_score_outcome: str
    snapshot_iso: str


# ===========================================================================
# Env loaders (NEVER raise)
# ===========================================================================


def _resolve_concurrency(explicit: Optional[int]) -> int:
    """Resolve bounded-concurrency: argument > env > default.
    Invalid env values fall back to default with a WARN log."""
    if explicit is not None and explicit > 0:
        return int(explicit)
    raw = os.environ.get(PARALLEL_CONCURRENCY_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_CONCURRENCY
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("must be > 0")
        return value
    except (ValueError, TypeError):
        logger.warning(
            "[SWEBenchPro.ParallelEval] invalid %s=%r - using default %d",
            PARALLEL_CONCURRENCY_ENV_VAR, raw, _DEFAULT_CONCURRENCY,
        )
        return _DEFAULT_CONCURRENCY


# ===========================================================================
# Synthetic record helpers (defensive contract violation paths)
# ===========================================================================


def _synthetic_record(
    instance_id: str,
    eval_outcome: EvaluationOutcome,
    score_outcome: ScoreOutcome,
    diagnostic: str,
) -> EvaluationRecord:
    """Build a synthetic EvaluationRecord for paths where the
    canonical pipeline yielded no usable result (e.g., a
    contract-violating raise). The record stamps the diagnostic
    into both nested dataclasses so consumers reading either
    surface see the failure mode."""
    ev = EvaluationResult(
        outcome=eval_outcome,
        problem_instance_id=instance_id,
        terminal_reason_code=diagnostic[:256],
    )
    sc = ScoringResult(
        outcome=score_outcome,
        problem_instance_id=instance_id,
        diagnostic=diagnostic[:200],
    )
    return EvaluationRecord(
        evaluation=ev,
        scoring=sc,
        recorded_at_iso=datetime.now(tz=timezone.utc).isoformat(),
    )


def _synthetic_skipped_scoring(
    instance_id: str,
) -> ScoringResult:
    """When ``score_each=False`` the rig still produces a record
    (so the operator sees evaluation outcomes); the scoring half
    is marked SKIPPED with a deterministic diagnostic."""
    return ScoringResult(
        outcome=ScoreOutcome.SKIPPED,
        problem_instance_id=instance_id,
        diagnostic="scoring_disabled_in_parallel_eval",
    )


# ===========================================================================
# Per-problem worker
# ===========================================================================


async def _run_one_problem(
    problem: ProblemSpec,
    *,
    semaphore: asyncio.Semaphore,
    intake_service: Any,
    operation_ledger: Optional[Any],
    broker: Optional[Any],
    eval_timeout_s: Optional[float],
    score_each: bool,
    score_test_timeout_s: Optional[float],
    reject_test_modifications: Optional[bool],
    record_each: bool,
    store: EvaluationResultStore,
    out_queue: "asyncio.Queue[EvaluationRecord]",
) -> None:
    """One problem's full pipeline. Holds the bounded-concurrency
    semaphore for the entire evaluate -> score -> record path so
    the rig caps total resource consumption (worktrees + pytest
    processes + JSONL appenders) not just orchestrator dispatch.

    NEVER raises (except CancelledError, which propagates so the
    rig's task cancellation works). Any other exception produces
    a synthetic record + logs at WARNING.
    """
    instance_id = getattr(problem, "instance_id", "") or ""
    record: Optional[EvaluationRecord] = None
    try:
        async with semaphore:
            try:
                ev_result = await evaluate_problem(
                    problem,
                    intake_service=intake_service,
                    operation_ledger=operation_ledger,
                    broker=broker,
                    timeout_s=eval_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - contract defensive
                logger.warning(
                    "[SWEBenchPro.ParallelEval] evaluate_problem "
                    "raised for instance=%s: %s",
                    instance_id, type(exc).__name__, exc_info=True,
                )
                record = _synthetic_record(
                    instance_id,
                    EvaluationOutcome.INGEST_FAILED,
                    ScoreOutcome.SCORING_ERROR,
                    f"evaluator_raised:{type(exc).__name__}",
                )
                await out_queue.put(record)
                return

            if score_each:
                try:
                    sc_result = await score_evaluation(
                        ev_result, problem,
                        test_timeout_s=score_test_timeout_s,
                        reject_test_modifications=reject_test_modifications,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[SWEBenchPro.ParallelEval] score_evaluation "
                        "raised for instance=%s: %s",
                        instance_id, type(exc).__name__, exc_info=True,
                    )
                    sc_result = ScoringResult(
                        outcome=ScoreOutcome.SCORING_ERROR,
                        problem_instance_id=instance_id,
                        diagnostic=f"scorer_raised:{type(exc).__name__}",
                    )
            else:
                sc_result = _synthetic_skipped_scoring(instance_id)

            recorded_at_iso = datetime.now(tz=timezone.utc).isoformat()
            record = EvaluationRecord(
                evaluation=ev_result,
                scoring=sc_result,
                recorded_at_iso=recorded_at_iso,
            )

            if record_each:
                try:
                    await store.record(ev_result, sc_result)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - record is fail-closed
                    logger.debug(
                        "[SWEBenchPro.ParallelEval] store.record raised",
                        exc_info=True,
                    )

        await out_queue.put(record)
    except asyncio.CancelledError:
        raise


# ===========================================================================
# Public API - parallel_evaluate (async generator)
# ===========================================================================


async def parallel_evaluate(
    problems: Iterable[ProblemSpec],
    *,
    intake_service: Any,
    concurrency: Optional[int] = None,
    score_each: bool = True,
    record_each: bool = True,
    operation_ledger: Optional[Any] = None,
    broker: Optional[Any] = None,
    store: Optional[EvaluationResultStore] = None,
    eval_timeout_s: Optional[float] = None,
    score_test_timeout_s: Optional[float] = None,
    reject_test_modifications: Optional[bool] = None,
    progress_callback: Optional[
        Callable[[ParallelEvalProgress], None]
    ] = None,
) -> AsyncIterator[EvaluationRecord]:
    """Drive ``problems`` concurrently through evaluate -> score ->
    record, yielding :class:`EvaluationRecord` instances as they
    complete (NOT in submission order).

    Parameters
    ----------
    problems:
        Iterable of :class:`ProblemSpec`. Drained once; can be a
        generator. Materialized into a list for length tallying.
    intake_service:
        REQUIRED - passed through to every ``evaluate_problem`` call.
    concurrency:
        Bounded concurrency cap. Precedence: argument > env
        :data:`PARALLEL_CONCURRENCY_ENV_VAR` > default 4. The
        underlying semaphore is process-wide (composes the canonical
        :func:`get_semaphore` hot-reload-safe primitive with key
        :data:`_SEMAPHORE_KEY`).
    score_each:
        When True (default), each evaluation flows through Phase C
        scorer. When False, the scoring half of the record is a
        synthetic SKIPPED ScoringResult.
    record_each:
        When True (default), each (evaluation, scoring) pair is
        persisted into the result store (default-singleton if no
        ``store`` injected).
    operation_ledger / broker / eval_timeout_s /
    score_test_timeout_s / reject_test_modifications:
        Passed through to ``evaluate_problem`` / ``score_evaluation``
        unchanged.
    store:
        Optional injected :class:`EvaluationResultStore`. When None
        (default), the rig uses :func:`get_default_store`.
    progress_callback:
        Optional sync callable receiving a frozen
        :class:`ParallelEvalProgress` snapshot on each completion.
        Errors raised by the callback are swallowed at DEBUG so a
        buggy observer cannot break the rig.

    Yields
    ------
    EvaluationRecord
        One per problem, in completion order. The iterator exhausts
        when every submitted task has either yielded a record or
        been cancelled. NEVER raises except ``asyncio.CancelledError``.

    Notes
    -----
    * Empty ``problems`` -> the iterator exhausts immediately,
      yielding nothing.
    * Per-task contract violations (unexpected raises from
      evaluate_problem / score_evaluation) produce synthetic records
      rather than tearing down the rig.
    * Iterator early-exit (caller breaks) cancels every in-flight
      task; pending records are dropped. Cleanup inside each task
      runs (evaluate_problem owns its finally block; score_evaluation
      owns its finally block).
    """
    problems_list: List[ProblemSpec] = list(problems)
    if not problems_list:
        return

    resolved_concurrency = _resolve_concurrency(concurrency)
    semaphore = get_semaphore(_SEMAPHORE_KEY, resolved_concurrency)
    resolved_store = store if store is not None else get_default_store()

    out_queue: "asyncio.Queue[EvaluationRecord]" = asyncio.Queue()

    tasks: Set["asyncio.Task[None]"] = set()
    for problem in problems_list:
        # Slice 2 naming convention — the evaluator_trace_observer
        # filters tasks by ``swe_bench_pro:`` prefix and classifies
        # phase from the suffix. AST-pinned: every asyncio.create_task
        # in evaluator path MUST carry ``name=swe_bench_pro:<phase>:<id>``.
        _task_name = (
            f"swe_bench_pro:parallel:{problem.instance_id}"
        )
        task = asyncio.create_task(
            _run_one_problem(
                problem,
                semaphore=semaphore,
                intake_service=intake_service,
                operation_ledger=operation_ledger,
                broker=broker,
                eval_timeout_s=eval_timeout_s,
                score_each=score_each,
                score_test_timeout_s=score_test_timeout_s,
                reject_test_modifications=reject_test_modifications,
                record_each=record_each,
                store=resolved_store,
                out_queue=out_queue,
            ),
            name=_task_name,
        )
        tasks.add(task)

    expected = len(tasks)
    completed = 0
    pass_count = 0
    fail_count = 0
    partial_count = 0
    error_count = 0
    skipped_count = 0

    try:
        while completed < expected:
            # ``asyncio.wait_for(queue.get(), timeout=None)`` is
            # the canonical "wait until the next item arrives or
            # cancel propagates" pattern. We do not bound this
            # wait_for - the per-task ``evaluate_problem`` /
            # ``score_evaluation`` invocations carry their own
            # bounded timeouts; bounding here would cause spurious
            # rig-level timeouts when the operator deliberately
            # raised a per-op cap.
            record = await out_queue.get()
            completed += 1

            outcome = record.scoring.outcome
            if outcome == ScoreOutcome.PASS:
                pass_count += 1
            elif outcome == ScoreOutcome.FAIL:
                fail_count += 1
            elif outcome == ScoreOutcome.PARTIAL:
                partial_count += 1
            elif outcome == ScoreOutcome.SCORING_ERROR:
                error_count += 1
            elif outcome == ScoreOutcome.SKIPPED:
                skipped_count += 1

            if progress_callback is not None:
                try:
                    progress_callback(ParallelEvalProgress(
                        total_submitted=expected,
                        total_completed=completed,
                        pending=expected - completed,
                        pass_count=pass_count,
                        fail_count=fail_count,
                        partial_count=partial_count,
                        error_count=error_count,
                        skipped_count=skipped_count,
                        last_instance_id=(
                            record.evaluation.problem_instance_id
                        ),
                        last_score_outcome=outcome.value,
                        snapshot_iso=datetime.now(
                            tz=timezone.utc,
                        ).isoformat(),
                    ))
                except Exception:  # noqa: BLE001 - observer is best-effort
                    logger.debug(
                        "[SWEBenchPro.ParallelEval] progress_callback "
                        "raised", exc_info=True,
                    )

            yield record
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        # Drain cancellations cooperatively. Each per-task coroutine
        # raises CancelledError, which is caught at task level - so
        # gathering with return_exceptions=True lets us wait for
        # cleanup without re-raising into the rig.
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        # Cooperative cleanup on early iterator exit (caller broke
        # out of the loop). Cancel any unfinished tasks; let each
        # task's finally block run via gather.
        if completed < expected:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by section 33.3 walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Returns count
    successfully registered. NEVER raises."""
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
            name=PARALLEL_CONCURRENCY_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_CONCURRENCY,
            description=(
                "Bounded concurrency for SWE-Bench-Pro Phase E "
                "parallel evaluation rig. Default 4 - tuned for a "
                "single workstation (4 worktrees + 4 orchestrator "
                "dispatches + 4 pytest invocations concurrently). "
                "Operators on larger infra flip higher; on memory-"
                "constrained envs flip to 1 or 2. The underlying "
                "semaphore is process-wide via the canonical "
                "_process_singletons.get_semaphore hot-reload-safe "
                "primitive, so concurrent invocations of "
                "parallel_evaluate share the same slot pool."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "parallel_eval.py"
            ),
            example=str(_DEFAULT_CONCURRENCY),
            since="v3.7 Phase 2 Phase E (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro.ParallelEval] flag registration failed "
                "for %s", getattr(spec, "name", "?"), exc_info=True,
            )
    return count


__all__ = [
    "PARALLEL_CONCURRENCY_ENV_VAR",
    "ParallelEvalProgress",
    "parallel_evaluate",
    "register_flags",
]
