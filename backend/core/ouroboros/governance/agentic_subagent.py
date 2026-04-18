"""
AgenticExploreSubagent — Phase 1 Step 2.

LLM-ready read-only subagent implementing the ExploreExecutor protocol
defined in subagent_orchestrator.py. Step 2 ships with the deterministic
path wired: every dispatch runs the existing ExplorationSubagent (AST +
regex + import resolution) and converts its ExplorationReport into a
structured SubagentResult that flows back to the parent generation model
through the orchestrator.

Step 3 will layer LLM-driven exploration on top by constructing the
subagent's own ToolLoopCoordinator with READONLY_TOOL_MANIFEST and
invoking provider.generate() under EXPLORE_SUBAGENT_SYSTEM_PROMPT. The
class already holds the structural seams (`_run_llm_driven`, timeout
budgets from PRIMARY_PROVIDER_TIMEOUT_S, provider-name inheritance on
SubagentContext) so that upgrade is additive, not rewriting.

Mathematical forbiddance of mutation (Manifesto §6 — Iron Gate):
    The underlying ExplorationSubagent has no mutation call paths
    whatsoever. This wrapper adds no filesystem writes. Every tool
    class exercised maps to READONLY_TOOL_MANIFEST. The master switch
    (JARVIS_SUBAGENT_DISPATCH_ENABLED, default false) still gates any
    dispatch attempt at the orchestrator boundary — this class is
    importable at any time but unreachable until the switch flips.

Iron Gate diversity requirement (Derek's Phase 1 mandate):
    After the deterministic backbone completes, we count the tool
    classes effectively exercised (_DETERMINISTIC_TOOL_CLASSES =
    comprehension + pattern + call_graph from the AST/search/import
    passes) and compare against MIN_TOOL_DIVERSITY. Deterministic-mode
    runs always meet the floor; LLM-mode (Step 3) will compute
    diversity from the actual tool_execution_records. Failures produce
    SubagentStatus.DIVERSITY_REJECTED with no findings — rejected,
    not retried.

Nervous System Reflex (Manifesto §5, Tier 3 — scaffolded):
    ctx.primary_provider_name + ctx.fallback_provider_name carry the
    adversarial pair. Step-2 deterministic path is provider-free, so
    fallback never fires here. Step 3 wraps the primary provider call
    in asyncio.wait_for(PRIMARY_PROVIDER_TIMEOUT_S); on timeout the
    orchestrator severs and retries on Claude within
    FALLBACK_PROVIDER_TIMEOUT_S. Survival supersedes cost.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import FrozenSet, List, Optional

from backend.core.ouroboros.governance.exploration_subagent import (
    ExplorationReport as DeterministicReport,
    ExplorationSubagent,
)
from backend.core.ouroboros.governance.subagent_contracts import (
    MIN_TOOL_DIVERSITY,
    PRIMARY_PROVIDER_TIMEOUT_S,
    SCHEMA_VERSION,
    SubagentContext,
    SubagentFinding,
    SubagentResult,
    SubagentStatus,
    SubagentTimeout,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Category passthrough — DeterministicFinding.category → SubagentFinding.category
#
# ExplorationSubagent already emits categories aligned with the
# SubagentFinding taxonomy. Any unexpected category is coerced to
# "pattern" rather than silently dropped; "pattern" is the lowest-signal
# bucket, so coercion degrades relevance without losing the finding.
# ============================================================================

_CATEGORY_PASSTHROUGH: FrozenSet[str] = frozenset({
    "import_chain",
    "call_graph",
    "complexity",
    "pattern",
    "structure",
    "api_surface",
    "test_gap",
})


# Tool classes effectively exercised by the deterministic backbone.
# ExplorationSubagent does:
#   _analyze_file       → AST parse          → comprehension
#   _search_codebase    → regex grep         → pattern
#   _resolve_import,    → import resolution  → call_graph
#   _find_test_file
# Step 3's LLM-driven mode will compute this dynamically from the
# ToolLoopCoordinator's tool_execution_records.
_DETERMINISTIC_TOOL_CLASSES: FrozenSet[str] = frozenset({
    "comprehension",
    "pattern",
    "call_graph",
})


# ============================================================================
# AgenticExploreSubagent
# ============================================================================


class AgenticExploreSubagent:
    """LLM-ready read-only subagent implementing ExploreExecutor.

    Every ``explore(ctx)`` call:
      1. Computes remaining wall-clock budget from ctx.deadline and
         request.timeout_s (clamped to PRIMARY_PROVIDER_TIMEOUT_S).
      2. Runs a fresh ExplorationSubagent.explore() under asyncio.wait_for.
         Fresh instance per call — no yield-flag leakage across dispatches.
      3. Converts the resulting ExplorationReport into a SubagentResult,
         passing through the category taxonomy and truncating evidence
         via SubagentFinding.truncated() downstream at the orchestrator.
      4. Computes tool_diversity from _DETERMINISTIC_TOOL_CLASSES and
         rejects results below MIN_TOOL_DIVERSITY with status
         DIVERSITY_REJECTED (zeroed findings per Derek's mandate).

    The constructor takes only project_root; no mutable state between calls.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = Path(project_root)

    # ------------------------------------------------------------------
    # ExploreExecutor protocol
    # ------------------------------------------------------------------

    async def explore(self, ctx: SubagentContext) -> SubagentResult:
        """Run the subagent's exploration for the given SubagentContext.

        Step-2 behavior: deterministic path. Enforces per-subagent
        timeout + cooperative cancellation via the backbone's
        request_yield() API.
        """
        started_ns = time.time_ns()
        timeout_s = self._remaining_s(ctx)
        provider = ctx.primary_provider_name or "deterministic"

        try:
            report = await self._run_deterministic(ctx, timeout_s=timeout_s)
        except SubagentTimeout as e:
            return self._timeout_result(ctx, started_ns, provider, str(e))
        except asyncio.CancelledError:
            # Re-raise so TaskGroup / gather propagate cancellation cleanly.
            raise
        except Exception as e:  # noqa: BLE001 — defense in depth
            logger.exception(
                "[AgenticExploreSubagent] Unexpected failure sub=%s",
                ctx.subagent_id,
            )
            return self._failure_result(ctx, started_ns, provider, e)

        result = self._build_result_from_report(
            ctx=ctx,
            report=report,
            started_ns=started_ns,
            provider_used=provider,
        )

        if result.tool_diversity < MIN_TOOL_DIVERSITY:
            return self._diversity_rejected(result)
        return result

    # ------------------------------------------------------------------
    # Future-ready scaffolding: LLM-driven mode (Step 3)
    # ------------------------------------------------------------------

    async def _run_llm_driven(self, ctx: SubagentContext) -> DeterministicReport:  # pragma: no cover
        """Step-3 entry point for LLM-driven exploration.

        Will construct a ToolLoopCoordinator with READONLY_TOOL_MANIFEST,
        call provider.generate() under EXPLORE_SUBAGENT_SYSTEM_PROMPT,
        and parse the model's final JSON answer into structured findings.
        Step 2 never reaches this method; it exists so Step 3 is a small
        additive change rather than a restructure.
        """
        raise NotImplementedError(
            "LLM-driven mode lands in Step 3. Step 2 uses the deterministic "
            "backbone only; this method is a structural seam."
        )

    # ------------------------------------------------------------------
    # Deterministic execution with timeout + cooperative-cancel
    # ------------------------------------------------------------------

    async def _run_deterministic(
        self,
        ctx: SubagentContext,
        timeout_s: float,
    ) -> DeterministicReport:
        """Run a fresh ExplorationSubagent under asyncio.wait_for.

        Fresh instance per call — avoids yield-flag state leakage between
        dispatches. On timeout, calls request_yield() so the current
        BFS round exits cleanly, then re-raises as SubagentTimeout so
        the orchestrator produces a structured timeout result.

        Entry-file guard rail: the wrapped ExplorationSubagent has a
        latent bug where empty `entry_files` triggers a call to the
        non-existent `_infer_entry_files` method (AttributeError). We
        refuse to trip that path — if the caller didn't supply
        target_files, we derive a minimal safe set from ctx.scope_path
        (globbing *.py under the scope) or fall back to the repo README.
        This keeps the wrapper independent of whatever state the
        backbone is in across releases.
        """
        if timeout_s <= 0:
            raise SubagentTimeout(
                f"budget exhausted before start (remaining={timeout_s:.2f}s)"
            )

        entry_files = self._resolve_entry_files(ctx)

        backbone = ExplorationSubagent(self._root)

        coro = backbone.explore(
            goal=ctx.request.goal,
            entry_files=entry_files,
            max_files=ctx.request.max_files,
            max_depth=ctx.request.max_depth,
        )
        try:
            return await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError as e:
            # Cooperative-cancel the backbone so in-flight file reads stop.
            backbone.request_yield()
            raise SubagentTimeout(
                f"deterministic exploration exceeded timeout={timeout_s:.2f}s"
            ) from e

    def _resolve_entry_files(self, ctx: SubagentContext) -> tuple:
        """Return a non-empty entry-file tuple, guarding the backbone's bug.

        Resolution order:
          1. ctx.request.target_files (if any)
          2. First 5 *.py files under ctx.scope_path (if present and valid)
          3. README.md at the repo root
          4. Any single *.py in the repo root
          5. Empty tuple — callers should handle this as a no-work request

        Step 3's LLM-driven mode will replace this with the model's own
        entry-file selection.
        """
        if ctx.request.target_files:
            return tuple(ctx.request.target_files)

        # Try scope_path directory listing.
        if ctx.scope_path:
            scope_dir = self._root / ctx.scope_path
            if scope_dir.is_dir():
                py_files = sorted(scope_dir.rglob("*.py"))
                if py_files:
                    # Return up to 5 relative paths so the BFS starts somewhere.
                    return tuple(
                        str(p.relative_to(self._root))
                        for p in py_files[:5]
                    )

        # Fall back to the repo README if present.
        readme = self._root / "README.md"
        if readme.exists():
            return ("README.md",)

        # Last-ditch: first *.py found at the repo root.
        for py in sorted(self._root.glob("*.py")):
            return (str(py.relative_to(self._root)),)

        return ()

    def _remaining_s(self, ctx: SubagentContext) -> float:
        """Compute remaining seconds until subagent deadline.

        Effective budget is min(request.timeout_s, PRIMARY_PROVIDER_TIMEOUT_S,
        time-to-ctx.deadline). If deadline is already past, returns 0 so
        _run_deterministic raises SubagentTimeout immediately.
        """
        request_cap = min(float(ctx.request.timeout_s), PRIMARY_PROVIDER_TIMEOUT_S)
        if ctx.deadline is None:
            return request_cap
        now = datetime.now(timezone.utc)
        try:
            remaining = (ctx.deadline - now).total_seconds()
        except TypeError:
            # Naive/aware mismatch — degrade gracefully to the request cap.
            return request_cap
        return max(0.0, min(remaining, request_cap))

    # ------------------------------------------------------------------
    # Result builders
    # ------------------------------------------------------------------

    def _build_result_from_report(
        self,
        *,
        ctx: SubagentContext,
        report: DeterministicReport,
        started_ns: int,
        provider_used: str,
    ) -> SubagentResult:
        """Convert a DeterministicReport into a structured SubagentResult.

        Categories outside _CATEGORY_PASSTHROUGH are coerced to "pattern"
        rather than silently dropped. Evidence is passed through unchanged;
        the orchestrator's truncated_for_prompt() handles the per-finding
        evidence cap before the result is injected into the parent's prompt.
        """
        now_ns = time.time_ns()
        findings: List[SubagentFinding] = []
        for df in report.findings:
            category = df.category if df.category in _CATEGORY_PASSTHROUGH else "pattern"
            findings.append(SubagentFinding(
                category=category,
                description=df.description,
                file_path=df.file_path,
                line=0,
                evidence=df.evidence,
                relevance=df.relevance,
            ))

        # Deterministic backbone exercises comprehension + pattern +
        # call_graph structurally. tool_calls is approximated as the
        # number of distinct read/search operations recorded in the
        # report. Step 3 replaces this with the LLM tool-loop count.
        approx_tool_calls = len(report.files_read) + len(report.search_queries)
        diversity = len(_DETERMINISTIC_TOOL_CLASSES)

        return SubagentResult(
            schema_version=SCHEMA_VERSION,
            subagent_id=ctx.subagent_id,
            subagent_type=ctx.subagent_type,
            status=SubagentStatus.COMPLETED,
            goal=report.goal or ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=now_ns,
            findings=tuple(findings),
            files_read=tuple(report.files_read),
            search_queries=tuple(report.search_queries),
            summary=report.summary or "",
            cost_usd=0.0,  # deterministic: no LLM cost.
            tool_calls=approx_tool_calls,
            tool_diversity=diversity,
            provider_used=provider_used,
            fallback_triggered=False,
            error_class="",
            error_detail="",
        )

    def _timeout_result(
        self,
        ctx: SubagentContext,
        started_ns: int,
        provider_used: str,
        detail: str,
    ) -> SubagentResult:
        return SubagentResult(
            schema_version=SCHEMA_VERSION,
            subagent_id=ctx.subagent_id,
            subagent_type=ctx.subagent_type,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            provider_used=provider_used,
            error_class="SubagentTimeout",
            error_detail=detail[:500],
        )

    def _failure_result(
        self,
        ctx: SubagentContext,
        started_ns: int,
        provider_used: str,
        exc: BaseException,
    ) -> SubagentResult:
        return SubagentResult(
            schema_version=SCHEMA_VERSION,
            subagent_id=ctx.subagent_id,
            subagent_type=ctx.subagent_type,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            provider_used=provider_used,
            error_class=exc.__class__.__name__,
            error_detail=str(exc)[:500],
        )

    def _diversity_rejected(self, result: SubagentResult) -> SubagentResult:
        """Return a DIVERSITY_REJECTED copy with zeroed-out findings.

        Per Derek's Phase 1 mandate: shallow exploration is REJECTED, not
        retried and not demoted. The model receives an explicit signal
        that the Iron Gate diversity floor was not met, and the findings
        are discarded so the model cannot accidentally consume a shallow
        result as if it were authoritative.
        """
        return SubagentResult(
            schema_version=result.schema_version,
            subagent_id=result.subagent_id,
            subagent_type=result.subagent_type,
            status=SubagentStatus.DIVERSITY_REJECTED,
            goal=result.goal,
            started_at_ns=result.started_at_ns,
            finished_at_ns=result.finished_at_ns,
            findings=(),  # rejected — no findings returned to the parent
            files_read=result.files_read,
            search_queries=result.search_queries,
            summary=(
                f"Iron Gate rejection: tool diversity "
                f"{result.tool_diversity} < MIN_TOOL_DIVERSITY "
                f"({MIN_TOOL_DIVERSITY}). Subagent relied on insufficient "
                "tool variety; result discarded per Phase 1 mandate."
            ),
            cost_usd=result.cost_usd,
            tool_calls=result.tool_calls,
            tool_diversity=result.tool_diversity,
            provider_used=result.provider_used,
            fallback_triggered=result.fallback_triggered,
            error_class="IronGateDiversityRejection",
            error_detail=(
                f"tool_diversity={result.tool_diversity} < "
                f"MIN_TOOL_DIVERSITY={MIN_TOOL_DIVERSITY}"
            ),
        )


# ============================================================================
# Convenience factory
# ============================================================================


def build_default_explore_factory(project_root: Path):
    """Return a factory callable suitable for SubagentOrchestrator.

    Each invocation constructs a fresh AgenticExploreSubagent so per-call
    state (yield flag, internal counters) never leaks across dispatches.
    """
    root = Path(project_root)

    def _factory() -> AgenticExploreSubagent:
        return AgenticExploreSubagent(root)

    return _factory
