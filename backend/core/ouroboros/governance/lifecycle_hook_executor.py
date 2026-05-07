"""Lifecycle Hook Registry — Slice 3 async executor.

Single orchestrator-callable async function that fires all
registered hooks for one event in parallel, bounded by per-hook
timeout via ``asyncio.wait_for``, fail-isolated via
``asyncio.gather(return_exceptions=True)``, and aggregates the
per-hook results into one :class:`AggregateHookDecision` via
Slice 1's :func:`compute_hook_decision` (BLOCK-wins).

Architectural reuse — three existing surfaces compose with ZERO
duplication:

  * Slice 1 :func:`compute_hook_decision` — total
    aggregation function. NEVER raises. Closed-taxonomy outcome
    via BLOCK-wins.
  * Slice 1 :func:`make_hook_result` — convenience constructor
    that auto-stamps Phase C tightening per outcome. Used by
    every defensive synthesizer in this module.
  * Slice 2 :class:`LifecycleHookRegistry` — the registration
    substrate. Executor calls ``registry.for_event(event)`` to
    get a priority-ordered tuple; never mutates the registry.

The only NEW code in Slice 3:

  * The :func:`fire_hooks` async coordinator that wires the above
    pieces together with per-hook timeout + fail-isolated parallel
    execution.
  * Per-hook defensive wrapper :func:`_run_one_hook` that wraps
    a sync hook in ``asyncio.to_thread`` + ``asyncio.wait_for``
    and converts any exception class into a typed
    :class:`HookResult` with outcome=FAILED.

Backward-compat by construction
-------------------------------

  * Master flag default-FALSE through Slices 1-4 → ``fire_hooks``
    short-circuits to CONTINUE before any registry lookup.
  * No hooks registered for an event → CONTINUE (empty).
  * Per-hook timeout → that hook gets FAILED; siblings unaffected.
  * Per-hook raise → that hook gets FAILED; siblings unaffected.
  * Per-hook returns non-HookResult → FAILED + log.
  * Per-hook is_enabled() returns False → DISABLED (visible in
    audit, distinct from FAILED).

Direct-solve principles
-----------------------

* **Asynchronous-ready** — sync hook callables wrapped via
  ``asyncio.to_thread`` per Slice 2's documented contract.
  Parallel execution via ``asyncio.gather(return_exceptions=True)``
  — one slow/raising hook never cancels siblings.
* **Dynamic** — per-hook timeout flows from
  :class:`HookRegistration.timeout_s` (registered with the
  hook); env default flows from Slice 1's
  :func:`default_hook_timeout_s` clamp.
* **Adaptive** — every degraded path (timeout / raise /
  non-HookResult / disabled / empty registry) maps to a
  closed-taxonomy outcome rather than raising.
* **Intelligent** — fail-isolation at the gather boundary
  preserves observability of which hooks failed: the aggregator
  records ``failed_hooks`` by name so operators see WHO failed.
* **Robust** — :func:`fire_hooks` NEVER raises out. asyncio
  cancellation propagates per asyncio convention (caller catches).
* **No hardcoding** — sentinel constants (FAILED detail formats)
  exposed as module-level symbols.

Authority invariants (AST-pinned by Slice 5):

* MAY import: ``lifecycle_hook`` (Slice 1 primitive),
  ``lifecycle_hook_registry`` (Slice 2 substrate).
* MUST NOT import: orchestrator / phase_runner / iron_gate /
  change_engine / candidate_generator / providers /
  doubleword_provider / urgency_router / auto_action_router /
  subagent_scheduler / tool_executor / semantic_guardian /
  semantic_firewall / risk_engine.
* No exec/eval/compile.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
import weakref
from typing import Any, Optional, Tuple

from backend.core.ouroboros.governance.lifecycle_hook import (
    AggregateHookDecision,
    HookContext,
    HookEventTypes,
    HookOutcome,
    HookResult,
    LifecycleEvent,
    ToolHookEvent,
    compute_hook_decision,
    hook_async_ffn_enabled,
    lifecycle_hooks_enabled,
    make_hook_result,
    venom_tool_hooks_enabled,
)
from backend.core.ouroboros.governance.lifecycle_hook_registry import (
    HookRegistration,
    LifecycleHookRegistry,
    get_default_registry,
)

logger = logging.getLogger(__name__)


LIFECYCLE_HOOK_EXECUTOR_SCHEMA_VERSION: str = (
    "lifecycle_hook_executor.1"
)


# ---------------------------------------------------------------------------
# Sentinel detail formats — Slice 5 will AST-pin
# ---------------------------------------------------------------------------

#: Per-hook timeout detail format. Used by the wrapper when a
#: hook exceeds its registered timeout_s. Operators grep for this
#: format in audit logs.
_TIMEOUT_DETAIL_PREFIX: str = "hook_timeout_after_"

#: Per-hook raise detail format. Carries exception type + bounded
#: message so operators can diagnose without leaking stack traces.
_RAISE_DETAIL_PREFIX: str = "hook_raised_"

#: Per-hook bad-return detail. The hook returned something that
#: isn't a HookResult — operator misconfig.
_BAD_RETURN_DETAIL_PREFIX: str = "hook_bad_return_"


# ---------------------------------------------------------------------------
# Per-hook defensive wrapper
# ---------------------------------------------------------------------------


async def _run_one_hook(
    registration: HookRegistration,
    context: HookContext,
) -> HookResult:
    """Run one registered hook with timeout + defensive wrapping.
    NEVER raises out (asyncio.CancelledError propagates per
    convention; everything else converted to FAILED).

    Wraps the sync callable in ``asyncio.to_thread`` so the event
    loop is never blocked. ``asyncio.wait_for`` enforces the
    per-hook timeout — overrun → FAILED with timeout detail.
    Any exception from the hook → FAILED with raise detail.
    Any non-HookResult return → FAILED with bad-return detail.
    """
    started_mono = time.monotonic()
    timeout_s = registration.timeout_s
    try:
        # Per-hook is_enabled gate — checked here (not in fire_hooks)
        # so a disabled hook still produces a HookResult for the
        # aggregator to record (observability: operators see
        # DISABLED in audit, distinct from "hook didn't run").
        if not registration.is_enabled():
            return make_hook_result(
                registration.name,
                HookOutcome.DISABLED,
                detail="enabled_check returned False",
                elapsed_ms=(time.monotonic() - started_mono) * 1000.0,
            )

        # Run via to_thread + wait_for so:
        # 1. sync hook doesn't block event loop
        # 2. timeout enforced at the asyncio boundary
        # 3. asyncio.CancelledError propagates cleanly
        #
        # Venom V3 (2026-05-07) — async hook support: if the
        # callable is a coroutine function, await it directly
        # (no to_thread wrapping; the coroutine is already
        # event-loop-native). Sync callables continue through
        # to_thread. Both paths are bounded by the same
        # per-hook timeout via wait_for.
        if inspect.iscoroutinefunction(registration.callable):
            result = await asyncio.wait_for(
                registration.callable(context),
                timeout=timeout_s,
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    registration.callable, context,
                ),
                timeout=timeout_s,
            )

        # Validate hook return — operators may misconfigure and
        # return None / dict / wrong type.
        if not isinstance(result, HookResult):
            elapsed_ms = (time.monotonic() - started_mono) * 1000.0
            return make_hook_result(
                registration.name,
                HookOutcome.FAILED,
                detail=(
                    f"{_BAD_RETURN_DETAIL_PREFIX}"
                    f"{type(result).__name__}"
                ),
                elapsed_ms=elapsed_ms,
            )

        # Defensive: ensure hook_name on the result matches the
        # registration name (operators may forget to wire it).
        # If mismatch, rebuild with the canonical name so audit
        # reflects WHO ran.
        if result.hook_name != registration.name:
            return make_hook_result(
                registration.name,
                result.outcome,
                detail=result.detail,
                elapsed_ms=(time.monotonic() - started_mono) * 1000.0,
            )

        # Stamp elapsed_ms if hook didn't (most won't — make_hook_result
        # accepts it but returns 0 by default).
        if result.elapsed_ms <= 0.0:
            return make_hook_result(
                registration.name,
                result.outcome,
                detail=result.detail,
                elapsed_ms=(time.monotonic() - started_mono) * 1000.0,
            )

        return result

    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - started_mono) * 1000.0
        logger.info(
            "[LifecycleHookExecutor] hook %s exceeded timeout "
            "%.1fs", registration.name, timeout_s,
        )
        return make_hook_result(
            registration.name,
            HookOutcome.FAILED,
            detail=f"{_TIMEOUT_DETAIL_PREFIX}{timeout_s:.1f}s",
            elapsed_ms=elapsed_ms,
        )
    except asyncio.CancelledError:
        # Caller-initiated cancellation — propagate per asyncio
        # convention. fire_hooks's gather will collect this.
        raise
    except Exception as exc:  # noqa: BLE001 — defensive contract
        elapsed_ms = (time.monotonic() - started_mono) * 1000.0
        # Bound the exception detail so a hostile hook can't blow
        # up the audit log.
        exc_name = type(exc).__name__
        exc_msg = str(exc)[:256]
        logger.info(
            "[LifecycleHookExecutor] hook %s raised %s: %s",
            registration.name, exc_name, exc_msg,
        )
        return make_hook_result(
            registration.name,
            HookOutcome.FAILED,
            detail=f"{_RAISE_DETAIL_PREFIX}{exc_name}:{exc_msg}",
            elapsed_ms=elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Public async coordinator — orchestrator-callable surface
# ---------------------------------------------------------------------------


async def fire_hooks(
    event: "LifecycleEvent | ToolHookEvent",
    context: HookContext,
    *,
    registry: Optional[LifecycleHookRegistry] = None,
    enabled: Optional[bool] = None,
) -> AggregateHookDecision:
    """Fire all registered hooks for one event. Returns the
    aggregated decision. NEVER raises out (asyncio.CancelledError
    propagates per convention).

    Decision flow:
      1. Master flag check — if disabled (Slice 1
         :func:`lifecycle_hooks_enabled` returns False AND no
         explicit ``enabled`` override), short-circuit to
         CONTINUE without registry lookup or task spawn.
      2. Look up :meth:`LifecycleHookRegistry.for_event` —
         priority-ordered tuple.
      3. If empty (no hooks registered for this event), return
         CONTINUE aggregate.
      4. Spawn one task per registration via :func:`_run_one_hook`
         (each wraps a sync callable in ``asyncio.to_thread`` +
         ``asyncio.wait_for``).
      5. Gather with ``return_exceptions=True`` so one
         CancelledError doesn't cancel siblings.
      6. Aggregate via Slice 1's :func:`compute_hook_decision`
         (BLOCK-wins).

    Args:
      event: The lifecycle event being fired.
      context: The frozen :class:`HookContext` shared across
        all hooks for this event.
      registry: Optional explicit registry (test injection).
        Defaults to :func:`get_default_registry`.
      enabled: Optional explicit enable override (test injection).
        Defaults to env via Slice 1's
        :func:`lifecycle_hooks_enabled`.

    Returns:
      :class:`AggregateHookDecision` — caller branches on
      ``aggregate`` (BLOCK / WARN / CONTINUE) and inspects
      ``blocking_hooks`` / ``warning_hooks`` / ``failed_hooks``
      for audit detail.
    """
    # 1. Validate event at boundary.
    # Venom V1 Slice 2 (2026-05-06) — accept either taxonomy.
    if not isinstance(event, HookEventTypes):
        logger.warning(
            "[LifecycleHookExecutor] non-event input type=%s — "
            "returning CONTINUE",
            type(event).__name__,
        )
        return AggregateHookDecision(
            event=LifecycleEvent.PRE_APPLY,
            aggregate=HookOutcome.CONTINUE,
            total_hooks=0,
            monotonic_tightening_verdict="",
        )

    # 2. Master flag short-circuit. The two surfaces have
    # SEPARATE master flags so operators can adopt phase hooks
    # without enabling tool hooks (or vice versa). Caller's
    # explicit ``enabled`` override always wins.
    if enabled is not None:
        is_enabled = bool(enabled)
    elif isinstance(event, ToolHookEvent):
        is_enabled = venom_tool_hooks_enabled()
    else:
        is_enabled = lifecycle_hooks_enabled()
    if not is_enabled:
        return AggregateHookDecision(
            event=event,
            aggregate=HookOutcome.CONTINUE,
            total_hooks=0,
            monotonic_tightening_verdict="",
        )

    # 3. Resolve registry (singleton by default).
    try:
        active_registry = registry or get_default_registry()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[LifecycleHookExecutor] registry resolution degraded: "
            "%s — returning CONTINUE", exc,
        )
        return AggregateHookDecision(
            event=event,
            aggregate=HookOutcome.CONTINUE,
            total_hooks=0,
            monotonic_tightening_verdict="",
        )

    # 4. Look up registrations for this event (priority-ordered).
    # Venom V4 (2026-05-07) — for ToolHookEvent, filter by
    # tool_name pattern at registry level so non-matched
    # callbacks are NOT spawned as tasks (preserves async
    # robustness + cuts useless wait_for work). Phase-boundary
    # events ignore the filter (their HookContext doesn't carry
    # a tool_name; the for_event_filtered method passes
    # tool_name=None which short-circuits to legacy for_event).
    if isinstance(event, ToolHookEvent):
        try:
            _tool_name = (
                str(context.payload.get("tool_name", ""))
                if context is not None and context.payload
                else ""
            )
        except Exception:  # noqa: BLE001 — defensive
            _tool_name = ""
        registrations: Tuple[HookRegistration, ...] = (
            active_registry.for_event_filtered(
                event, _tool_name,
            )
        )
    else:
        registrations = active_registry.for_event(event)
    if not registrations:
        return AggregateHookDecision(
            event=event,
            aggregate=HookOutcome.CONTINUE,
            total_hooks=0,
            monotonic_tightening_verdict="",
        )

    # 5. Validate context at boundary; coerce to a sentinel if
    #    garbage so hooks always receive a HookContext (their
    #    Protocol contract).
    if not isinstance(context, HookContext):
        logger.warning(
            "[LifecycleHookExecutor] non-context input type=%s "
            "— substituting empty HookContext",
            type(context).__name__,
        )
        context = HookContext(event=event)

    # 6. Venom V3 (PRD §32.6 line 380, 2026-05-07) — partition
    #    blocking vs fire-and-forget (FFN) registrations BEFORE
    #    spawning the blocking gather.
    #
    #    Discipline (operator binding 2026-05-07):
    #      * is_async=True hooks scheduled AFTER aggregation
    #        (via asyncio.create_task on the running loop);
    #        their HookResult does NOT contribute to BLOCK-wins.
    #      * is_async=False (default) hooks awaited inside
    #        asyncio.gather and contribute to aggregation.
    #      * Master flag JARVIS_HOOK_ASYNC_ENABLED gates the
    #        partition: when OFF, ALL hooks treated as blocking
    #        (byte-identical pre-V3 behavior; AST-pinned).
    #      * FFN tasks named via name= kwarg + tracked in weak
    #        registry (operator-visible; drain helper available
    #        via :func:`drain_ffn_tasks`).
    if hook_async_ffn_enabled():
        blocking_regs = tuple(
            r for r in registrations
            if not getattr(r, "is_async", False)
        )
        ffn_regs = tuple(
            r for r in registrations
            if getattr(r, "is_async", False)
        )
    else:
        blocking_regs = registrations
        ffn_regs = ()
    # Spawn blocking tasks. Each bounded by per-hook timeout +
    # defensive wrapping inside _run_one_hook.
    tasks = [
        asyncio.create_task(
            _run_one_hook(reg, context),
            name=f"lifecycle-hook-{reg.name}",
        )
        for reg in blocking_regs
    ]
    try:
        gathered = await asyncio.gather(
            *tasks, return_exceptions=True,
        )
    except asyncio.CancelledError:
        # Caller-initiated cancellation. Cancel any stragglers
        # before propagating. FFN tasks (if any) have not been
        # spawned yet — V3 schedules them AFTER aggregation.
        for t in tasks:
            if not t.done():
                t.cancel()
        raise

    # 7. Convert any exception in gather results to FAILED. This
    #    handles the rare case where _run_one_hook itself raises
    #    (should never happen — its contract is NEVER-raise except
    #    CancelledError) AND the case where CancelledError leaked
    #    from one task's underlying to_thread.
    results: list = []
    for reg, gathered_item in zip(blocking_regs, gathered):
        if isinstance(gathered_item, HookResult):
            results.append(gathered_item)
        elif isinstance(gathered_item, asyncio.CancelledError):
            # Per-task cancellation (not caller-initiated).
            # Treat as FAILED with sentinel detail.
            results.append(make_hook_result(
                reg.name,
                HookOutcome.FAILED,
                detail="hook_cancelled",
            ))
        elif isinstance(gathered_item, BaseException):
            # _run_one_hook contract violation — should never
            # happen. Defensive: convert to FAILED.
            exc_name = type(gathered_item).__name__
            exc_msg = str(gathered_item)[:256]
            logger.warning(
                "[LifecycleHookExecutor] _run_one_hook contract "
                "violation for %s: %s — FAILED",
                reg.name, exc_name,
            )
            results.append(make_hook_result(
                reg.name,
                HookOutcome.FAILED,
                detail=(
                    f"runner_contract_violation:{exc_name}:{exc_msg}"
                ),
            ))
        else:
            # Non-HookResult, non-exception. Should not happen.
            results.append(make_hook_result(
                reg.name,
                HookOutcome.FAILED,
                detail=(
                    f"runner_returned_{type(gathered_item).__name__}"
                ),
            ))

    # 8. Aggregate via Slice 1 (BLOCK-wins). Aggregation runs
    #    over BLOCKING results only — FFN registrations are
    #    structurally excluded from the BLOCK-wins decision.
    decision = compute_hook_decision(event, tuple(results))

    # 9. Venom V3 (2026-05-07) — schedule FFN tasks AFTER
    #    aggregation. They run in parallel with whatever the
    #    caller does next; the dispatcher does NOT await them.
    #    Each task is named (operator-visible) + tracked in the
    #    weak registry (queryable via :func:`drain_ffn_tasks`).
    #    Operator binding: "specify ... task names + weak
    #    registry + optional ... drain on graceful shutdown."
    if ffn_regs:
        try:
            _schedule_ffn_tasks(ffn_regs, context, event)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[LifecycleHookExecutor] FFN scheduling raised: "
                "%s — aggregation already complete, decision "
                "unaffected", exc,
            )
    return decision


# ---------------------------------------------------------------------------
# Venom V3 — FFN task registry + drain helper
# ---------------------------------------------------------------------------


# Weak registry of in-flight FFN tasks. Tasks self-remove on
# completion via the asyncio runtime; the WeakSet ensures
# orphaned tasks (e.g., loop closing) drop out automatically.
# Operator binding 2026-05-07: "task names + weak registry +
# optional asyncio.TaskGroup drain on graceful shutdown hook"
# — this is the chosen pattern, AST-pinned via the FFN
# scheduling AST invariant below.
_FFN_TASK_REGISTRY: "weakref.WeakSet[asyncio.Task[Any]]" = (
    weakref.WeakSet()
)


def _schedule_ffn_tasks(
    ffn_regs: Tuple[HookRegistration, ...],
    context: HookContext,
    event: "LifecycleEvent | ToolHookEvent",
) -> None:
    """Schedule fire-and-forget hook tasks AFTER aggregation.
    NEVER raises (caller invokes inside try/except). Task names
    follow the pattern ``venom_v3_ffn_<hook_name>_<event>`` so
    operators can grep ``asyncio.all_tasks()`` to audit which
    FFN tasks are in-flight."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — should not happen since we're
        # inside an awaited fire_hooks call. Defensive
        # short-circuit; FFN scheduling is best-effort.
        return
    event_value = (
        event.value if hasattr(event, "value") else str(event)
    )
    for reg in ffn_regs:
        try:
            task = loop.create_task(
                _run_one_hook(reg, context),
                name=f"venom_v3_ffn_{reg.name}_{event_value}",
            )
            _FFN_TASK_REGISTRY.add(task)
            task.add_done_callback(_log_ffn_task_done)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[LifecycleHookExecutor] FFN spawn raised "
                "for %s: %s", reg.name, exc,
            )


