"""
AgenticGeneralSubagent — Phase B Semantic Firewall executor (Manifesto §5).

Implements the GeneralExecutor protocol defined in
subagent_orchestrator.py. Unlike EXPLORE (read-only cartography),
REVIEW (verdict on candidate), or PLAN (DAG output), GENERAL is
open-ended — the invocation's ``goal`` field is an arbitrary task.
This makes GENERAL the most vulnerable subagent type to prompt
injection and context drift.

Manifesto §5 (Semantic Firewall) — architectural constraint:
    GENERAL is heavily sandboxed. Because it lacks a specific domain
    constraint, it is the most vulnerable to prompt injection or
    context drift. Strict boundary conditions gate its invocation.

    The firewall enforcement happens in TWO layers:

      Layer 1 — ``SubagentOrchestrator.dispatch_general()``:
          * Recursion ban (parent cannot already be inside GENERAL).
          * Tier -1 input sanitization via
            ``semantic_firewall.sanitize_for_firewall()``.
          * Boundary-condition validation via
            ``semantic_firewall.validate_boundary_conditions()``.

      Layer 2 — THIS EXECUTOR at run time:
          * Re-verify invocation fields survived sanitization.
          * Stamp ``_within_general_subagent=True`` on any context it
            exposes to sub-tool-loops, so if that code-path later
            dispatches GENERAL again, layer 1's recursion ban trips.
          * Wrap output in a ``<general_subagent_output untrusted="true">``
            fence so downstream consumers preserve the trust boundary.
          * Elevated observability: every tool-loop round logs at INFO
            with ``via=general_subagent``.

Phase B scope — what this first cut ships:
    * The full infrastructure: contracts, orchestrator routing, firewall
      layer-1 enforcement, output quarantine.
    * A STUB execution body that honors the firewall constraints but
      returns a structured "NOT_IMPLEMENTED_NEEDS_LLM_WIRING" placeholder.

    The full LLM-driven execution body (running a restricted Venom tool
    loop under a GENERAL-specific system prompt) is a Step-2 concern —
    matches the pattern of Phase 1's AgenticExploreSubagent which shipped
    the deterministic backbone first, LLM mode second. The upgrade is
    additive: replace the stub body with a ToolLoopCoordinator driven by
    ``ctx.request.general_invocation["allowed_tools"]`` and
    ``invocation_reason``. Firewall constraints stay unchanged.

Output quarantine:
    The subagent's eventual LLM output will be wrapped in a typed
    fence so downstream consumers (orchestrator, ConversationBridge,
    memory) treat it as untrusted. The fence carries:
      * ``<general_subagent_output untrusted="true" sub_id="...">``
      * Sanitized content (Tier -1 pass on the way out)
      * ``</general_subagent_output>``
    Even in the Phase B stub, the result payload includes the fence
    shape so regression tests can pin the contract.

Cost: $0.00 per invocation in Phase B stub mode. Phase B+1 (LLM wiring)
will charge the parent's cost budget per Venom tool loop conventions.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.semantic_firewall import (
    is_within_general_subagent,
    readonly_tool_whitelist,
    sanitize_for_firewall,
    validate_boundary_conditions,
)
from backend.core.ouroboros.governance.subagent_contracts import (
    SubagentContext,
    SubagentFinding,
    SubagentResult,
    SubagentStatus,
    SubagentType,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Output quarantine fence — typed constants so downstream regex matches stay
# aligned across modules.
# ============================================================================


QUARANTINE_FENCE_OPEN_FMT = '<general_subagent_output untrusted="true" sub_id={sub_id!r}>'
QUARANTINE_FENCE_CLOSE = "</general_subagent_output>"


def quarantine_wrap(sub_id: str, sanitized_content: str) -> str:
    """Wrap a GENERAL subagent's output in the untrusted-trust fence.

    Pure function — called from the executor AND from regression tests
    that pin the contract. The fence shape is documented in the module
    docstring and MUST stay parse-stable; downstream consumers match
    on the literal open/close strings.
    """
    return (
        QUARANTINE_FENCE_OPEN_FMT.format(sub_id=sub_id)
        + "\n"
        + sanitized_content
        + "\n"
        + QUARANTINE_FENCE_CLOSE
    )


# ============================================================================
# AgenticGeneralSubagent
# ============================================================================


class AgenticGeneralSubagent:
    """GeneralExecutor implementation with Semantic Firewall layer-2 checks.

    Phase B ships with a stub execution body that honors every firewall
    constraint but returns a ``NOT_IMPLEMENTED_NEEDS_LLM_WIRING`` status.
    The full LLM-driven tool-loop body is Step-2 — enabled via a
    dependency-injected ``llm_driver`` callable once that track matures.

        # Default (Phase B) — stub body, firewall enforcement only.
        general = AgenticGeneralSubagent(project_root=Path("/repo"))

        # Future (Step 2) — wire an LLM driver.
        general = AgenticGeneralSubagent(
            project_root=Path("/repo"),
            llm_driver=my_tool_loop_driver,
        )
    """

    def __init__(
        self,
        project_root: Path,
        *,
        llm_driver: Optional[
            Callable[[Dict[str, Any]], Any]
        ] = None,
        llm_budget_s: float = 90.0,
    ) -> None:
        self._root = Path(project_root)
        self._llm_driver = llm_driver
        self._llm_budget_s = float(llm_budget_s)

    # ------------------------------------------------------------------
    # GeneralExecutor protocol
    # ------------------------------------------------------------------

    async def general(self, ctx: SubagentContext) -> SubagentResult:
        """Run a GENERAL subagent on ctx.request.general_invocation.

        Returns a well-formed SubagentResult. Never raises except for
        asyncio.CancelledError.

        Phase B behavior:
          * Re-validate the firewall constraints (defense in depth —
            layer 1 already ran at dispatch, but if a future caller
            bypasses dispatch_general() and constructs a
            SubagentRequest directly, layer 2 catches it here).
          * Stamp _within_general_subagent=True on the context shadow
            so any sub-dispatch via this ctx trips the recursion ban.
          * Return a structured NOT_IMPLEMENTED_NEEDS_LLM_WIRING
            status in the stub body OR delegate to llm_driver when
            wired.
          * Wrap output in the quarantine fence unconditionally.
        """
        started_ns = time.time_ns()

        invocation = getattr(ctx.request, "general_invocation", None)
        if not invocation:
            return self._malformed_input_result(
                ctx, started_ns,
                detail="general_invocation missing from request — "
                "callers must use SubagentOrchestrator.dispatch_general() "
                "which populates this field from validated boundary conditions",
            )

        # ---- Layer-2 re-validation (defense in depth) -------------------
        # Even if a caller bypassed dispatch_general() and constructed
        # SubagentRequest directly, we re-run the firewall here. This is
        # explicit Manifesto §5 / §1 Boundary Principle: "trust is
        # asserted at the boundary, not assumed by module".
        valid, boundary_reasons = validate_boundary_conditions(invocation)
        goal_scan = sanitize_for_firewall(
            invocation.get("goal", ""), field_name="goal",
        )
        reason_scan = sanitize_for_firewall(
            invocation.get("invocation_reason", ""),
            max_chars=200,
            field_name="invocation_reason",
        )
        firewall_reasons: List[str] = []
        if not valid:
            firewall_reasons.extend(boundary_reasons)
        if goal_scan.rejected:
            firewall_reasons.extend(goal_scan.reasons)
        if reason_scan.rejected:
            firewall_reasons.extend(reason_scan.reasons)
        if firewall_reasons:
            logger.warning(
                "[AgenticGeneralSubagent] layer-2 firewall rejection "
                "(caller bypassed dispatch_general?) sub=%s reasons=%d",
                ctx.subagent_id, len(firewall_reasons),
            )
            return self._firewall_layer2_rejection_result(
                ctx, started_ns, reasons=tuple(firewall_reasons),
            )

        # Recursion check — layer 2 re-verifies in case the parent ctx
        # was mutated between dispatch and execution.
        parent_ctx = ctx.parent_ctx
        if is_within_general_subagent(parent_ctx):
            return self._firewall_layer2_rejection_result(
                ctx, started_ns,
                reasons=(
                    "parent already within a GENERAL subagent "
                    "(layer-2 recursion detect)",
                ),
            )

        # Stamp the "within GENERAL" marker on the parent ctx so any
        # sub-dispatch downstream trips the recursion ban. Frozen
        # OperationContext → use object.__setattr__ for the non-hash-
        # chained marker, matching the pattern used for task_complexity
        # elsewhere in the code base.
        try:
            object.__setattr__(parent_ctx, "_within_general_subagent", True)
        except Exception:
            # Some parent_ctx shapes (MagicMocks in tests) set attributes
            # freely; others (frozen dataclasses) accept __setattr__ only
            # via object.__setattr__. Either way, we've done our best-
            # effort — the orchestrator's dispatch_general() layer-1
            # check caught it already.
            logger.debug(
                "[AgenticGeneralSubagent] could not stamp recursion "
                "marker on parent ctx — layer-1 check remains the gate",
            )

        # ---- Execution body ---------------------------------------------
        try:
            exec_trace = await self._execute_body(ctx, invocation)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "[AgenticGeneralSubagent] unexpected failure sub=%s",
                ctx.subagent_id,
            )
            return self._internal_failure_result(
                ctx, started_ns, error=e,
            )

        # ---- Output quarantine ------------------------------------------
        # Even the stub's placeholder output goes through the fence.
        sanitized_output_scan = sanitize_for_firewall(
            exec_trace.get("raw_output", ""),
            max_chars=16384,
            field_name="output",
        )
        fenced_output = quarantine_wrap(
            sub_id=ctx.subagent_id,
            sanitized_content=sanitized_output_scan.sanitized,
        )

        # Findings — keep the observability pipeline unchanged; one
        # finding carrying the sanitized output snippet + metadata.
        findings = (
            SubagentFinding(
                category="pattern",
                description=(
                    f"general_invocation goal={invocation.get('goal', '')[:80]!r} "
                    f"tools={list(invocation.get('allowed_tools', ()))}"
                ),
                file_path=(
                    list(invocation.get("operation_scope", ()))[0]
                    if invocation.get("operation_scope") else ""
                ),
                line=0,
                evidence=fenced_output[:300],
                relevance=0.7,
            ),
        )

        payload: Tuple[Tuple[str, Any], ...] = (
            ("status", exec_trace.get("status", "stub")),
            ("fenced_output", fenced_output),
            ("tool_calls_made", int(exec_trace.get("tool_calls_made", 0))),
            ("allowed_tools", tuple(invocation.get("allowed_tools", ()))),
            ("operation_scope", tuple(invocation.get("operation_scope", ()))),
            ("max_mutations", int(invocation.get("max_mutations", 0))),
            ("firewall_layer2_passed", True),
        )

        status = (
            SubagentStatus.COMPLETED
            if exec_trace.get("status") == "completed"
            else SubagentStatus.NOT_IMPLEMENTED
        )
        finished_ns = time.time_ns()
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.GENERAL,
            status=status,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=finished_ns,
            findings=findings,
            files_read=tuple(invocation.get("operation_scope", ())),
            search_queries=(),
            summary=f"GENERAL status={exec_trace.get('status')} "
                    f"tool_calls={exec_trace.get('tool_calls_made', 0)}",
            cost_usd=float(exec_trace.get("cost_usd", 0.0)),
            tool_calls=int(exec_trace.get("tool_calls_made", 0)),
            tool_diversity=int(exec_trace.get("tool_diversity", 0)),
            provider_used=exec_trace.get("provider_used", "firewall_stub"),
            fallback_triggered=bool(exec_trace.get("fallback_triggered", False)),
            type_payload=payload,
        )

    # ------------------------------------------------------------------
    # Execution body
    # ------------------------------------------------------------------

    async def _execute_body(
        self, ctx: SubagentContext, invocation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run the subagent's actual work.

        Phase B: stub — returns a placeholder exec_trace. The firewall
        has already gated the invocation; the stub's job is to emit a
        well-shaped "not yet wired" signal so the observability pipeline
        produces a normal-shaped record.

        Future (Step 2): invoke an LLM-driven Venom tool loop restricted
        to ``invocation['allowed_tools']`` with the GENERAL-specific
        system prompt. The tool loop's output becomes the exec_trace's
        ``raw_output`` and ``tool_calls_made`` populates from the loop
        records.
        """
        # Elevated observability — every GENERAL execution gets an INFO
        # line regardless of outcome. Stays DEBUG-level noisy on
        # non-GENERAL subagent types; GENERAL earns the extra audit
        # visibility by §5 discipline.
        logger.info(
            "[AgenticGeneralSubagent] exec sub=%s goal=%r scope=%s tools=%s "
            "max_mutations=%d parent_risk=%s",
            ctx.subagent_id,
            invocation.get("goal", "")[:80],
            list(invocation.get("operation_scope", ())),
            list(invocation.get("allowed_tools", ())),
            int(invocation.get("max_mutations", 0)),
            invocation.get("parent_op_risk_tier", ""),
        )
        if self._llm_driver is None:
            # Stub mode — phase B default. Return a structured placeholder
            # so the output pipeline + regression tests see a well-shaped
            # result shape. The verdict-equivalent for GENERAL is the
            # exec trace itself; missing llm_driver surfaces as
            # status=stub so callers can decide whether to retry later.
            return {
                "status": "stub",
                "raw_output": (
                    "GENERAL subagent infrastructure reached the "
                    "execution body. LLM driver is not wired in this "
                    "Phase B infrastructure cut — Step 2 will replace "
                    "this stub with a restricted Venom tool loop."
                ),
                "tool_calls_made": 0,
                "tool_diversity": 0,
                "cost_usd": 0.0,
                "provider_used": "firewall_stub",
                "fallback_triggered": False,
            }
        # LLM-driven mode (Step 2). Wrap in hard-kill pattern — GENERAL
        # must never hang the orchestrator.
        #
        # Since the driver signature is future-defined and we're in the
        # infrastructure cut, the wiring here is deliberately minimal:
        # call llm_driver with the sanitized invocation, enforce the
        # hard-kill budget, return whatever shape the driver produces
        # (which the executor normalizes through the exec_trace dict).
        runner = self._llm_driver

        async def _await_driver() -> Dict[str, Any]:
            # Enrich the payload with provider + budget hints so the
            # driver can resolve a provider and bound its tool loop
            # without reaching back into ctx after the call site returns.
            # Added 2026-04-20 for the Phase C Slice 1a LLM-driver wire-in.
            result = runner({
                "sub_id": ctx.subagent_id,
                "invocation": dict(invocation),
                "project_root": str(self._root),
                "primary_provider_name": str(
                    getattr(ctx, "primary_provider_name", "") or ""
                ),
                "fallback_provider_name": str(
                    getattr(ctx, "fallback_provider_name", "") or ""
                ),
                # Convert ctx.deadline (datetime | None) to a monotonic
                # float for the ToolLoopCoordinator. None signals
                # "driver picks its own budget".
                "deadline": None,  # driver uses self._llm_budget_s-based default
                "max_rounds": None,  # let driver read env
                "tool_timeout_s": None,  # let driver read env
            })
            if asyncio.iscoroutine(result):
                return await result
            return result  # already a dict

        task = asyncio.create_task(_await_driver())
        try:
            done, pending = await asyncio.wait(
                {task}, timeout=self._llm_budget_s + 30.0,
            )
            if pending:
                for t in pending:
                    t.cancel()
                logger.error(
                    "[AgenticGeneralSubagent] HARD-KILL llm_driver after "
                    "%.1fs sub=%s — driver wedged",
                    self._llm_budget_s + 30.0, ctx.subagent_id,
                )
                return {
                    "status": "hard_kill",
                    "raw_output": "",
                    "tool_calls_made": 0,
                    "tool_diversity": 0,
                    "cost_usd": 0.0,
                    "provider_used": "hard_kill",
                    "fallback_triggered": False,
                }
            result = await task
            if not isinstance(result, dict):
                return {
                    "status": "malformed_driver_output",
                    "raw_output": str(result)[:2000],
                    "tool_calls_made": 0,
                    "tool_diversity": 0,
                    "cost_usd": 0.0,
                    "provider_used": "malformed",
                    "fallback_triggered": False,
                }
            # Normalize expected keys.
            result.setdefault("status", "completed")
            result.setdefault("raw_output", "")
            result.setdefault("tool_calls_made", 0)
            result.setdefault("tool_diversity", 0)
            result.setdefault("cost_usd", 0.0)
            result.setdefault("provider_used", "llm_driver")
            result.setdefault("fallback_triggered", False)
            return result
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Failure result helpers
    # ------------------------------------------------------------------

    def _malformed_input_result(
        self, ctx: SubagentContext, started_ns: int, *, detail: str,
    ) -> SubagentResult:
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.GENERAL,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            error_class="MalformedGeneralInput",
            error_detail=detail,
            provider_used="firewall_stub",
        )

    def _firewall_layer2_rejection_result(
        self,
        ctx: SubagentContext,
        started_ns: int,
        *,
        reasons: Tuple[str, ...],
    ) -> SubagentResult:
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.GENERAL,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            error_class="SubagentSemanticFirewallRejection",
            error_detail="; ".join(reasons)[:500],
            provider_used="firewall_stub",
            type_payload=(("rejection_reasons", reasons),),
        )

    def _internal_failure_result(
        self, ctx: SubagentContext, started_ns: int, *, error: Exception,
    ) -> SubagentResult:
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.GENERAL,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            error_class=type(error).__name__,
            error_detail=str(error)[:500],
            provider_used="firewall_stub",
        )


