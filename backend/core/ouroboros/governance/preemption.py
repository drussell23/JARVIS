"""Slice 246 — cooperative human-override preemption registry.

A non-blocking, resumable interrupt distinct from the terminal CancelToken path
(``cancel_token.py``). When a live human intent is submitted while a resurrected
op is actively running, the BackgroundAgentPool sentinel calls
:func:`request_preemption` for that op. The op's tool loop calls
:func:`check_preemption` at each round boundary; if a preemption is pending it
raises :class:`OperationPreemptedError` — a GRACEFUL yield (no hard SIGTERM), at
a coherent round boundary. The pool worker catches it and re-ingests the op via
Slice 245's ``resubmit_resurrected`` (micro-hibernation), so the survivor
re-enters the VIP lane below the human and resumes from its last durable phase.

Kept separate from CancelToken on purpose: a CancelToken fires → terminal
POSTMORTEM. A preemption fires → resumable re-ingest. Conflating them would route
preempted survivors to terminal death. Pure, thread-safe, NEVER raises except the
intentional OperationPreemptedError.
"""
from __future__ import annotations

import os
import threading
from typing import Set

_ENV_ENABLED = "JARVIS_HUMAN_PREEMPTION_ENABLED"

_REQUESTED: Set[str] = set()
_LOCK = threading.Lock()


class OperationPreemptedError(BaseException):
    """Raised at a tool-round boundary when a human override preempts a running
    (resurrected) op. RESUMABLE — the pool re-ingests the op rather than
    terminating it. Distinct from cancel_token.OperationCancelledError.

    Inherits from BaseException (NOT Exception) — deliberately, mirroring
    asyncio.CancelledError. The tool loop is wrapped by broad ``except Exception``
    handlers in candidate_generator / orchestrator that convert failures into
    terminal GenerationResults; a preemption must slip PAST those and reach the
    BackgroundAgentPool worker's explicit ``except OperationPreemptedError`` so
    the survivor is re-ingested, not terminated. finally-blocks still run, so
    resources release cleanly."""

    def __init__(self, op_id: str) -> None:
        self.op_id = op_id
        super().__init__(f"operation preempted by human override: {op_id}")


def human_preemption_enabled() -> bool:
    """Master gate (default-TRUE). NEVER raises."""
    try:
        return os.getenv(_ENV_ENABLED, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def request_preemption(op_id: str) -> None:
    """Mark an op for graceful preemption at its next round boundary. NEVER raises."""
    if not op_id:
        return
    try:
        with _LOCK:
            _REQUESTED.add(op_id)
    except Exception:  # noqa: BLE001
        pass


def is_preemption_requested(op_id: str) -> bool:
    """True iff a preemption is pending for this op. NEVER raises."""
    if not op_id:
        return False
    try:
        with _LOCK:
            return op_id in _REQUESTED
    except Exception:  # noqa: BLE001
        return False


def clear_preemption(op_id: str) -> None:
    """Clear a pending preemption (after the op has yielded). NEVER raises."""
    try:
        with _LOCK:
            _REQUESTED.discard(op_id)
    except Exception:  # noqa: BLE001
        pass


def check_preemption(op_id: str) -> None:
    """Cooperative checkpoint — raise OperationPreemptedError iff a preemption is
    pending for this op AND the gate is on. The ONLY function here that raises
    (intentionally). Call at coherent boundaries (between tool rounds)."""
    if not human_preemption_enabled():
        return
    if is_preemption_requested(op_id):
        raise OperationPreemptedError(op_id)


def reset_preemptions() -> None:
    """Test isolation — clear all pending preemptions."""
    try:
        with _LOCK:
            _REQUESTED.clear()
    except Exception:  # noqa: BLE001
        pass
