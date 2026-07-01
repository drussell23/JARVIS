"""Process-global cooperative-shutdown signal.

Bridges the harness shutdown decision (wall-clock cap / SIGTERM / Spot preemption)
to deep-in-the-stack coroutines (the streaming inference loop) WITHOUT threading a
handle through every layer. The harness sets it; the streaming loop polls it between
token chunks and yields COOPERATIVELY (raising GracefulStreamInterruption with its
buffered partial) instead of holding the event loop hostage until a blind SIGKILL.

Advisory + decoupled: this is NOT a watchdog and shares no state-ledger with the
blind wall-clock cap (Slice-47 intact) -- it is a one-way "please wind down at the
next safe boundary" flag. Thread + async safe (a plain bool set/read under a lock).
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_requested: bool = False
_reason: str = ""


def request(reason: str = "shutdown") -> None:
    """Signal a cooperative wind-down. Idempotent; the first reason sticks."""
    global _requested, _reason
    with _lock:
        if not _requested:
            _reason = str(reason or "shutdown")
        _requested = True


def is_requested() -> bool:
    with _lock:
        return _requested


def reason() -> str:
    with _lock:
        return _reason


def reset() -> None:
    """Test / re-arm hook -- clear the signal."""
    global _requested, _reason
    with _lock:
        _requested = False
        _reason = ""
