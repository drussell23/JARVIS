"""
LedgerSubagentSink — Phase 1 Step 5b subagent persistence adapter.

Bridges the SubagentOrchestrator's sync `LedgerSink.append()` protocol to
the async `OperationLedger.append(entry)` persistence API so subagent
records survive process restarts and accumulate alongside the rest of
the operation's durable ledger history.

Design choices:
  * Sync `append(parent_op_id, subagent_id, result)` method (matching the
    LedgerSink protocol). Internally constructs a `LedgerEntry` keyed on
    `(parent_op_id, OperationState.SUBAGENT_DISPATCH, subagent_id)` so
    multiple subagents per parent op coexist under the same op_id
    without the (op_id, state) dedup collapsing them.
  * Schedules the async append via `asyncio.create_task` on the running
    loop. The sync caller does not await — same fire-and-forget pattern
    as CommProtocolCommSink. If no loop is running, the coroutine is
    closed silently and observability degrades without breaking
    dispatch.
  * Late-bound OperationLedger resolution via callable, matching the
    CommProtocolCommSink pattern. Safe to construct before the
    governance stack's ledger is ready; a missing ledger yields a
    silent no-op.
  * The ledger entry's `data` field carries the full SubagentResult
    telemetry needed for postmortem reconstruction: status, findings
    count, tool calls, tool diversity, cost, provider, fallback flag,
    duration, error class, and the subagent_type label. Truncated
    payloads are NOT stored here — findings[] itself lives only in
    the prompt return path, not the ledger.

Manifesto alignment:
  §4 — Synthetic Soul: subagent outcomes become durable memory. A
       future ConsciousnessBridge can scan SUBAGENT_DISPATCH records
       for per-file exploration-quality signals.
  §6 — Iron Gate: failed subagents are remembered. IronGateDiversity
       rejections, provider-stall fallback events, budget-exhaustion
       terminations all hit disk with typed error_class so the system
       learns which dispatch patterns to avoid.
  §7 — Absolute Observability: every subagent dispatch produces one
       durable ledger record, mirroring the rest of the op's history.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from backend.core.ouroboros.governance.subagent_contracts import (
    SubagentResult,
)

logger = logging.getLogger(__name__)


class LedgerSubagentSink:
    """LedgerSink adapter that persists subagent records to OperationLedger.

    The sink is constructed with a callable that returns the active
    OperationLedger instance (or None). Late binding is required because
    GovernedLoopService builds the subagent orchestrator alongside the
    tool backend, while the ledger is typically part of the governance
    stack constructed earlier but attached via a separate reference.

    The callable is invoked on every event; if it returns None, the
    record is dropped silently. Scheduling and persistence failures are
    swallowed so observability never breaks dispatch.
    """

    def __init__(
        self,
        ledger_lookup: Callable[[], Optional[Any]],
    ) -> None:
        self._get_ledger = ledger_lookup

    def append(
        self,
        parent_op_id: str,
        subagent_id: str,
        result: SubagentResult,
    ) -> None:
        """Schedule a persistent LedgerEntry write for this subagent record.

        The entry is keyed on ``(parent_op_id, SUBAGENT_DISPATCH, subagent_id)``
        via the LedgerEntry.entry_id disambiguator, so parallel subagents
        within one parent op each produce a distinct durable record.
        """
        ledger = self._safe_lookup()
        if ledger is None:
            return

        # Build the entry synchronously so any serialization issue surfaces
        # here rather than inside the fire-and-forget task where we'd lose
        # the traceback.
        try:
            from backend.core.ouroboros.governance.ledger import (
                LedgerEntry,
                OperationState,
            )
            entry = LedgerEntry(
                op_id=parent_op_id,
                state=OperationState.SUBAGENT_DISPATCH,
                data=self._build_data(subagent_id, result),
                timestamp=time.monotonic(),
                wall_time=time.time(),
                entry_id=subagent_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[LedgerSubagentSink] entry construction failed: %s", exc
            )
            return

        self._schedule_append(ledger, entry)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_data(
        self,
        subagent_id: str,
        result: SubagentResult,
    ) -> dict:
        """Shape the ledger entry's ``data`` field from a SubagentResult.

        We intentionally omit the findings[] array from the ledger record
        — findings already flow back to the model via the prompt-return
        path, and persisting full evidence strings here would bloat the
        ledger without a clear consumer. A future postmortem tool can
        reconstruct finding-level detail from the heartbeat message log.
        """
        return {
            "subagent_id": subagent_id,
            "subagent_type": result.subagent_type.value,
            "status": result.status.value,
            "goal": (result.goal or "")[:320],
            "findings_count": len(result.findings),
            "files_read_count": len(result.files_read),
            "search_queries_count": len(result.search_queries),
            "cost_usd": round(result.cost_usd, 6),
            "tool_calls": result.tool_calls,
            "tool_diversity": result.tool_diversity,
            "provider_used": result.provider_used,
            "fallback_triggered": result.fallback_triggered,
            "duration_s": round(result.duration_s, 3),
            "error_class": result.error_class,
            "error_detail": (result.error_detail or "")[:500],
            "schema_version": result.schema_version,
        }

    def _safe_lookup(self) -> Optional[Any]:
        try:
            return self._get_ledger()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[LedgerSubagentSink] ledger lookup raised: %s", exc
            )
            return None

    def _schedule_append(self, ledger: Any, entry: Any) -> None:
        """Fire-and-forget schedule of the async append call."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — drop the write to avoid "attached to a
            # different loop" leaks. Close the coroutine we would have
            # created by not creating it at all.
            return
        try:
            loop.create_task(self._append_task(ledger, entry))
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[LedgerSubagentSink] create_task failed: %s", exc
            )

    async def _append_task(self, ledger: Any, entry: Any) -> None:
        """Fault-isolated wrapper around the ledger append call.

        Any failure in the ledger path (file lock contention, disk full,
        permission error) is logged at debug and swallowed. The subagent
        dispatch has already returned by this point; the record simply
        doesn't persist.
        """
        try:
            await ledger.append(entry)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[LedgerSubagentSink] ledger append failed op=%s sub=%s: %s",
                getattr(entry, "op_id", "?"),
                getattr(entry, "entry_id", "?"),
                exc,
            )


# ============================================================================
# Factory helper — constructs the sink with a GLS-shaped attribute chain.
# ============================================================================


def build_ledger_sink_from_gls(gls: Any) -> LedgerSubagentSink:
    """Build a LedgerSubagentSink that resolves OperationLedger from a GLS.

    Looks up ``gls._ledger`` (preferred) then ``gls._governance_stack.ledger``
    (fallback). Either missing returns None; the sink silently no-ops until
    one becomes available.
    """
    def _lookup() -> Optional[Any]:
        direct = getattr(gls, "_ledger", None)
        if direct is not None:
            return direct
        gov = getattr(gls, "_governance_stack", None)
        if gov is None:
            return None
        return getattr(gov, "ledger", None)

    return LedgerSubagentSink(ledger_lookup=_lookup)