def build_default_general_factory(
    project_root: Path,
) -> Callable[[], AgenticGeneralSubagent]:
    """Factory helper matching the pattern of other build_default_*_factory.

    The default factory wires AgenticGeneralSubagent with NO llm_driver
    — Phase B infrastructure cut is firewall-only. Use
    :func:`build_llm_general_factory` when the LLM-driver flag is on
    and a provider registry is available.
    """
    def _factory() -> AgenticGeneralSubagent:
        return AgenticGeneralSubagent(project_root=project_root)
    return _factory


def build_llm_general_factory(
    project_root: Path,
    provider_registry: Callable[[str], Any],
    *,
    policy: Optional[Any] = None,
    llm_budget_s: float = 90.0,
) -> Callable[[], AgenticGeneralSubagent]:
    """Phase C Slice 1a factory — wires an LLM driver behind the flag.

    When ``JARVIS_GENERAL_LLM_DRIVER_ENABLED=true``, the returned
    factory constructs an ``AgenticGeneralSubagent`` with
    ``llm_driver`` set to a closure over
    :func:`general_driver.run_general_tool_loop`. When the flag is
    off, falls back to :func:`build_default_general_factory` (stub
    path) so operators can attach the factory unconditionally at boot
    and opt in at runtime via the env var.

    Parameters
    ----------
    project_root:
        Closed over so the driver can construct an
        ``AsyncProcessToolBackend`` scoped to the repo.
    provider_registry:
        Callable ``name -> provider`` mapping. The driver calls this
        with ``payload["primary_provider_name"]`` at run time.
        Production uses the governed loop's provider registry;
        tests pass a stub.
    policy:
        Optional global ``GoverningToolPolicy``. When ``None`` the
        driver builds a default policy scoped to ``project_root``.
    llm_budget_s:
        Forwarded to ``AgenticGeneralSubagent.__init__`` for its
        hard-kill wrapper (``asyncio.wait`` timeout around the
        llm_driver coroutine).

    Returns
    -------
    A zero-arg factory that produces a fresh
    ``AgenticGeneralSubagent`` per dispatch. The returned subagent
    has ``llm_driver`` set iff the flag is ``true`` at construction
    time; runtime flips mid-session are explicitly NOT supported (the
    flag is read at factory-invocation time to keep dispatch
    behavior deterministic within a session).
    """
    from backend.core.ouroboros.governance.general_driver import (
        driver_enabled,
        run_general_tool_loop,
    )

    if not driver_enabled():
        # Fallback to stub factory — opt-out semantics. This keeps
        # every call site byte-identical to the default factory when
        # the flag is off, so build_llm_general_factory is a safe
        # drop-in replacement for build_default_general_factory.
        return build_default_general_factory(project_root)

    async def _driver(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Closed-over driver — resolves provider via registry each call."""
        return await run_general_tool_loop(
            payload,
            project_root=project_root,
            provider_registry=provider_registry,
            policy=policy,
        )

    def _factory() -> AgenticGeneralSubagent:
        return AgenticGeneralSubagent(
            project_root=project_root,
            llm_driver=_driver,
            llm_budget_s=llm_budget_s,
        )
    return _factory
