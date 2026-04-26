"""P3 Slice 2 — InlineApprovalProvider conforming to ApprovalProvider Protocol.

Implements the same async surface as :class:`CLIApprovalProvider` so the
orchestrator can swap implementations without code churn, but routes
pending requests through Slice 1's :class:`InlineApprovalQueue` so the
SerpentFlow renderer (Slice 3) can show them inline in the CLI.

Adds a JSONL **cancel-ledger audit hook** at
``.jarvis/inline_approval_audit.jsonl`` that records every terminal
decision (APPROVED / REJECTED / EXPIRED / TIMEOUT_DEFERRED) for PRD §8
absolute observability. The ledger is best-effort — failures never
propagate.

Slice 2 ships the **provider + audit ledger** only. Slice 3 adds the
SerpentFlow diff renderer + 30s prompt I/O + ``$EDITOR`` shell-out.
Slice 4 graduates the master env knob.

Authority invariants (PRD §12.2):
  * Imports limited to ``approval_provider`` (Protocol it implements)
    + ``op_context`` (typed input) + ``inline_approval`` (own slice).
    No orchestrator / policy / iron_gate / risk_tier / change_engine /
    candidate_generator / gate / semantic_guardian.
  * The audit ledger is the **only** I/O surface this module owns.
    All other state is in-memory.
  * Master flag ``JARVIS_APPROVAL_UX_INLINE_ENABLED`` (Slice 1) still
    default false. When off, the orchestrator's existing factory keeps
    returning :class:`CLIApprovalProvider`; this module is dormant.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
    CLIApprovalProvider,
)
from backend.core.ouroboros.governance.inline_approval import (
    InlineApprovalChoice,
    InlineApprovalQueue,
    InlineApprovalRequest,
    decision_timeout_s,
    get_default_queue,
    is_enabled as inline_approval_is_enabled,
)
from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger(__name__)


# Schema is frozen; bump on any field change so downstream parsers (PRD
# §8) can pin a version.
AUDIT_LEDGER_SCHEMA_VERSION: int = 1

# Audit ledger writes are bounded only by disk; provider keeps a
# soft-cap on per-process retained _PendingRequest entries to avoid
# leaking forever for a long-running daemon.
MAX_RETAINED_REQUESTS: int = 256


def audit_ledger_path() -> Path:
    """Return the JSONL audit ledger path. Env-overridable via
    ``JARVIS_INLINE_APPROVAL_AUDIT_PATH``; defaults to
    ``.jarvis/inline_approval_audit.jsonl`` under the cwd."""
    raw = os.environ.get("JARVIS_INLINE_APPROVAL_AUDIT_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "inline_approval_audit.jsonl"


# ---------------------------------------------------------------------------
# Internal pending-request container
# ---------------------------------------------------------------------------


@dataclass
class _PendingRequest:
    """Per-request bookkeeping. Mirrors the CLIApprovalProvider shape so
    behaviours (idempotent / SUPERSEDED / EXPIRED) are byte-equivalent."""

    context: OperationContext
    result: Optional[ApprovalResult]
    event: asyncio.Event
    created_at: datetime


# ---------------------------------------------------------------------------
# Audit ledger
# ---------------------------------------------------------------------------


class _AuditLedger:
    """Append-only JSONL writer. Best-effort: any I/O error is logged
    once and swallowed — never blocks the FSM."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or audit_ledger_path()
        self._lock = threading.Lock()
        self._io_warned = False

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: Dict[str, object]) -> bool:
        """Write one JSONL line. Returns True on success, False on any
        I/O failure (logged once per process)."""
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # Use a temp open + write to keep crash-safety reasonable;
                # JSONL append is naturally line-atomic on most filesystems.
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
            return True
        except OSError as exc:
            if not self._io_warned:
                logger.warning(
                    "[InlineApproval] audit ledger write failed at %s: %s "
                    "(further failures suppressed)",
                    self._path, exc,
                )
                self._io_warned = True
            return False

    def reset_warned_for_tests(self) -> None:
        self._io_warned = False


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class InlineApprovalProvider:
    """Inline-CLI approval provider. Conforms to
    :class:`approval_provider.ApprovalProvider` Protocol.

    Single-event-loop async use (matches the CLIApprovalProvider
    contract). The queue + audit ledger have their own internal locks
    for cross-thread inspection by the SerpentFlow renderer (Slice 3).
    """

    def __init__(
        self,
        queue: Optional[InlineApprovalQueue] = None,
        audit_ledger: Optional[_AuditLedger] = None,
    ) -> None:
        # Queue defaults to the process-wide singleton so SerpentFlow
        # can observe the same state without explicit wiring.
        self._queue = queue if queue is not None else get_default_queue()
        self._audit = audit_ledger if audit_ledger is not None else _AuditLedger()
        self._requests: Dict[str, _PendingRequest] = {}

    # -- ApprovalProvider.request --

    async def request(self, context: OperationContext) -> str:
        """Submit an operation for approval. Idempotent on the same
        ``context.op_id``."""
        request_id = context.op_id
        if request_id in self._requests:
            return request_id

        # Soft cap on retained requests so a long daemon doesn't grow
        # unbounded. FIFO eviction of decided requests only — never
        # evict undecided state.
        self._gc_decided_if_needed()

        risk_tier_name = self._risk_tier_name(context)
        target_files = tuple(context.target_files or ())

        ev = asyncio.Event()
        now = time.time()
        req_obj = InlineApprovalRequest(
            request_id=request_id,
            op_id=context.op_id,
            risk_tier=risk_tier_name,
            target_files=target_files,
            diff_summary=getattr(context, "description", "") or "",
            created_unix=now,
            deadline_unix=now + decision_timeout_s(),
        )
        # Best-effort enqueue. If the queue is full, we still create the
        # in-process pending entry so await_decision can EXPIRE — never
        # auto-approve. Slice 3 surfaces queue-full via the SerpentFlow
        # renderer.
        enqueued = self._queue.enqueue(req_obj)
        if not enqueued:
            logger.warning(
                "[InlineApproval] queue full or duplicate at request: %s",
                request_id,
            )

        self._requests[request_id] = _PendingRequest(
            context=context,
            result=None,
            event=ev,
            created_at=datetime.now(tz=timezone.utc),
        )
        logger.info(
            "[InlineApproval] PENDING op_id=%s files=%s tier=%s",
            request_id, target_files, risk_tier_name,
        )
        return request_id

    # -- ApprovalProvider.approve --

    async def approve(
        self, request_id: str, approver: str,
    ) -> ApprovalResult:
        pending = self._get_or_raise(request_id)
        if pending.result is not None:
            if pending.result.status is ApprovalStatus.APPROVED:
                return pending.result
            return self._superseded(request_id, approver, None)

        result = ApprovalResult(
            status=ApprovalStatus.APPROVED,
            approver=approver,
            reason=None,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )
        pending.result = result
        pending.event.set()
        # Mirror onto the queue (best-effort — queue may have rejected
        # the original enqueue with QUEUE_FULL).
        self._queue.record_decision(
            request_id=request_id,
            choice=InlineApprovalChoice.APPROVE,
            reason="",
            operator=approver,
        )
        self._audit_terminal(request_id, result, pending)
        logger.info("[InlineApproval] APPROVED %s by %s", request_id, approver)
        return result

    # -- ApprovalProvider.reject --

    async def reject(
        self, request_id: str, approver: str, reason: str,
    ) -> ApprovalResult:
        pending = self._get_or_raise(request_id)
        if pending.result is not None:
            if pending.result.status is ApprovalStatus.REJECTED:
                return pending.result
            return self._superseded(request_id, approver, reason)

        result = ApprovalResult(
            status=ApprovalStatus.REJECTED,
            approver=approver,
            reason=reason,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )
        pending.result = result
        pending.event.set()
        self._queue.record_decision(
            request_id=request_id,
            choice=InlineApprovalChoice.REJECT,
            reason=reason,
            operator=approver,
        )
        self._audit_terminal(request_id, result, pending)
        logger.info(
            "[InlineApproval] REJECTED %s by %s reason=%r",
            request_id, approver, reason,
        )
        return result

    # -- ApprovalProvider.await_decision --

    async def await_decision(
        self, request_id: str, timeout_s: float,
    ) -> ApprovalResult:
        pending = self._get_or_raise(request_id)
        if pending.result is not None:
            return pending.result

        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            if pending.result is None:
                expired = ApprovalResult(
                    status=ApprovalStatus.EXPIRED,
                    approver=None,
                    reason=None,
                    decided_at=datetime.now(tz=timezone.utc),
                    request_id=request_id,
                )
                pending.result = expired
                pending.event.set()
                self._queue.mark_timeout(request_id)
                self._audit_terminal(request_id, expired, pending)
                logger.warning(
                    "[InlineApproval] EXPIRED %s after %.1fs",
                    request_id, timeout_s,
                )
        assert pending.result is not None
        return pending.result

    # -- ApprovalProvider.elicit (Slice 3 wires real I/O) --

    async def elicit(
        self,
        request_id: str,
        question: str,
        options: Optional[List[str]] = None,
        timeout_s: float = 300.0,
    ) -> Optional[str]:
        """Slice 2 stub: log the elicitation and return None on the
        configured timeout. Slice 3 wires the SerpentFlow prompt loop
        so the operator can answer inline."""
        # Make sure the request exists (matches CLIApprovalProvider
        # behaviour: KeyError on unknown id).
        self._get_or_raise(request_id)
        logger.info(
            "[InlineApproval] ELICIT (slice-2 stub) %s question=%r "
            "options=%r timeout=%.1fs",
            request_id, question, options, timeout_s,
        )
        try:
            await asyncio.sleep(timeout_s)
        except asyncio.CancelledError:
            raise
        return None

    # -- list_pending (parity with CLIApprovalProvider for REPL) --

    async def list_pending(self) -> List[Dict[str, object]]:
        out: List[Dict[str, object]] = []
        for request_id, pending in self._requests.items():
            if pending.result is not None:
                continue
            out.append({
                "op_id": pending.context.op_id,
                "description": getattr(pending.context, "description", ""),
                "target_files": pending.context.target_files,
                "created_at": pending.created_at,
                "request_id": request_id,
            })
        return out

    # -- internals --

    def _get_or_raise(self, request_id: str) -> _PendingRequest:
        try:
            return self._requests[request_id]
        except KeyError:
            raise KeyError(
                f"Unknown approval request_id: {request_id!r}",
            ) from None

    def _superseded(
        self, request_id: str, approver: str, reason: Optional[str],
    ) -> ApprovalResult:
        result = ApprovalResult(
            status=ApprovalStatus.SUPERSEDED,
            approver=approver,
            reason=reason,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )
        # SUPERSEDED is a terminal observation, not a state mutation —
        # we still audit it so operators can trace the second-call.
        pending = self._requests.get(request_id)
        self._audit_terminal(request_id, result, pending)
        logger.warning(
            "[InlineApproval] SUPERSEDED %s by %s", request_id, approver,
        )
        return result

    def _audit_terminal(
        self,
        request_id: str,
        result: ApprovalResult,
        pending: Optional[_PendingRequest],
    ) -> None:
        target_files: tuple = ()
        risk_tier_name = "UNKNOWN"
        if pending is not None:
            target_files = tuple(pending.context.target_files or ())
            risk_tier_name = self._risk_tier_name(pending.context)
        record = {
            "schema_version": AUDIT_LEDGER_SCHEMA_VERSION,
            "request_id": request_id,
            "op_id": request_id,
            "status": result.status.name,
            "approver": result.approver,
            "reason": result.reason,
            "decided_at_unix": (
                result.decided_at.timestamp()
                if result.decided_at is not None else None
            ),
            "target_files": list(target_files),
            "risk_tier": risk_tier_name,
        }
        self._audit.append(record)

    def _gc_decided_if_needed(self) -> None:
        """Soft-cap retained requests by evicting decided ones (FIFO)."""
        if len(self._requests) < MAX_RETAINED_REQUESTS:
            return
        # Walk in insertion order, drop decided entries until under cap.
        to_drop: List[str] = []
        for rid, pending in self._requests.items():
            if pending.result is not None:
                to_drop.append(rid)
                if len(self._requests) - len(to_drop) < MAX_RETAINED_REQUESTS:
                    break
        for rid in to_drop:
            self._requests.pop(rid, None)

    @staticmethod
    def _risk_tier_name(context: OperationContext) -> str:
        tier = getattr(context, "risk_tier", None)
        if tier is None:
            return "UNKNOWN"
        return getattr(tier, "name", str(tier))


