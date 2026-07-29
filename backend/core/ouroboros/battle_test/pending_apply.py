"""The rejection window, made visible on the surface the operator is watching.

A NOTIFY_APPLY op announces itself and then waits — five seconds in which
`/reject` will stop it. Locally that wait is a Rich `Live` panel counting
down. Remotely it was `_notify_apply_plain_fallback`: a silent sleep that
polls the cancel flag and emits nothing.

So an attached operator saw

    ⎿  /reject 7759-86 to cancel — diff follows as ⏺ Update

and then five seconds of nothing. The window was open and the only sign of it
was a sentence that had already scrolled. A deadline nobody can see is not a
choice, it is a delay.

Why not simply mirror the panel
--------------------------------
`Live` is a screen-REWRITING widget: it repaints the same region eight times
a second. The bridge is a line-oriented stream, so forwarding it would put
forty panels a second into the deck — worse than silence, and it would bury
the very diff the panel exists to show.

The remote representation of live state on this cockpit is not a mirrored
widget, it is state carried on the heartbeat and drawn by a strip that
re-renders each frame — the shape the agent roster and the status line
already use. This is that, for a countdown.

Remaining, never a deadline
---------------------------
The snapshot carries SECONDS LEFT, computed where the clock lives.
`time.monotonic()` is a per-process origin, so shipping a deadline and
subtracting the reader's clock yields a countdown wrong by however long the
two processes have been alive — and plausibly wrong, which is worse. The
reader counts DOWN by the frame's own age between heartbeats, exactly as the
pulse counts elapsed UP.

NEVER raises. A countdown that can break a repaint is not worth having, and
the apply proceeds on its own timer regardless of whether anything drew it.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.PendingApply")

PENDING_APPLY_SCHEMA_VERSION = "pending_apply.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_PENDING_APPLY_STRIP_ENABLED"

_LOCK = threading.Lock()
#: op_id → (deadline_monotonic, reason). At most a handful are ever open.
_PENDING: Dict[str, Any] = {}


def strip_enabled() -> bool:
    """Default ON. Off, the window is invisible again — which is the state
    this module exists to end, so it is off only by explicit choice."""
    return os.environ.get(
        MASTER_FLAG_ENV_VAR, "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def note_pending(
    op_id: str, *, delay_s: float, reason: str = "",
    clock: Optional[Any] = None,
) -> None:
    """An apply is waiting out its rejection window. NEVER raises."""
    try:
        key = str(op_id or "").strip()
        if not key or float(delay_s) <= 0:
            return
        now = (clock or time.monotonic)()
        with _LOCK:
            _PENDING[key] = (now + float(delay_s), str(reason or ""))
    except Exception:  # noqa: BLE001
        pass


def clear_pending(op_id: str) -> None:
    """The window closed — applied, rejected, or failed. NEVER raises.

    Called from the SAME `finally` that ends the wait, so a rejection and a
    completion retire the strip by the identical path. A cleared-on-success
    -only design leaves a rejected op counting down forever.
    """
    try:
        with _LOCK:
            _PENDING.pop(str(op_id or "").strip(), None)
    except Exception:  # noqa: BLE001
        pass


def reset_for_tests() -> None:
    with _LOCK:
        _PENDING.clear()


def snapshot(clock: Optional[Any] = None) -> Optional[dict]:
    """Open windows as a transport-safe dict, or None when none are.

    None rather than an empty list: "no apply is pending" and "the daemon
    never told us" are different facts, and a reader that cannot tell them
    apart will draw the first when it means the second.

    Expired entries are dropped HERE, where the clock that set them lives.
    A reader deciding an op had expired would be guessing from a frame that
    is already a second old.
    """
    try:
        if not strip_enabled():
            return None
        now = (clock or time.monotonic)()
        rows: List[dict] = []
        with _LOCK:
            for op_id, (deadline, reason) in list(_PENDING.items()):
                remaining = deadline - now
                if remaining <= 0:
                    _PENDING.pop(op_id, None)
                    continue
                rows.append({
                    "op_id": op_id,
                    # SECONDS LEFT, never the deadline — see the docstring.
                    "remaining_s": round(float(remaining), 2),
                    "reason": reason,
                })
        if not rows:
            return None
        rows.sort(key=lambda r: r["remaining_s"])
        return {
            "schema_version": PENDING_APPLY_SCHEMA_VERSION,
            "rows": rows,
        }
    except Exception:  # noqa: BLE001
        logger.debug("[PendingApply] snapshot degraded", exc_info=True)
        return None


def render(
    payload: Optional[dict], *, age_s: float = 0.0, width: Optional[int] = None,
) -> List[str]:
    """Rows for the countdown strip, or [] when nothing is pending.

    ``age_s`` is how long ago the snapshot was taken; the countdown advances
    DOWN by it, so the seconds tick between 1 Hz heartbeats instead of
    stepping. The mirror of how the pulse advances elapsed UP.

    An entry whose remaining time has run out between frames renders as
    `applying…` rather than vanishing: the operator's last impression should
    be that the window closed, not that the op disappeared.
    """
    try:
        if not strip_enabled() or not isinstance(payload, dict):
            return []
        rows = [r for r in (payload.get("rows") or ()) if isinstance(r, dict)]
        if not rows:
            return []
        age = max(0.0, float(age_s or 0.0))
        cols = int(width) if width and int(width) > 0 else 80
        out: List[str] = []
        for row in rows:
            left = float(row.get("remaining_s") or 0.0) - age
            op = str(row.get("op_id") or "")[:16]
            if left <= 0.0:
                out.append(f"  ⏵ applying {op}…")
                continue
            reason = " ".join(str(row.get("reason") or "").split())
            head = f"  ⏵ {op} applies in {left:0.1f}s · /reject {op} to stop"
            room = cols - len(head) - 3
            if reason and room > 12:
                head = f"{head} · {reason[:room]}"
            out.append(head[:cols])
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[PendingApply] render degraded", exc_info=True)
        return []


__all__ = [
    "MASTER_FLAG_ENV_VAR",
    "PENDING_APPLY_SCHEMA_VERSION",
    "clear_pending",
    "note_pending",
    "render",
    "reset_for_tests",
    "snapshot",
    "strip_enabled",
]
