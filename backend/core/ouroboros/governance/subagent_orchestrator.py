"""
SubagentOrchestrator — Phase 1 scaffolding.

Coordinates subagent dispatch for the `dispatch_subagent` Venom tool.
This Step-1 module is structural scaffolding only: it validates the
request, builds `SubagentContext` instances, gates on the master switch,
and returns well-formed `SubagentResult` objects with
`status=NOT_IMPLEMENTED` until Step 2 lands the actual
`AgenticExploreSubagent` class that performs the LLM-driven exploration.

Nothing in this file calls an LLM. Nothing in this file mutates disk.
The module is import-safe, side-effect-free on construction, and inert
when `JARVIS_SUBAGENT_DISPATCH_ENABLED` is explicitly set to `false`
(default `true` as of Phase 1 graduation 2026-04-18).

Manifesto alignment:
  §3 — Asynchronous tendrils: parallel fan-out uses `asyncio.TaskGroup`
       (Python 3.11+) with a transparent `asyncio.gather(..., return_exceptions=True)`
       fallback for 3.9/3.10 environments. No event-loop starvation;
       each subagent is a fully isolated coroutine.
  §5 — Intelligence-driven routing: the orchestrator stamps each context
       with primary + fallback provider names so Step-2's Nervous System
       Reflex can sever the primary thread and retry on Claude if DW
       stalls. Survival supersedes cost.
  §6 — The Iron Gate: `_ensure_dispatch_enabled` is the structural master
       switch. Any dispatch without the switch flipped raises
       `SubagentDispatchDisabled` before any work is performed.
  §7 — Absolute observability: every dispatch emits a spawn event, a
       result event, and a ledger entry (stubbed in Step 1, wired to
       real CommProtocol/Ledger in Step 4).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, List, Optional, Protocol, Tuple

from backend.core.ouroboros.governance.subagent_contracts import (
    SCHEMA_VERSION,
    SubagentContext,
    SubagentDispatchDisabled,
    SubagentError,
    SubagentFinding,
    SubagentRequest,
    SubagentResult,
    SubagentStatus,
    SubagentType,
    subagent_dispatch_enabled,
)

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger(__name__)


# ============================================================================
# Protocols the orchestrator consumes at construction.
#
# All three are dependency-injected so Step-1 tests can drive the
# orchestrator with in-memory fakes (no pipeline, no LLM, no disk).
# ============================================================================


class ExploreExecutor:
    """Structural protocol — Step 2 will implement this as AgenticExploreSubagent.

    Not an abstract base class; Step 2 concrete classes may or may not
    inherit from it. The orchestrator only needs `.explore(ctx)` to return
    an awaitable SubagentResult.
    """

    async def explore(self, ctx: SubagentContext) -> SubagentResult:  # pragma: no cover — interface stub
        raise NotImplementedError(
            "ExploreExecutor.explore is Step-2 work. "
            "Step-1 scaffolding returns NOT_IMPLEMENTED results."
        )


class CommSink(Protocol):
    """Structural protocol — emits subagent lifecycle events.

    Step-1 uses an in-process logger sink; Step-4 wires this to the real
    CommProtocol heartbeat channel for SerpentFlow rendering.
    """

    def emit_spawn(
        self,
        parent_op_id: str,
        subagent_id: str,
        subagent_type: SubagentType,
        goal: str,
    ) -> None: ...

    def emit_result(
        self,
        parent_op_id: str,
        subagent_id: str,
        result: SubagentResult,
    ) -> None: ...


class LoggerCommSink:
    """Default CommSink that logs to the stdlib logger.

    Sufficient for Step-1 commits. SerpentFlow wiring lands in Step 4.
    """

    def emit_spawn(
        self,
        parent_op_id: str,
        subagent_id: str,
        subagent_type: SubagentType,
        goal: str,
    ) -> None:
        logger.info(
            "[Subagent] spawn parent=%s sub=%s type=%s goal=%r",
            parent_op_id, subagent_id, subagent_type.value, goal[:80],
        )

    def emit_result(
        self,
        parent_op_id: str,
        subagent_id: str,
        result: SubagentResult,
    ) -> None:
        logger.info(
            "[Subagent] result parent=%s sub=%s status=%s cost=$%.4f "
            "findings=%d tool_calls=%d diversity=%d provider=%s fallback=%s "
            "duration=%.2fs",
            parent_op_id,
            subagent_id,
            result.status.value,
            result.cost_usd,
            len(result.findings),
            result.tool_calls,
            result.tool_diversity,
            result.provider_used or "-",
            result.fallback_triggered,
            result.duration_s,
        )


class LedgerSink(Protocol):
    """Structural protocol — appends subagent records to the parent op's ledger.

    Step-1 default is an in-memory collector; Step-4 wires this to the
    real `ledger.append_subagent_record()` call site.
    """

    def append(
        self,
        parent_op_id: str,
        subagent_id: str,
        result: SubagentResult,
    ) -> None: ...


@dataclass
class InMemoryLedgerSink:
    """Test-friendly LedgerSink that keeps a list of appended records.

    Replace with a real `OperationLedger` wrapper in Step 4.
    """

    records: List[Tuple[str, str, SubagentResult]] = field(default_factory=list)

    def append(
        self,
        parent_op_id: str,
        subagent_id: str,
        result: SubagentResult,
    ) -> None:
        self.records.append((parent_op_id, subagent_id, result))


# ============================================================================
# SubagentOrchestrator
# ============================================================================


class SubagentOrchestrator:
    """Coordinates subagent dispatch for the `dispatch_subagent` Venom tool.

    Responsibilities:
      * Gate dispatch on the master switch (`SubagentDispatchDisabled` if off).
      * Build a SubagentContext per dispatched unit (single or parallel).
      * Run single-dispatch or parallel-dispatch via asyncio.TaskGroup.
      * Aggregate parallel results into a single merged SubagentResult.
      * Emit spawn/result events and append ledger records.
      * Enforce per-parent cost budget (cooperative cancellation on
        `ParentBudgetExhausted`).

    Step 1 stubs the executor's actual exploration work — every dispatch
    returns `SubagentStatus.NOT_IMPLEMENTED` with a helpful error string.
    Step 2 swaps in a real `AgenticExploreSubagent` and the orchestrator
    starts producing real findings without further changes here.
    """

    def __init__(
        self,
        explore_factory: Callable[[], ExploreExecutor],
        comm: Optional[CommSink] = None,
        ledger: Optional[LedgerSink] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._explore_factory = explore_factory
        self._comm: CommSink = comm or LoggerCommSink()
        self._ledger: LedgerSink = ledger or InMemoryLedgerSink()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sub_seq_by_parent: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        parent_ctx: "OperationContext",
        request: SubagentRequest,
    ) -> SubagentResult:
        """Dispatch a subagent (single or parallel) on behalf of the parent op.

        Gated by `JARVIS_SUBAGENT_DISPATCH_ENABLED`. Returns a well-formed
        `SubagentResult`; raises `SubagentDispatchDisabled` if the master
        switch is off (callers should catch this and surface it as a
        tool-level error message to the model).
        """
        self._ensure_dispatch_enabled()

        if request.parallel_scopes <= 1:
            return await self._dispatch_single(parent_ctx, request, scope="")

        scopes = self._resolve_parallel_scopes(request)
        if len(scopes) == 1:
            return await self._dispatch_single(parent_ctx, request, scope=scopes[0])

        return await self._dispatch_parallel(parent_ctx, request, scopes)

    # ------------------------------------------------------------------
    # Master-switch guard (Manifesto §6 — structural refusal before work)
    # ------------------------------------------------------------------

    def _ensure_dispatch_enabled(self) -> None:
        if not subagent_dispatch_enabled():
            raise SubagentDispatchDisabled(
                "Subagent dispatch is disabled. Set JARVIS_SUBAGENT_DISPATCH_ENABLED=true "
                "to enable. Default is false until Phase 1 graduates per Manifesto §6."
            )

    # ------------------------------------------------------------------
    # Scope resolution for parallel fan-out
    # ------------------------------------------------------------------

    def _resolve_parallel_scopes(self, request: SubagentRequest) -> Tuple[str, ...]:
        """Pick up to MAX_PARALLEL_SCOPES scopes from the request.

        If the model supplied explicit `scope_paths`, use those. Otherwise
        fall back to scoping by target_files (each target becomes its
        own scope). Final count is clamped to MAX_PARALLEL_SCOPES and to
        the request's own `parallel_scopes` cap.
        """
        if request.scope_paths:
            return tuple(request.scope_paths[: request.parallel_scopes])
        if request.target_files:
            # Use distinct parent directories as scopes.
            dirs: List[str] = []
            seen: set[str] = set()
            for p in request.target_files:
                d = p.rsplit("/", 1)[0] if "/" in p else "."
                if d not in seen:
                    seen.add(d)
                    dirs.append(d)
                if len(dirs) >= request.parallel_scopes:
                    break
            return tuple(dirs)
        # No scopes given; degrade to a single scope at repo root.
        return (".",)

    # ------------------------------------------------------------------
    # Context construction
    # ------------------------------------------------------------------

    def _next_sub_id(self, parent_op_id: str) -> str:
        """Monotonic per-parent subagent id: op-<parent>::sub-<NN>.

        Mirrors the `_apply_multi_file_candidate` sub-op convention used in
        the multi-file APPLY pipeline. Monotonicity is important for the
        ledger's deterministic replay semantics.
        """
        n = self._sub_seq_by_parent.get(parent_op_id, 0) + 1
        self._sub_seq_by_parent[parent_op_id] = n
        return f"{parent_op_id}::sub-{n:02d}"

    def _build_sub_context(
        self,
        parent_ctx: "OperationContext",
        request: SubagentRequest,
        scope: str,
    ) -> SubagentContext:
        """Build a SubagentContext, clamping deadlines and inheriting provider."""
        parent_provider = self._infer_parent_provider(parent_ctx)
        sub_timeout = request.timeout_s
        clamped_deadline = self._clamp_deadline(parent_ctx, sub_timeout)
        cost_remaining = self._infer_parent_cost_remaining(parent_ctx)
        return SubagentContext(
            parent_op_id=self._parent_op_id(parent_ctx),
            parent_ctx=parent_ctx,
            subagent_id=self._next_sub_id(self._parent_op_id(parent_ctx)),
            subagent_type=request.subagent_type,
            request=request,
            deadline=clamped_deadline,
            scope_path=scope,
            yield_requested=False,
            cost_remaining_usd=cost_remaining,
            primary_provider_name=parent_provider,
            fallback_provider_name="claude-api",
            tool_loop=None,  # Step-2 wires the ToolLoopCoordinator.
        )

    def _parent_op_id(self, parent_ctx: "OperationContext") -> str:
        """Defensive accessor — parent ctx may be a stub/mock in tests."""
        return str(getattr(parent_ctx, "op_id", "op-unknown"))

    def _infer_parent_provider(self, parent_ctx: "OperationContext") -> str:
        """Infer parent's provider name for Adversarial Provider Inheritance.

        Step-1 uses a string field on the parent context if present.
        Step-2 will consult the real CandidateGenerator state.
        """
        provider = getattr(parent_ctx, "provider_name", "") or ""
        return str(provider) if provider else "unknown"

    def _infer_parent_cost_remaining(self, parent_ctx: "OperationContext") -> float:
        """Infer parent's remaining cost budget.

        Step-1 uses a best-effort accessor; Step-4 consults CostGovernor directly.
        """
        remaining = getattr(parent_ctx, "cost_remaining_usd", None)
        if remaining is None:
            return float("inf")
        try:
            return float(remaining)
        except (TypeError, ValueError):
            return float("inf")

    def _clamp_deadline(
        self,
        parent_ctx: "OperationContext",
        sub_timeout_s: float,
    ) -> datetime:
        """Clamp the subagent's deadline to min(parent_deadline, now+sub_timeout)."""
        sub_deadline = self._now() + timedelta(seconds=sub_timeout_s)
        parent_deadline = getattr(parent_ctx, "pipeline_deadline", None)
        if parent_deadline is None:
            return sub_deadline
        try:
            return min(sub_deadline, parent_deadline)
        except TypeError:
            # Mixed naive/aware datetimes — fall back to sub_deadline.
            return sub_deadline

    # ------------------------------------------------------------------
    # Single-dispatch path
    # ------------------------------------------------------------------

    async def _dispatch_single(
        self,
        parent_ctx: "OperationContext",
        request: SubagentRequest,
        scope: str,
    ) -> SubagentResult:
        ctx = self._build_sub_context(parent_ctx, request, scope=scope)
        self._comm.emit_spawn(
            ctx.parent_op_id, ctx.subagent_id, ctx.subagent_type, request.goal
        )
        result = await self._run_one_safely(ctx)
        self._comm.emit_result(ctx.parent_op_id, ctx.subagent_id, result)
        self._ledger.append(ctx.parent_op_id, ctx.subagent_id, result)
        return result.truncated_for_prompt()

    # ------------------------------------------------------------------
    # Parallel-dispatch path
    # ------------------------------------------------------------------

    async def _dispatch_parallel(
        self,
        parent_ctx: "OperationContext",
        request: SubagentRequest,
        scopes: Tuple[str, ...],
    ) -> SubagentResult:
        """Fan out up to MAX_PARALLEL_SCOPES subagents concurrently.

        Uses asyncio.TaskGroup on Python 3.11+ for structured concurrency
        with proper cancellation semantics; falls back to asyncio.gather
        with `return_exceptions=True` on 3.9/3.10.
        """
        contexts = [
            self._build_sub_context(parent_ctx, request, scope=s) for s in scopes
        ]
        for ctx in contexts:
            self._comm.emit_spawn(
                ctx.parent_op_id, ctx.subagent_id, ctx.subagent_type, request.goal
            )

        results: List[SubagentResult] = await self._gather_subagents(contexts)

        for ctx, res in zip(contexts, results):
            self._comm.emit_result(ctx.parent_op_id, ctx.subagent_id, res)
            self._ledger.append(ctx.parent_op_id, ctx.subagent_id, res)

        merged = self._merge_results(request, contexts, results)
        return merged.truncated_for_prompt()

    async def _gather_subagents(
        self,
        contexts: List[SubagentContext],
    ) -> List[SubagentResult]:
        """Run N subagents concurrently with structured cancellation semantics."""
        if sys.version_info >= (3, 11) and hasattr(asyncio, "TaskGroup"):
            return await self._gather_with_taskgroup(contexts)
        return await self._gather_with_gather(contexts)

    async def _gather_with_taskgroup(
        self,
        contexts: List[SubagentContext],
    ) -> List[SubagentResult]:
        """Python 3.11+ structured-concurrency path."""
        tasks: List[asyncio.Task] = []
        async with asyncio.TaskGroup() as tg:  # type: ignore[attr-defined]
            for ctx in contexts:
                tasks.append(tg.create_task(self._run_one_safely(ctx)))
        return [t.result() for t in tasks]

    async def _gather_with_gather(
        self,
        contexts: List[SubagentContext],
    ) -> List[SubagentResult]:
        """Python 3.9/3.10 fallback using gather(return_exceptions=True)."""
        outcomes = await asyncio.gather(
            *(self._run_one_safely(ctx) for ctx in contexts),
            return_exceptions=True,
        )
        results: List[SubagentResult] = []
        for ctx, outcome in zip(contexts, outcomes):
            if isinstance(outcome, BaseException):
                results.append(self._failure_result(ctx, outcome))
            else:
                results.append(outcome)
        return results

    # ------------------------------------------------------------------
    # Single subagent execution with unconditional failure envelope
    # ------------------------------------------------------------------

    async def _run_one_safely(self, ctx: SubagentContext) -> SubagentResult:
        """Invoke the explore executor, converting every failure to a structured result.

        This is the only place where exceptions are turned into
        SubagentResult objects — above this method, the orchestrator
        only ever handles well-formed results. Below this method, the
        executor can raise freely.
        """
        started_ns = time.time_ns()
        try:
            executor = self._explore_factory()
            # Step-1 stub: the default ExploreExecutor raises NotImplementedError.
            # We catch that here and return a NOT_IMPLEMENTED status so callers
            # get a well-formed result while Step 2 is in development.
            result = await executor.explore(ctx)
            # Always stamp the final timing fields in case the executor didn't.
            if not result.started_at_ns or not result.finished_at_ns:
                result = self._stamp_timing(result, started_ns)
            return result
        except NotImplementedError as e:
            logger.info(
                "[Subagent] Step-1 scaffolding hit on %s (expected until Step 2): %s",
                ctx.subagent_id, e,
            )
            return self._not_implemented_result(ctx, started_ns)
        except SubagentError as e:
            return self._failure_result(ctx, e, started_ns=started_ns)
        except asyncio.CancelledError:
            # Re-raise cancellation so TaskGroup / gather propagate it.
            raise
        except Exception as e:  # noqa: BLE001 — defense in depth
            logger.exception(
                "[Subagent] Unexpected error in %s", ctx.subagent_id
            )
            return self._failure_result(ctx, e, started_ns=started_ns)

    def _stamp_timing(
        self,
        result: SubagentResult,
        started_ns: int,
    ) -> SubagentResult:
        now_ns = time.time_ns()
        return SubagentResult(
            schema_version=result.schema_version,
            subagent_id=result.subagent_id,
            subagent_type=result.subagent_type,
            status=result.status,
            goal=result.goal,
            started_at_ns=result.started_at_ns or started_ns,
            finished_at_ns=result.finished_at_ns or now_ns,
            findings=result.findings,
            files_read=result.files_read,
            search_queries=result.search_queries,
            summary=result.summary,
            cost_usd=result.cost_usd,
            tool_calls=result.tool_calls,
            tool_diversity=result.tool_diversity,
            provider_used=result.provider_used,
            fallback_triggered=result.fallback_triggered,
            error_class=result.error_class,
            error_detail=result.error_detail,
        )

    def _not_implemented_result(
        self,
        ctx: SubagentContext,
        started_ns: int,
    ) -> SubagentResult:
        """Return a well-formed NOT_IMPLEMENTED result for Step-1 scaffolding."""
        return SubagentResult(
            schema_version=SCHEMA_VERSION,
            subagent_id=ctx.subagent_id,
            subagent_type=ctx.subagent_type,
            status=SubagentStatus.NOT_IMPLEMENTED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            findings=(),
            files_read=(),
            search_queries=(),
            summary=(
                "Subagent orchestrator is wired; AgenticExploreSubagent "
                "executor is not yet implemented. This is a Step-1 "
                "scaffolding placeholder. Phase-1 Step-2 will replace it."
            ),
            cost_usd=0.0,
            tool_calls=0,
            tool_diversity=0,
            provider_used=ctx.primary_provider_name,
            fallback_triggered=False,
            error_class="NotImplementedYet",
            error_detail=(
                "AgenticExploreSubagent.explore not yet implemented. "
                "Orchestrator scaffolding is functional; executor is pending."
            ),
        )

    def _failure_result(
        self,
        ctx: SubagentContext,
        exc: BaseException,
        started_ns: Optional[int] = None,
    ) -> SubagentResult:
        """Convert an arbitrary exception into a structured FAILED result."""
        return SubagentResult(
            schema_version=SCHEMA_VERSION,
            subagent_id=ctx.subagent_id,
            subagent_type=ctx.subagent_type,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns or time.time_ns(),
            finished_at_ns=time.time_ns(),
            provider_used=ctx.primary_provider_name,
            error_class=exc.__class__.__name__,
            error_detail=str(exc)[:500],
        )

    # ------------------------------------------------------------------
    # Parallel result merging
    # ------------------------------------------------------------------

    def _merge_results(
        self,
        request: SubagentRequest,
        contexts: List[SubagentContext],
        results: List[SubagentResult],
    ) -> SubagentResult:
        """Merge N parallel SubagentResults into a single aggregated result.

        The merged result inherits the first context's identifiers but
        aggregates findings, files_read, search_queries, cost, and tool
        counts across all children. The merged status is:
          * COMPLETED  — if every child completed.
          * PARTIAL    — if any child completed and any child failed.
          * FAILED     — if every child failed.
          * CANCELLED  — if any child was cancelled (takes precedence
                         over PARTIAL only when no COMPLETED results).
        """
        if not results:
            raise SubagentError("_merge_results called with empty results list")

        all_findings: List[SubagentFinding] = []
        all_files: List[str] = []
        all_queries: List[str] = []
        total_cost = 0.0
        total_tool_calls = 0
        any_fallback = False
        started_ns = min(r.started_at_ns or 0 for r in results) or time.time_ns()
        finished_ns = max(r.finished_at_ns or 0 for r in results) or time.time_ns()
        statuses = {r.status for r in results}

        for r in results:
            all_findings.extend(r.findings)
            all_files.extend(r.files_read)
            all_queries.extend(r.search_queries)
            total_cost += r.cost_usd
            total_tool_calls += r.tool_calls
            any_fallback = any_fallback or r.fallback_triggered

        if SubagentStatus.COMPLETED in statuses and statuses == {SubagentStatus.COMPLETED}:
            merged_status = SubagentStatus.COMPLETED
        elif statuses == {SubagentStatus.FAILED}:
            merged_status = SubagentStatus.FAILED
        elif SubagentStatus.CANCELLED in statuses and SubagentStatus.COMPLETED not in statuses:
            merged_status = SubagentStatus.CANCELLED
        elif SubagentStatus.NOT_IMPLEMENTED in statuses and len(statuses) == 1:
            merged_status = SubagentStatus.NOT_IMPLEMENTED
        else:
            merged_status = SubagentStatus.PARTIAL

        # Deduplicate files_read / search_queries while preserving order.
        seen_files: set[str] = set()
        dedup_files: List[str] = []
        for f in all_files:
            if f not in seen_files:
                seen_files.add(f)
                dedup_files.append(f)
        seen_queries: set[str] = set()
        dedup_queries: List[str] = []
        for q in all_queries:
            if q not in seen_queries:
                seen_queries.add(q)
                dedup_queries.append(q)

        # Merged tool_diversity is the union of children's diversity.
        # Step-1 scaffolding doesn't have real diversity data yet; Step 2
        # will populate each child's `tool_diversity`. We conservatively
        # report max() across children in the meantime.
        merged_diversity = max((r.tool_diversity for r in results), default=0)

        summary = self._build_merged_summary(request, results, merged_status)

        primary_id = contexts[0].subagent_id if contexts else ""

        return SubagentResult(
            schema_version=SCHEMA_VERSION,
            subagent_id=primary_id,
            subagent_type=request.subagent_type,
            status=merged_status,
            goal=request.goal,
            started_at_ns=started_ns,
            finished_at_ns=finished_ns,
            findings=tuple(all_findings),
            files_read=tuple(dedup_files),
            search_queries=tuple(dedup_queries),
            summary=summary,
            cost_usd=round(total_cost, 6),
            tool_calls=total_tool_calls,
            tool_diversity=merged_diversity,
            provider_used=contexts[0].primary_provider_name if contexts else "",
            fallback_triggered=any_fallback,
            error_class="" if merged_status == SubagentStatus.COMPLETED else "MergedResult",
            error_detail="",
        )

    def _build_merged_summary(
        self,
        request: SubagentRequest,
        results: List[SubagentResult],
        merged_status: SubagentStatus,
    ) -> str:
        """Produce a compact deterministic summary across parallel subagents."""
        completed = sum(1 for r in results if r.status == SubagentStatus.COMPLETED)
        failed = sum(1 for r in results if r.status == SubagentStatus.FAILED)
        not_impl = sum(1 for r in results if r.status == SubagentStatus.NOT_IMPLEMENTED)
        total_findings = sum(len(r.findings) for r in results)
        total_files = sum(len(r.files_read) for r in results)
        parts = [
            f"Merged {len(results)} parallel subagents for: {request.goal}",
            f"status={merged_status.value}",
            f"completed={completed} failed={failed} not_implemented={not_impl}",
            f"findings={total_findings} files_read={total_files}",
        ]
        return " · ".join(parts)
