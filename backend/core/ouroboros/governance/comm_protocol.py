"""
5-Phase Communication Protocol
===============================

Every autonomous Ouroboros operation MUST emit a mandatory sequence of five
message types through the communication pipeline::

    INTENT -> PLAN -> HEARTBEAT -> DECISION -> POSTMORTEM

Messages are delivered to one or more *transports*.  Transport failures are
**fault-isolated**: a broken transport never blocks delivery to healthy ones.
This guarantees that governance observability degrades gracefully instead of
halting the entire autonomous pipeline.

Each message carries:
- A :class:`MessageType` discriminant
- The originating ``op_id`` (from :mod:`operation_id`)
- A monotonically increasing per-operation ``seq`` number
- An optional ``causal_parent_seq`` linking to the previous message
- An arbitrary ``payload`` dict
- A wall-clock ``timestamp``
"""

from __future__ import annotations

import enum
import logging
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MessageType enum
# ---------------------------------------------------------------------------


class MessageType(enum.Enum):
    """Discriminant for the five mandatory communication phases."""

    INTENT = "INTENT"
    PLAN = "PLAN"
    HEARTBEAT = "HEARTBEAT"
    DECISION = "DECISION"
    POSTMORTEM = "POSTMORTEM"


# ---------------------------------------------------------------------------
# CommMessage dataclass
# ---------------------------------------------------------------------------


@dataclass
class CommMessage:
    """A single message emitted during an autonomous operation lifecycle.

    Parameters
    ----------
    msg_type:
        Which of the five phases this message represents.
    op_id:
        The ``op-<uuidv7>-<origin>`` identifier for the operation.
    seq:
        Monotonically increasing sequence number within this operation.
    causal_parent_seq:
        Sequence number of the message that causally preceded this one.
        ``None`` for the very first message (INTENT).
    payload:
        Arbitrary key-value data specific to the message type.
    timestamp:
        Unix epoch seconds (wall clock) when the message was created.
    """

    msg_type: MessageType
    op_id: str
    seq: int
    causal_parent_seq: Optional[int]
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    idempotency_key: str = ""  # set by CommProtocol._emit; format: op_id:boot_id:phase:seq
    # P1-5: Cross-op global monotonic sequence for causal ordering across all
    # operations and repos.  Populated by CommProtocol._emit via GlobalEventSequencer.
    global_seq: int = 0
    # P2-6: Runbook-grade observability — cross-operation correlation identifier.
    # Defaults to op_id for single-repo ops; shared across all saga-member ops.
    # Populated by CommProtocol._emit from the active correlation context.
    correlation_id: str = ""


# ---------------------------------------------------------------------------
# LogTransport — default in-memory + logging transport
# ---------------------------------------------------------------------------


class LogTransport:
    """Transport that appends messages to an in-memory list and logs them.

    This is the default transport used when no explicit transports are
    provided to :class:`CommProtocol`.
    """

    def __init__(self) -> None:
        self.messages: List[CommMessage] = []

    async def send(self, msg: CommMessage) -> None:
        """Append *msg* to the in-memory list and emit a log line."""
        self.messages.append(msg)
        logger.info(
            "[CommProtocol] %s op=%s seq=%d payload=%s",
            msg.msg_type.value,
            msg.op_id,
            msg.seq,
            msg.payload,
        )


# ---------------------------------------------------------------------------
# CommProtocol — the 5-phase emitter
# ---------------------------------------------------------------------------


