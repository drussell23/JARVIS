"""
Approval Provider Protocol & CLI Implementation
=================================================

Human-in-the-loop approval gate for the governed self-programming pipeline.

When the risk engine classifies an operation as ``APPROVAL_REQUIRED``, the
orchestrator calls :meth:`ApprovalProvider.request` to submit the operation
for review, then :meth:`ApprovalProvider.await_decision` to block until a
human makes a decision (or timeout expires).

This module provides:

- :class:`ApprovalStatus` -- enum of possible decision states
- :class:`ApprovalResult` -- frozen result dataclass
- :class:`ApprovalProvider` -- runtime-checkable protocol
- :class:`CLIApprovalProvider` -- in-memory implementation for Phase 1 CLI use

Behavioral Guarantees
---------------------

- **Idempotent**: approving an already-approved request returns the existing
  decision unchanged.  Same for reject.
- **Timeout -> EXPIRED**: ``await_decision`` never auto-approves; timeout
  produces an ``EXPIRED`` result.
- **Late decision after EXPIRED -> SUPERSEDED**: once a request has expired
  or been decided, any subsequent approve/reject returns ``SUPERSEDED``.
- **Unknown request_id -> KeyError**: all operations on unknown IDs raise
  ``KeyError``.

Future adapters (TUI, voice, webhook) implement the same
:class:`ApprovalProvider` protocol.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ApprovalStatus Enum
# ---------------------------------------------------------------------------


class ApprovalStatus(Enum):
    """Possible states for an approval decision."""

    PENDING = auto()
    APPROVED = auto()
    REJECTED = auto()
    EXPIRED = auto()
    SUPERSEDED = auto()


# ---------------------------------------------------------------------------
# ApprovalResult frozen dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalResult:
    """Immutable result of an approval decision.

    Parameters
    ----------
    status:
        The decision status.
    approver:
        Identifier of the human or system that made the decision.
        ``None`` for PENDING or EXPIRED.
    reason:
        Free-text justification. ``None`` unless rejected.
    decided_at:
        Timestamp when the decision was made. ``None`` for PENDING.
    request_id:
        The request identifier (same as the operation's ``op_id``).
    """

    status: ApprovalStatus
    approver: Optional[str]
    reason: Optional[str]
    decided_at: Optional[datetime]
    request_id: str


# ---------------------------------------------------------------------------
# Internal _PendingRequest
# ---------------------------------------------------------------------------


@dataclass
class _PendingRequest:
    """Internal mutable container tracking a single approval request.

    Not exposed outside this module.

    Parameters
    ----------
    context:
        The :class:`OperationContext` that was submitted for approval.
    result:
        The finalized :class:`ApprovalResult`, or ``None`` if still pending.
    event:
        An :class:`asyncio.Event` that is set once a decision is recorded.
    created_at:
        Timestamp when the request was first submitted.
    """

    context: OperationContext
    result: Optional[ApprovalResult]
    event: asyncio.Event
    created_at: datetime


# ---------------------------------------------------------------------------
# ApprovalProvider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ApprovalProvider(Protocol):
    """Runtime-checkable protocol for approval providers.

    Any class that implements these four async methods can serve as
    an approval backend.
    """

    async def request(self, context: OperationContext) -> str:
        """Submit an operation for approval.

        Idempotent: calling with the same ``context.op_id`` returns
        the same ``request_id`` without creating a duplicate.

        Parameters
        ----------
        context:
            The operation context requiring approval.

        Returns
        -------
        str
            The request identifier (which is ``context.op_id``).
        """
        ...  # pragma: no cover

    async def approve(self, request_id: str, approver: str) -> ApprovalResult:
        """Approve a pending request.

        Idempotent: approving an already-approved request returns the
        existing decision.  Approving after EXPIRED or REJECTED returns
        SUPERSEDED.

        Parameters
        ----------
        request_id:
            The request identifier returned by :meth:`request`.
        approver:
            Identifier of the human or system approving.

        Returns
        -------
        ApprovalResult

        Raises
        ------
        KeyError
            If *request_id* is unknown.
        """
        ...  # pragma: no cover

    async def reject(
        self, request_id: str, approver: str, reason: str
    ) -> ApprovalResult:
        """Reject a pending request.

        Idempotent: rejecting an already-rejected request returns the
        existing decision.  Rejecting after EXPIRED or APPROVED returns
        SUPERSEDED.

        Parameters
        ----------
        request_id:
            The request identifier returned by :meth:`request`.
        approver:
            Identifier of the human or system rejecting.
        reason:
            Free-text justification for the rejection.

        Returns
        -------
        ApprovalResult

        Raises
        ------
        KeyError
            If *request_id* is unknown.
        """
        ...  # pragma: no cover

    async def await_decision(
        self, request_id: str, timeout_s: float
    ) -> ApprovalResult:
        """Block until a decision is made or timeout expires.

        On timeout the request is marked EXPIRED and the event is set
        so that any concurrent waiters also receive EXPIRED.  Never
        auto-approves.

        Parameters
        ----------
        request_id:
            The request identifier returned by :meth:`request`.
        timeout_s:
            Maximum seconds to wait.

        Returns
        -------
        ApprovalResult

        Raises
        ------
        KeyError
            If *request_id* is unknown.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# CLIApprovalProvider
# ---------------------------------------------------------------------------


class CLIApprovalProvider:
    """In-memory approval provider for Phase 1 CLI interaction.

    Requests are stored in a dict keyed by ``request_id`` (which is the
    operation's ``op_id``).  Decisions are coordinated via per-request
    :class:`asyncio.Event` instances.

    Thread Safety
    -------------
    This class is designed for single-event-loop async use.  All state
    mutations happen within the event loop so no explicit locking is needed.
    """

    def __init__(self, project_root: Optional[Path] = None) -> None:
        self._requests: Dict[str, _PendingRequest] = {}
        self._project_root = project_root

    # -- request --

    async def request(self, context: OperationContext) -> str:
        """Submit an operation for approval.

        Idempotent on the same ``context.op_id``.
        """
        request_id = context.op_id
        if request_id not in self._requests:
            self._requests[request_id] = _PendingRequest(
                context=context,
                result=None,
                event=asyncio.Event(),
                created_at=datetime.now(tz=timezone.utc),
            )
            logger.info(
                "[Approval] Pending: op_id=%s desc=%s files=%s",
                context.op_id, context.description, context.target_files,
            )
        return request_id

    # -- approve --

    async def approve(self, request_id: str, approver: str) -> ApprovalResult:
        """Approve a pending request.

        Idempotent if already APPROVED.  Returns SUPERSEDED if the
        request was already decided with a different terminal status
        (REJECTED, EXPIRED).
        """
        pending = self._get_or_raise(request_id)

        if pending.result is not None:
            # Already decided
            if pending.result.status is ApprovalStatus.APPROVED:
                return pending.result
            # Was REJECTED or EXPIRED -> SUPERSEDED
            logger.warning("[Approval] SUPERSEDED: %s (approve after %s)", request_id, pending.result.status.name)
            return ApprovalResult(
                status=ApprovalStatus.SUPERSEDED,
                approver=approver,
                reason=None,
                decided_at=datetime.now(tz=timezone.utc),
                request_id=request_id,
            )

        result = ApprovalResult(
            status=ApprovalStatus.APPROVED,
            approver=approver,
            reason=None,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )
        pending.result = result
        pending.event.set()
        logger.info("[Approval] APPROVED: %s by %s", request_id, approver)
        return result

    # -- reject --

    async def reject(
        self, request_id: str, approver: str, reason: str
    ) -> ApprovalResult:
        """Reject a pending request.

        Idempotent if already REJECTED.  Returns SUPERSEDED if the
        request was already decided with a different terminal status
        (APPROVED, EXPIRED).
        """
        pending = self._get_or_raise(request_id)

        if pending.result is not None:
            # Already decided
            if pending.result.status is ApprovalStatus.REJECTED:
                return pending.result
            # Was APPROVED or EXPIRED -> SUPERSEDED
            logger.warning("[Approval] SUPERSEDED: %s (reject after %s)", request_id, pending.result.status.name)
            return ApprovalResult(
                status=ApprovalStatus.SUPERSEDED,
                approver=approver,
                reason=reason,
                decided_at=datetime.now(tz=timezone.utc),
                request_id=request_id,
            )

        result = ApprovalResult(
            status=ApprovalStatus.REJECTED,
            approver=approver,
            reason=reason,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )
        # GAP 8: auto-memory — persist rejection reason to OUROBOROS.md
        if self._project_root is not None:
            try:
                from backend.core.ouroboros.governance.correction_writer import write_correction
                write_correction(
                    project_root=self._project_root,
                    op_id=request_id,
                    reason=reason,
                )
            except Exception as _exc:
                logger.warning("[Approval] correction_writer failed for op=%s: %s", request_id, _exc)
        pending.result = result
        pending.event.set()
        logger.info("[Approval] REJECTED: %s by %s reason=%r", request_id, approver, reason)
        return result

    # -- await_decision --

    async def await_decision(
        self, request_id: str, timeout_s: float
    ) -> ApprovalResult:
        """Block until a decision is made or *timeout_s* expires.

        On timeout the request is stamped EXPIRED and the event is set.
        """
        pending = self._get_or_raise(request_id)

        # If already decided, return immediately
        if pending.result is not None:
            return pending.result

        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            # Only mark expired if no decision snuck in right at the deadline
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
                logger.warning("[Approval] EXPIRED: %s after %.1fs", request_id, timeout_s)

        # At this point pending.result is guaranteed non-None
        assert pending.result is not None  # invariant
        return pending.result

    # -- list_pending --

    async def list_pending(self) -> List[Dict[str, Any]]:
        """Return a list of undecided requests.

        Each entry is a dict with keys: ``op_id``, ``description``,
        ``target_files``, ``created_at``, ``request_id``.
        """
        result: List[Dict[str, Any]] = []
        for request_id, pending in self._requests.items():
            if pending.result is not None:
                continue
            result.append(
                {
                    "op_id": pending.context.op_id,
                    "description": pending.context.description,
                    "target_files": pending.context.target_files,
                    "created_at": pending.created_at,
                    "request_id": request_id,
                }
            )
        return result

    # -- internal --

    def _get_or_raise(self, request_id: str) -> _PendingRequest:
        """Look up a pending request or raise ``KeyError``."""
        try:
            return self._requests[request_id]
        except KeyError:
            raise KeyError(
                f"Unknown approval request_id: {request_id!r}"
            ) from None
