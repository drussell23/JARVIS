"""backend/core/ouroboros/governance/comms/tui_panel.py

TUI "Self-Programming" panel data provider. Tracks active ops,
pending approvals, and recent completions for the Textual TUI dashboard.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType

logger = logging.getLogger(__name__)

_MAX_RECENT = 10
_TERMINAL_TYPES = {MessageType.DECISION, MessageType.POSTMORTEM}


@dataclass
class PipelineStatus:
    """Mutable tracking state for an active operation."""

    op_id: str
    phase: str
    target_file: str
    repo: str
    trigger_source: str
    provider: Optional[str]
    started_at: float  # monotonic
    started_at_utc: datetime
    awaiting_approval: bool = False

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at


@dataclass(frozen=True)
class CompletionSummary:
    """Immutable record of a completed operation."""

    op_id: str
    target_file: str
    outcome: str
    completed_at: datetime
    duration_s: float
    provider: Optional[str] = None


@dataclass(frozen=True)
class SelfProgramPanelState:
    """Snapshot of panel state for TUI rendering."""

    active_ops: Tuple[PipelineStatus, ...]
    pending_approvals: Tuple[PipelineStatus, ...]
    recent_completions: Tuple[CompletionSummary, ...]
    intent_engine_state: str = "watching"
    ops_today: int = 0
    ops_limit: int = 20
    repos_online: Tuple[str, ...] = ()


class TUISelfProgramPanel:
    """CommProtocol transport that maintains panel state for TUI rendering."""

    def __init__(self, ops_limit: int = 20) -> None:
        self._active: Dict[str, PipelineStatus] = {}
        self._completions: deque[CompletionSummary] = deque(maxlen=_MAX_RECENT)
        self._ops_today: int = 0
        self._ops_limit = ops_limit

    async def send(self, msg: CommMessage) -> None:
        """CommProtocol transport interface."""
        try:
            if msg.msg_type == MessageType.INTENT:
                self._handle_intent(msg)
            elif msg.msg_type == MessageType.HEARTBEAT:
                self._handle_heartbeat(msg)
            elif msg.msg_type in _TERMINAL_TYPES:
                self._handle_terminal(msg)
        except Exception:
            logger.debug("TUISelfProgramPanel: error processing %s", msg.op_id)

    def get_state(self) -> SelfProgramPanelState:
        """Return current panel state snapshot."""
        active = tuple(self._active.values())
        pending = tuple(op for op in active if op.awaiting_approval)
        return SelfProgramPanelState(
            active_ops=active,
            pending_approvals=pending,
            recent_completions=tuple(self._completions),
            ops_today=self._ops_today,
            ops_limit=self._ops_limit,
        )

    def _handle_intent(self, msg: CommMessage) -> None:
        payload = msg.payload
        target_files = payload.get("target_files", [])
        target_file = target_files[0] if target_files else "unknown"
        self._active[msg.op_id] = PipelineStatus(
            op_id=msg.op_id,
            phase="intent",
            target_file=target_file,
            repo=payload.get("repo", "jarvis"),
            trigger_source=payload.get("trigger_source", "unknown"),
            provider=payload.get("provider"),
            started_at=time.monotonic(),
            started_at_utc=datetime.now(timezone.utc),
        )

    def _handle_heartbeat(self, msg: CommMessage) -> None:
        status = self._active.get(msg.op_id)
        if status is None:
            return
        status.phase = msg.payload.get("phase", status.phase)
        if msg.payload.get("phase") == "approve":
            status.awaiting_approval = True

    def _handle_terminal(self, msg: CommMessage) -> None:
        status = self._active.pop(msg.op_id, None)
        if msg.msg_type == MessageType.POSTMORTEM:
            outcome = "postmortem"
        else:
            outcome = msg.payload.get("outcome", "complete")

        duration_s = status.elapsed_s if status else 0.0
        target_file = status.target_file if status else "unknown"
        provider = status.provider if status else None

        self._completions.append(CompletionSummary(
            op_id=msg.op_id,
            target_file=target_file,
            outcome=outcome,
            completed_at=datetime.now(timezone.utc),
            duration_s=duration_s,
            provider=provider,
        ))
        self._ops_today += 1
