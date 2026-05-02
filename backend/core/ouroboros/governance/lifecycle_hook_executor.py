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
import logging
import time
from typing import Optional, Tuple

from backend.core.ouroboros.governance.lifecycle_hook import (
    AggregateHookDecision,
    HookContext,
    HookOutcome,
    HookResult,
    LifecycleEvent,
    compute_hook_decision,
    lifecycle_hooks_enabled,
    make_hook_result,
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
        result = await asyncio.wait_for(
            asyncio.to_thread(registration.callable, context),
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
    event: LifecycleEvent,
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
    if not isinstance(event, LifecycleEvent):
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

    # 2. Master flag short-circuit.
    is_enabled = (
        enabled if enabled is not None
        else lifecycle_hooks_enabled()
    )
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
    registrations: Tuple[HookRegistration, ...] = (
        active_registry.for_event(event)
    )
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

    # 6. Spawn one task per hook in parallel; gather with
    #    return_exceptions=True so one cancellation doesn't
    #    cancel siblings. Each task is bounded by the per-hook
    #    timeout + defensive wrapping inside _run_one_hook.
    tasks = [
        asyncio.create_task(
            _run_one_hook(reg, context),
            name=f"lifecycle-hook-{reg.name}",
        )
        for reg in registrations
    ]
    try:
        gathered = await asyncio.gather(
            *tasks, return_exceptions=True,
        )
    except asyncio.CancelledError:
        # Caller-initiated cancellation. Cancel any stragglers
        # before propagating.
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
    for reg, gathered_item in zip(registrations, gathered):
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

    # 8. Aggregate via Slice 1 (BLOCK-wins).
    return compute_hook_decision(event, tuple(results))


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "LIFECYCLE_HOOK_EXECUTOR_SCHEMA_VERSION",
    "fire_hooks",
]
