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


class ReviewExecutor:
    """Structural protocol for Phase B REVIEW subagents.

    Concrete implementation: ``AgenticReviewSubagent`` in
    ``agentic_review_subagent.py``. The orchestrator only needs
    ``.review(ctx)`` to return an awaitable SubagentResult whose
    ``type_payload`` carries the verdict tuple (verdict,
    semantic_integrity_score, mutation_score, reservations,
    reject_reasons, rationale).

    Manifesto §6 (Execution Validation): REVIEW's verdict must be
    structurally derived from AST signals + optional mutation testing,
    not LLM-prose intuition. Concrete implementations MUST wire
    SemanticGuardian's pattern set as the primary integrity signal
    and MAY add mutation_score for allowlisted critical paths.

    REVIEW is orchestrator-driven: dispatched from post-VALIDATE
    unconditionally (not via the model's tool loop). The SubagentContext
    carries the candidate being reviewed in
    ``ctx.request.review_target_candidate``.
    """

    async def review(self, ctx: SubagentContext) -> SubagentResult:  # pragma: no cover — interface stub
        raise NotImplementedError(
            "ReviewExecutor.review requires a concrete implementation. "
            "Use AgenticReviewSubagent from agentic_review_subagent.py."
        )


class PlanExecutor:
    """Structural protocol for Phase B PLAN subagents.

    Concrete implementation: ``AgenticPlanSubagent`` in
    ``agentic_plan_subagent.py``. The orchestrator only needs
    ``.plan(ctx)`` to return an awaitable SubagentResult whose
    ``type_payload`` carries the typed DAG tuple (dag_units, dag_edges,
    barriers, validation_result).

    Manifesto §2 (Directed Acyclic Graph): PLAN cannot output flat
    task lists. It must emit a strict, mathematically verifiable DAG
    of implementation units. Concrete implementations MUST self-
    validate via ``dag_validator.validate_plan_dag(units)`` and refuse
    to return an invalid DAG.

    PLAN is orchestrator-driven (same as REVIEW): dispatched pre-
    GENERATE from the orchestrator's PLAN phase for any op with ≥ 2
    target files. Single-file ops skip PLAN entirely — no DAG to
    build. The SubagentContext carries the op being planned in
    ``ctx.request.plan_target``.
    """

    async def plan(self, ctx: SubagentContext) -> SubagentResult:  # pragma: no cover — interface stub
        raise NotImplementedError(
            "PlanExecutor.plan requires a concrete implementation. "
            "Use AgenticPlanSubagent from agentic_plan_subagent.py."
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
        review_factory: Optional[Callable[[], "ReviewExecutor"]] = None,
        plan_factory: Optional[Callable[[], "PlanExecutor"]] = None,
    ) -> None:
        self._explore_factory = explore_factory
        # Phase B: review_factory + plan_factory are optional at
        # construction so existing call sites that only wire explore
        # keep working. Dispatches for those types without the matching
        # factory return NOT_IMPLEMENTED with a clear error_detail —
        # no silent fallbacks.
        self._review_factory = review_factory
        self._plan_factory = plan_factory
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

    async def dispatch_plan(
        self,
        parent_ctx: "OperationContext",
        *,
        op_description: str,
        target_files: Tuple[str, ...],
        primary_repo: str = "jarvis",
        risk_tier: str = "",
        timeout_s: float = 60.0,
    ) -> SubagentResult:
        """Orchestrator-driven PLAN dispatch (Manifesto §2 DAG).

        Constructs a SubagentRequest programmatically and dispatches
        through the same machinery used for model-driven EXPLORE calls.
        PLAN is orchestrator-driven — the Venom ``dispatch_subagent``
        tool does NOT expose plan; the orchestrator calls this method
        unconditionally pre-GENERATE for ops with >= 2 target files
        (single-file ops skip PLAN — there is no DAG to build).

        The returned SubagentResult's ``type_payload`` carries the
        typed DAG tuple. The caller MUST treat ``valid=False`` in
        the embedded validation_result as a hard failure — do not
        consume an invalid DAG downstream (§2 is non-negotiable).

        Parameters
        ----------
        op_description:
            The goal text from the parent op. Passed unchanged to the
            planner; the planner's prompt + DAG construction rules
            own interpretation.
        target_files:
            The target files scoped by the parent op. The planner
            partitions these into DAG units.
        primary_repo:
            Repo the op belongs to. Most ops are "jarvis"; passed
            through for cross-repo DAGs (which delegate to
            _SCHEMA_VERSION_EXECUTION_GRAPH's multi-repo shape).
        risk_tier:
            Parent op's risk tier. Planner may widen acceptance_tests
            on higher tiers.
        timeout_s:
            Plan budget. Short by default — DAG construction on <20
            files is fast; longer if the concrete planner is LLM-driven.
        """
        request = SubagentRequest(
            subagent_type=SubagentType.PLAN,
            goal=f"Plan DAG for: {op_description[:200]}",
            target_files=target_files,
            scope_paths=(),
            max_files=max(len(target_files), 1),
            max_depth=1,
            timeout_s=timeout_s,
            parallel_scopes=1,
            plan_target={
                "op_description": op_description,
                "target_files": tuple(target_files),
                "primary_repo": primary_repo,
                "risk_tier": risk_tier,
            },
        )
        return await self.dispatch(parent_ctx, request)

    async def dispatch_review(
        self,
        parent_ctx: "OperationContext",
        *,
        file_path: str,
        pre_apply_content: str,
        candidate_content: str,
        generation_intent: str,
        timeout_s: float = 60.0,
    ) -> SubagentResult:
        """Orchestrator-driven REVIEW dispatch (Manifesto §6).

        Constructs a SubagentRequest programmatically and dispatches it
        through the same machinery used for model-driven EXPLORE calls.
        Review is orchestrator-driven (unconditional, post-VALIDATE) —
        the Venom ``dispatch_subagent`` tool does NOT expose review; the
        model cannot opt out of review by not asking for it.

        Parameters
        ----------
        file_path:
            Repo-relative path of the candidate file.
        pre_apply_content:
            Original file content as on disk before the candidate would
            apply. Required for AST diff signals.
        candidate_content:
            The candidate's proposed new content — what APPLY would write.
        generation_intent:
            Short summary of what the parent op was trying to achieve.
            Used by AgenticReviewSubagent to judge semantic alignment.
        timeout_s:
            Review budget. Short by default because AST analysis is fast;
            longer if a mutation-testing hook is triggered.
        """
        request = SubagentRequest(
            subagent_type=SubagentType.REVIEW,
            goal=f"Review candidate for {file_path}: {generation_intent[:200]}",
            target_files=(file_path,),
            scope_paths=(),
            max_files=1,
            max_depth=1,
            timeout_s=timeout_s,
            parallel_scopes=1,
            review_target_candidate={
                "file_path": file_path,
                "pre_apply_content": pre_apply_content,
                "candidate_content": candidate_content,
                "generation_intent": generation_intent,
            },
        )
        return await self.dispatch(parent_ctx, request)

    # ------------------------------------------------------------------
    # Master-switch guard (Manifesto §6 — structural refusal before work)
    # ------------------------------------------------------------------

    def _ensure_dispatch_enabled(self) -> None:
        if not subagent_dispatch_enabled():
            raise SubagentDispatchDisabled(
                "Subagent dispatch is disabled via "
                "JARVIS_SUBAGENT_DISPATCH_ENABLED=false. Default is true as "
                "of the 2026-04-18 Phase 1 graduation — only explicit "
                "operator override reaches this error."
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
            # Route by subagent_type. EXPLORE is Phase 1 (graduated).
            # REVIEW + PLAN are Phase B (orchestrator-driven dispatch).
            # Future types add additional branches here; do not collapse
            # back to a single factory.
            if ctx.subagent_type == SubagentType.REVIEW:
                if self._review_factory is None:
                    logger.warning(
                        "[Subagent] REVIEW dispatch attempted without a "
                        "review_factory wired — returning NOT_IMPLEMENTED. "
                        "sub=%s",
                        ctx.subagent_id,
                    )
                    return self._not_implemented_result(ctx, started_ns)
                review_executor = self._review_factory()
                result = await review_executor.review(ctx)
            elif ctx.subagent_type == SubagentType.PLAN:
                if self._plan_factory is None:
                    logger.warning(
                        "[Subagent] PLAN dispatch attempted without a "
                        "plan_factory wired — returning NOT_IMPLEMENTED. "
                        "sub=%s",
                        ctx.subagent_id,
                    )
                    return self._not_implemented_result(ctx, started_ns)
                plan_executor = self._plan_factory()
                result = await plan_executor.plan(ctx)
            else:
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