class CommProtocol:
    """Mandatory 5-phase communication emitter with fault-isolated transport.

    Parameters
    ----------
    transports:
        A list of transport objects, each exposing an ``async send(msg)``
        method.  Defaults to a single :class:`LogTransport`.
    """

    def __init__(self, transports: Optional[List[Any]] = None) -> None:
        self._transports: List[Any] = transports if transports is not None else [LogTransport()]
        self._seq_counters: Dict[str, int] = {}
        self._boot_id: str = _uuid.uuid4().hex[:12]  # stable per instance, resets per restart
        # P2-6: Active correlation_id — stamped onto every CommMessage in _emit().
        # Set by the orchestrator when beginning a new operation via set_correlation_id().
        self._active_correlation_id: str = ""

    def set_correlation_id(self, correlation_id: str) -> None:
        """Set the correlation_id stamped on all subsequent messages.

        Called by the orchestrator at the start of each operation::

            protocol.set_correlation_id(ctx.correlation_id)

        For single-repo ops this equals op_id; for multi-repo sagas all
        member ops share the saga root's correlation_id.
        """
        self._active_correlation_id = correlation_id

    # -- Sequence helpers ---------------------------------------------------

    def _next_seq(self, op_id: str) -> int:
        """Increment and return the next sequence number for *op_id*."""
        current = self._seq_counters.get(op_id, 0)
        next_val = current + 1
        self._seq_counters[op_id] = next_val
        return next_val

    def _prev_seq(self, op_id: str) -> Optional[int]:
        """Return the current (most recently emitted) sequence number.

        Returns ``None`` when no message has been emitted yet for *op_id*
        (counter is 0) or only one message exists (counter is 1 — the INTENT
        has no causal parent).  For all subsequent messages the causal parent
        is the sequence number that was current *before* ``_next_seq`` is
        called, i.e. the value stored in the counter right now.
        """
        current = self._seq_counters.get(op_id, 0)
        return current if current >= 1 else None

    # -- Internal emit ------------------------------------------------------

    async def _emit(self, msg: CommMessage) -> None:
        """Send *msg* to ALL transports, fault-isolating each one.

        A failing transport logs a warning but never prevents delivery to
        the remaining transports.

        Before delivery the message is stamped with an idempotency key of
        the form ``op_id:boot_id:phase:seq``.  The ``boot_id`` is stable for
        the lifetime of this :class:`CommProtocol` instance and resets on
        every process restart, making the combined key globally unique and
        safe for deduplication in downstream consumers.
        """
        msg.idempotency_key = (
            f"{msg.op_id}:{self._boot_id}:{msg.msg_type.value.lower()}:{msg.seq}"
        )
        # P1-5: Stamp cross-op global sequence number for causal ordering.
        try:
            from backend.core.ouroboros.event_sequencer import next_seq as _next_global_seq
            msg.global_seq = _next_global_seq()
        except Exception:
            pass  # Never block delivery on sequencer errors.
        # P2-6: Stamp correlation_id for runbook-grade cross-op observability.
        if not msg.correlation_id:
            msg.correlation_id = self._active_correlation_id or msg.op_id
        for transport in self._transports:
            try:
                await transport.send(msg)
            except Exception:
                logger.warning(
                    "[CommProtocol] Transport %r failed for op=%s seq=%d — skipping",
                    transport,
                    msg.op_id,
                    msg.seq,
                    exc_info=True,
                )

    # -- Public phase emitters ----------------------------------------------

    async def emit_intent(
        self,
        op_id: str,
        goal: str,
        target_files: List[str],
        risk_tier: str,
        blast_radius: int,
    ) -> None:
        """Emit an INTENT message (phase 1).  Sequence always starts at 1."""
        seq = self._next_seq(op_id)
        msg = CommMessage(
            msg_type=MessageType.INTENT,
            op_id=op_id,
            seq=seq,
            causal_parent_seq=None,
            payload={
                "goal": goal,
                "target_files": target_files,
                "risk_tier": risk_tier,
                "blast_radius": blast_radius,
            },
        )
        await self._emit(msg)

    async def emit_plan(
        self,
        op_id: str,
        steps: List[str],
        rollback_strategy: str,
    ) -> None:
        """Emit a PLAN message (phase 2).  Causal parent links to previous."""
        causal_parent = self._prev_seq(op_id)
        seq = self._next_seq(op_id)
        msg = CommMessage(
            msg_type=MessageType.PLAN,
            op_id=op_id,
            seq=seq,
            causal_parent_seq=causal_parent,
            payload={
                "steps": steps,
                "rollback_strategy": rollback_strategy,
            },
        )
        await self._emit(msg)

    async def emit_heartbeat(
        self,
        op_id: str,
        phase: str,
        progress_pct: float,
        **extra: Any,
    ) -> None:
        """Emit a HEARTBEAT message (phase 3).  Causal parent links to previous.

        Extra kwargs are merged into the payload so subsystems (triage,
        sensors, dream engine) can attach metadata for the TUI dashboard.
        """
        causal_parent = self._prev_seq(op_id)
        seq = self._next_seq(op_id)
        payload = {
            "phase": phase,
            "progress_pct": progress_pct,
        }
        if extra:
            payload.update(extra)
        msg = CommMessage(
            msg_type=MessageType.HEARTBEAT,
            op_id=op_id,
            seq=seq,
            causal_parent_seq=causal_parent,
            payload=payload,
        )
        await self._emit(msg)

    async def emit_decision(
        self,
        op_id: str,
        outcome: str,
        reason_code: str,
        diff_summary: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        **extra: Any,
    ) -> None:
        """Emit a DECISION message (phase 4).  Causal parent links to previous."""
        causal_parent = self._prev_seq(op_id)
        seq = self._next_seq(op_id)
        payload: Dict[str, Any] = {
            "outcome": outcome,
            "reason_code": reason_code,
        }
        if diff_summary is not None:
            payload["diff_summary"] = diff_summary
        if target_files is not None:
            payload["target_files"] = target_files
        if extra:
            payload.update(extra)
        msg = CommMessage(
            msg_type=MessageType.DECISION,
            op_id=op_id,
            seq=seq,
            causal_parent_seq=causal_parent,
            payload=payload,
        )
        await self._emit(msg)

    async def emit_postmortem(
        self,
        op_id: str,
        root_cause: str,
        failed_phase: Optional[str],
        next_safe_action: Optional[str] = None,
        target_files: Optional[List[str]] = None,
    ) -> None:
        """Emit a POSTMORTEM message (phase 5).  Causal parent links to previous."""
        causal_parent = self._prev_seq(op_id)
        seq = self._next_seq(op_id)
        payload: Dict[str, Any] = {
            "root_cause": root_cause,
            "failed_phase": failed_phase,
        }
        if next_safe_action is not None:
            payload["next_safe_action"] = next_safe_action
        if target_files is not None:
            payload["target_files"] = target_files
        msg = CommMessage(
            msg_type=MessageType.POSTMORTEM,
            op_id=op_id,
            seq=seq,
            causal_parent_seq=causal_parent,
            payload=payload,
        )
        await self._emit(msg)