def _log_ffn_task_done(task: "asyncio.Task[Any]") -> None:
    """Done-callback attached to every FFN task. Logs FAILED
    outcomes so exceptions in FFN hooks remain auditable
    (operator binding: 'exception in FFN logged, pipeline
    continues'). Cancelled tasks are silent (graceful
    shutdown). NEVER raises."""
    try:
        if task.cancelled():
            return
        result = task.result()
        if isinstance(result, HookResult):
            if result.outcome == HookOutcome.FAILED:
                logger.warning(
                    "[VenomV3FFN] task %s returned FAILED: %s",
                    task.get_name(),
                    getattr(result, "detail", "")[:200],
                )
        # Non-HookResult / unexpected return — log
        else:
            logger.warning(
                "[VenomV3FFN] task %s returned non-HookResult "
                "type=%s",
                task.get_name(), type(result).__name__,
            )
    except asyncio.CancelledError:
        # Task was cancelled — silent.
        pass
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[VenomV3FFN] task %s raised: %s",
            task.get_name(), exc,
        )


async def drain_ffn_tasks(
    *, timeout: float = 5.0,
) -> int:
    """Best-effort drain of in-flight FFN tasks. Returns count
    of tasks that completed within ``timeout`` seconds. NEVER
    raises.

    Operators wanting strict drain semantics (e.g., "no FFN
    work outlives the orchestrator") invoke this from their
    graceful shutdown hook. Default-off — pre-V3 callers don't
    need it. Bounded by ``timeout`` so a slow FFN hook can't
    block shutdown indefinitely.

    Operator binding 2026-05-07: "graceful shutdown drains or
    asserts bounded task count" — this helper provides the
    drain primitive; operators choose whether to invoke it.
    """
    try:
        pending = [
            t for t in list(_FFN_TASK_REGISTRY)
            if not t.done()
        ]
    except Exception:  # noqa: BLE001 — defensive
        return 0
    if not pending:
        return 0
    try:
        timeout_clamped = max(0.1, min(60.0, float(timeout)))
    except (TypeError, ValueError):
        timeout_clamped = 5.0
    try:
        done, _pending_after = await asyncio.wait(
            pending, timeout=timeout_clamped,
        )
        return len(done)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[VenomV3FFN] drain raised: %s", exc,
        )
        return 0


