"""
CommProtocolCommSink — Phase 1 Step 5 observability adapter.

Bridges the SubagentOrchestrator's CommSink protocol (sync `emit_spawn` /
`emit_result`) to the async CommProtocol heartbeat channel so subagent
lifecycle events flow through the same causal-ordered event spine that
carries INTENT / PLAN / HEARTBEAT / DECISION / POSTMORTEM messages.

Design choices:
  * Uses HEARTBEAT message type with phase="subagent_spawn" /
    "subagent_result" rather than introducing new MessageType enum
    values — smaller blast radius, consumers that don't know about
    subagents simply ignore unfamiliar heartbeat phases.
  * Fire-and-forget scheduling on the running loop via
    `asyncio.create_task`. The sync CommSink methods never await —
    orchestrator dispatch remains non-blocking on comm-transport
    latency. Schedule failures (no running loop, task creation error)
    are swallowed: observability degradation is preferred over
    breaking dispatch.
  * Late-bound CommProtocol lookup via callable so the sink can be
    constructed at GovernedLoopService boot time before the governance
    stack's CommProtocol instance is finalized. The callable returns
    `None` until the stack is ready; the sink silently no-ops.
  * Ledger-sink variant defers to the in-memory InMemoryLedgerSink
    until a richer OperationLedger integration lands in a follow-up.

Manifesto alignment:
  §7 — Absolute observability: subagent spawn/result events share the
       same causal-ordered heartbeat channel as the rest of the
       operation. A replay of the op's messages reconstructs the full
       subagent dispatch timeline.
  §3 — Disciplined concurrency: emit is fire-and-forget, never blocks
       the caller, and never raises into the dispatch path.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from backend.core.ouroboros.governance.subagent_contracts import (
    SubagentResult,
    SubagentType,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Heartbeat phase tags — consumers key off these strings.
# ============================================================================

PHASE_SUBAGENT_SPAWN = "subagent_spawn"
PHASE_SUBAGENT_RESULT = "subagent_result"


# ============================================================================
# CommProtocolCommSink
# ============================================================================


class CommProtocolCommSink:
    """CommSink adapter that emits subagent events as CommProtocol heartbeats.

    The sink is constructed with a *callable* that returns the active
    CommProtocol instance (or None). This late binding is required
    because GovernedLoopService builds the subagent orchestrator before
    the full governance stack is ready; a direct CommProtocol reference
    captured at construction time would be stale.

    The callable is invoked on every event; if it returns None, the
    event is dropped silently. Schedule failures are swallowed so
    observability never breaks dispatch.
    """

    def __init__(
        self,
        comm_lookup: Callable[[], Optional[Any]],
    ) -> None:
        self._get_comm = comm_lookup

    def emit_spawn(
        self,
        parent_op_id: str,
        subagent_id: str,
        subagent_type: SubagentType,
        goal: str,
    ) -> None:
        comm = self._safe_lookup()
        if comm is None:
            return
        self._schedule(comm.emit_heartbeat(
            op_id=parent_op_id,
            phase=PHASE_SUBAGENT_SPAWN,
            progress_pct=0.0,
            subagent_id=subagent_id,
            subagent_type=subagent_type.value,
            goal=goal[:160] if goal else "",
        ))

    def emit_result(
        self,
        parent_op_id: str,
        subagent_id: str,
        result: SubagentResult,
    ) -> None:
        comm = self._safe_lookup()
        if comm is None:
            return
        self._schedule(comm.emit_heartbeat(
            op_id=parent_op_id,
            phase=PHASE_SUBAGENT_RESULT,
            progress_pct=1.0,
            subagent_id=subagent_id,
            subagent_type=result.subagent_type.value,
            status=result.status.value,
            findings_count=len(result.findings),
            files_read=len(result.files_read),
            tool_calls=result.tool_calls,
            tool_diversity=result.tool_diversity,
            cost_usd=round(result.cost_usd, 6),
            provider_used=result.provider_used,
            fallback_triggered=result.fallback_triggered,
            duration_s=round(result.duration_s, 3),
            error_class=result.error_class,
        ))

    # ------------------------------------------------------------------
    # Internals — fault-isolated scheduling and lookup
    # ------------------------------------------------------------------

    def _safe_lookup(self) -> Optional[Any]:
        """Resolve the CommProtocol via the late-bound callback.

        Any exception from the callback is swallowed and logged at debug.
        """
        try:
            return self._get_comm()
        except Exception as exc:  # noqa: BLE001 — observability is best-effort
            logger.debug(
                "[CommProtocolCommSink] comm lookup raised: %s", exc
            )
            return None

    def _schedule(self, coro) -> None:
        """Fire-and-forget schedule on the running loop.

        If there's no running loop (e.g., during synchronous boot or in
        a unit test without an event loop), the coroutine is dropped
        silently with a debug log. Observability degrades; dispatch
        never breaks.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — drop the coroutine to avoid an "attached
            # to a different loop" leak. CLose the coroutine explicitly
            # so the GC doesn't flag it as never-awaited.
            coro.close()
            return
        try:
            loop.create_task(coro)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CommProtocolCommSink] create_task failed: %s", exc
            )
            try:
                coro.close()
            except Exception:
                pass


# ============================================================================
# Factory helper — constructs the sink from a GovernedLoopService-style
# governance stack reference via the same late-bound pattern used by
# ToolNarrationChannel. Callers pass either a concrete CommProtocol or
# a getter that returns one.
# ============================================================================


def build_comm_sink_from_gls(gls: Any) -> CommProtocolCommSink:
    """Build a CommProtocolCommSink that resolves CommProtocol via a GLS-shaped
    attribute chain:

        gls._governance_stack.comm  →  CommProtocol

    Unknown structure or unavailable stack returns None from the lookup
    (sink silently no-ops).
    """
    def _lookup() -> Optional[Any]:
        gov = getattr(gls, "_governance_stack", None)
        if gov is None:
            return None
        return getattr(gov, "comm", None)

    return CommProtocolCommSink(comm_lookup=_lookup)
