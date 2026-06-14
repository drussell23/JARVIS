"""Slice 249 — live human-in-the-loop steering registry.

A non-blocking, op-id-keyed guidance channel into a RUNNING tool loop — distinct
from the Slice 246 preemption path (which suspends the op). When the Sovereign
Host pushes guidance for an op that is actively exploring, the BackgroundAgent /
REPL calls :func:`inject_guidance`; the tool loop drains it at the next round
boundary via :func:`consume_guidance` and folds it into the live prompt WITHOUT
yielding the lane or suspending the operation.

Mirrors ``preemption.py``: a small thread-safe singleton registry, pure, env-
gated, NEVER raises (it sits on the round-boundary hot path). The actual prompt
fold + telemetry happen in the tool loop; this module only stores/drains/format.
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

_ENV_ENABLED = "JARVIS_LIVE_STEERING_ENABLED"

_PENDING: Dict[str, Deque[str]] = defaultdict(deque)
_LOCK = threading.Lock()


def live_steering_enabled() -> bool:
    """Master gate (default-TRUE). When OFF, guidance is never absorbed (the tool
    loop runs byte-identical to pre-249). NEVER raises."""
    try:
        return os.getenv(_ENV_ENABLED, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def inject_guidance(op_id: str, text: str) -> None:
    """Queue a human guidance payload for a running op. Absorbed at the op's next
    tool-round boundary. Non-blocking, NEVER raises. Empty op_id / text ignored."""
    if not op_id or not text:
        return
    try:
        with _LOCK:
            _PENDING[op_id].append(str(text))
    except Exception:  # noqa: BLE001
        pass


def has_guidance(op_id: str) -> bool:
    """True iff guidance is pending for this op. NEVER raises."""
    if not op_id:
        return False
    try:
        with _LOCK:
            return bool(_PENDING.get(op_id))
    except Exception:  # noqa: BLE001
        return False


def consume_guidance(op_id: str) -> Optional[str]:
    """Drain ALL pending guidance for this op (consume-once), joined newest-last.
    Returns None when nothing is pending. NEVER raises."""
    if not op_id:
        return None
    try:
        with _LOCK:
            q = _PENDING.get(op_id)
            if not q:
                return None
            items = list(q)
            q.clear()
            _PENDING.pop(op_id, None)
        return "\n".join(items)
    except Exception:  # noqa: BLE001
        return None


def format_guidance_block(text: str) -> str:
    """Render absorbed guidance as a prompt block the model treats as a live
    human instruction. ASCII-only (Iron Gate strictness)."""
    return (
        "## LIVE HUMAN GUIDANCE (injected mid-flight by the Sovereign Host)\n"
        "The operator has steered this operation in real time. Treat the "
        "following as an authoritative course-correction and apply it to the "
        "remainder of your work:\n"
        f"{text}"
    )


def reset_guidance() -> None:
    """Test isolation — clear all pending guidance."""
    try:
        with _LOCK:
            _PENDING.clear()
    except Exception:  # noqa: BLE001
        pass