def ffn_pending_count() -> int:
    """Read-only count of in-flight FFN tasks. Operator
    visibility primitive — observability surfaces (status
    REPL / SSE) compose this. NEVER raises."""
    try:
        return sum(
            1 for t in list(_FFN_TASK_REGISTRY) if not t.done()
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0


def reset_ffn_registry_for_tests() -> None:
    """Test-only — clear the FFN registry. Production code
    MUST NOT call this (would orphan in-flight tasks from the
    drain primitive)."""
    global _FFN_TASK_REGISTRY
    _FFN_TASK_REGISTRY = weakref.WeakSet()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "LIFECYCLE_HOOK_EXECUTOR_SCHEMA_VERSION",
    "drain_ffn_tasks",
    "ffn_pending_count",
    "fire_hooks",
    "register_shipped_invariants",
    "reset_ffn_registry_for_tests",
]


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned shipped_code_invariants contribution
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Register Slice 3's structural invariants. Discovered
    automatically. Returns :class:`ShippedCodeInvariant` instances."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_authority_allowlist(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 3 may import only Slice 1 + Slice 2."""
        violations: list = []
        allowed = {
            "backend.core.ouroboros.governance.lifecycle_hook",
            "backend.core.ouroboros.governance.lifecycle_hook_registry",
        }
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in _ast.walk(tree):
            if isinstance(fnode, _ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                lineno = getattr(node, "lineno", 0)
                if any(s <= lineno <= e for s, e in exempt_ranges):
                    continue
                for ban in banned_substrings:
                    if ban in module:
                        violations.append(
                            f"line {lineno}: BANNED orchestrator-tier "
                            f"substring {ban!r} in {module!r}"
                        )
                if "backend." in module or (
                    "governance" in module and module
                ):
                    if module not in allowed:
                        violations.append(
                            f"line {lineno}: import outside Slice 3 "
                            f"allowlist: {module!r}"
                        )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"MUST NOT {node.func.id}()"
                        )
        return tuple(violations)

    def _validate_fail_isolated_gather(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        """Critical safety property: Slice 3 executor MUST use
        ``return_exceptions=True`` on its asyncio.gather call so
        one cancelled/failed task doesn't cancel siblings. Drift
        here would silently break fail-isolation."""
        violations: list = []
        if "return_exceptions=True" not in source:
            violations.append(
                "executor must use return_exceptions=True on "
                "asyncio.gather (fail-isolation property)"
            )
        if "asyncio.gather" not in source:
            violations.append(
                "executor must use asyncio.gather for parallel "
                "hook execution"
            )
        if "asyncio.wait_for" not in source:
            violations.append(
                "executor must use asyncio.wait_for for per-hook "
                "timeout enforcement"
            )
        return tuple(violations)

    def _validate_v3_ffn_discipline(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Venom V3 (2026-05-07) — FFN tasks MUST be:

          1. Created via ``loop.create_task`` (named, registered,
             callback-attached) inside ``_schedule_ffn_tasks``.
          2. Named with ``name=`` kwarg (operator-visible).
          3. Scheduled AFTER ``compute_hook_decision`` (FFN
             results MUST NOT contribute to BLOCK-wins).
          4. Tracked in :data:`_FFN_TASK_REGISTRY` weak set.
          5. NEVER awaited inside ``fire_hooks`` (the dispatcher
             returns the decision immediately after scheduling).

        AST scan enforces (1)-(4); test suite enforces (5) via
        spy."""
        violations: list = []
        # Find _schedule_ffn_tasks function — its body MUST
        # contain loop.create_task with name= kwarg.
        scheduler_func = None
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "_schedule_ffn_tasks"
            ):
                scheduler_func = node
                break
        if scheduler_func is None:
            violations.append(
                "Venom V3 — _schedule_ffn_tasks function "
                "missing"
            )
            return tuple(violations)
        found_named_create_task = False
        found_registry_add = False
        for sub in _ast.walk(scheduler_func):
            if isinstance(sub, _ast.Call):
                fn = sub.func
                # Detect loop.create_task(...) OR
                # asyncio.create_task(...) with name= kwarg.
                if (
                    isinstance(fn, _ast.Attribute)
                    and fn.attr == "create_task"
                ):
                    has_name_kw = any(
                        kw.arg == "name" for kw in sub.keywords
                    )
                    if has_name_kw:
                        found_named_create_task = True
                # _FFN_TASK_REGISTRY.add(...)
                if (
                    isinstance(fn, _ast.Attribute)
                    and fn.attr == "add"
                    and isinstance(fn.value, _ast.Name)
                    and fn.value.id == "_FFN_TASK_REGISTRY"
                ):
                    found_registry_add = True
        if not found_named_create_task:
            violations.append(
                "Venom V3 — _schedule_ffn_tasks MUST create "
                "tasks with name= kwarg (operator-visible)"
            )
        if not found_registry_add:
            violations.append(
                "Venom V3 — _schedule_ffn_tasks MUST add tasks "
                "to _FFN_TASK_REGISTRY (weak set; drain "
                "primitive depends on it)"
            )
        # Check fire_hooks: _schedule_ffn_tasks call MUST come
        # AFTER compute_hook_decision call.
        fire_hooks_func = None
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.AsyncFunctionDef)
                and node.name == "fire_hooks"
            ):
                fire_hooks_func = node
                break
        if fire_hooks_func is None:
            violations.append(
                "Venom V3 — fire_hooks function missing"
            )
            return tuple(violations)
        compute_lineno = None
        schedule_lineno = None
        for sub in _ast.walk(fire_hooks_func):
            if isinstance(sub, _ast.Call):
                fn = sub.func
                if (
                    isinstance(fn, _ast.Name)
                    and fn.id == "compute_hook_decision"
                ):
                    compute_lineno = getattr(
                        sub, "lineno", None,
                    )
                if (
                    isinstance(fn, _ast.Name)
                    and fn.id == "_schedule_ffn_tasks"
                ):
                    schedule_lineno = getattr(
                        sub, "lineno", None,
                    )
        if compute_lineno is None:
            violations.append(
                "Venom V3 — fire_hooks must call "
                "compute_hook_decision"
            )
        if schedule_lineno is None:
            violations.append(
                "Venom V3 — fire_hooks must call "
                "_schedule_ffn_tasks"
            )
        if (
            compute_lineno is not None
            and schedule_lineno is not None
            and schedule_lineno < compute_lineno
        ):
            violations.append(
                f"Venom V3 — _schedule_ffn_tasks (line "
                f"{schedule_lineno}) MUST be called AFTER "
                f"compute_hook_decision (line {compute_lineno}) "
                f"so FFN results don't contribute to BLOCK-wins"
            )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/lifecycle_hook_executor.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="lifecycle_hook_executor_authority_allowlist",
            target_file=target,
            description=(
                "Slice 3 executor imports stay within "
                "{lifecycle_hook, lifecycle_hook_registry} (+ "
                "registration-contract exemption). Banned: "
                "orchestrator-tier."
            ),
            validate=_validate_authority_allowlist,
        ),
        ShippedCodeInvariant(
            invariant_name="lifecycle_hook_executor_fail_isolated",
            target_file=target,
            description=(
                "Slice 3 executor must use asyncio.gather with "
                "return_exceptions=True (fail-isolation: one "
                "cancelled/failed task doesn't cancel siblings) "
                "AND asyncio.wait_for (per-hook timeout)."
            ),
            validate=_validate_fail_isolated_gather,
        ),
        ShippedCodeInvariant(
            invariant_name="lifecycle_hook_executor_v3_ffn_discipline",
            target_file=target,
            description=(
                "Venom V3 (2026-05-07) — FFN tasks named via "
                "name= kwarg, registered in _FFN_TASK_REGISTRY, "
                "scheduled AFTER compute_hook_decision. "
                "Operator binding 2026-05-07: 'task names + "
                "weak registry + ... drain on graceful "
                "shutdown.'"
            ),
            validate=_validate_v3_ffn_discipline,
        ),
    ]