def build_approval_provider(
    project_root: Optional[Path] = None,
):
    """Slice 4 graduation factory.

    Returns ``InlineApprovalProvider`` when
    ``JARVIS_APPROVAL_UX_INLINE_ENABLED`` is truthy (default **true**
    post-graduation), else falls back to the legacy
    :class:`CLIApprovalProvider`.

    Single source of truth for which approval surface the loop binds.
    Hot-revert: ``JARVIS_APPROVAL_UX_INLINE_ENABLED=false`` returns the
    CLI provider on the next factory call — no orchestrator restart
    needed for the construction site itself, though the existing
    process holds whichever was built at boot.

    The factory is the only place the env knob gates behaviour for
    construction selection — every downstream caller (renderer, queue,
    audit ledger) stays flag-agnostic so they remain inspectable even
    when the inline path is disabled (operators may want to read prior
    decisions). This mirrors the P0 / P0.5 graduation pattern."""
    if inline_approval_is_enabled():
        return InlineApprovalProvider()
    return CLIApprovalProvider(project_root=project_root)


__all__ = [
    "AUDIT_LEDGER_SCHEMA_VERSION",
    "InlineApprovalProvider",
    "MAX_RETAINED_REQUESTS",
    "audit_ledger_path",
    "build_approval_provider",
]
