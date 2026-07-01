"""Process-global cooperative-shutdown signal.

Bridges the harness shutdown decision (wall-clock cap / SIGTERM / Spot preemption)
to deep-in-the-stack coroutines (the streaming inference loop) WITHOUT threading a
handle through every layer. The harness sets it; the streaming loop polls it between
token chunks and yields COOPERATIVELY (raising GracefulStreamInterruption with its
buffered partial) instead of holding the event loop hostage until a blind SIGKILL.

Advisory + decoupled: this is NOT a watchdog and shares no state-ledger with the
blind wall-clock cap (Slice-47 intact) -- it is a one-way "please wind down at the
next safe boundary" flag. Thread + async safe (a plain bool set/read under a lock).

Loop-safe async race (constraint 2, the Preemptive Asynchronous Race): a caller in
ANY running loop can `await wait_async()` and be woken the instant `request()` fires
-- even when `request()` is driven from a signal-handler thread with no running loop.
We do NOT cache a single loop-bound `asyncio.Event` (that object binds to the loop it
was first awaited on and raises "bound to a different event loop" the moment a second
loop -- a new `asyncio.run`, a re-ignition -- tries to await it). Instead each
`wait_async()` mints a FRESH event on ITS running loop, registers it, and `request()`
wakes every live waiter on its own loop via `call_soon_threadsafe`. This mirrors the
canonical cross-thread wakeup already used by `backend/core/async_safety.py`.
"""
from __future__ import annotations

import asyncio
import threading
from typing import List, Set, Tuple

_lock = threading.Lock()
_requested: bool = False
_reason: str = ""
# Live async waiters: (loop, event). Each wait_async() adds one on entry and removes
# it on exit; request() wakes each on its own loop. No stale global singleton.
_waiters: "Set[Tuple[asyncio.AbstractEventLoop, asyncio.Event]]" = set()


async def wait_async() -> None:
    """Await the cooperative-shutdown signal (for asyncio.wait racing). Returns
    immediately if already requested; otherwise blocks until request() fires. Loop-safe:
    the event is bound to the CURRENT running loop, never a stale one."""
    loop = asyncio.get_running_loop()
    ev = asyncio.Event()
    key = (loop, ev)
    # Register + fast-path check under the SAME lock section that request() snapshots
    # waiters under -> no lost wakeup: either we see _requested here, or request() sees
    # our event in _waiters and sets it.
    with _lock:
        if _requested:
            return
        _waiters.add(key)
    try:
        await ev.wait()
    finally:
        with _lock:
            _waiters.discard(key)


def request(reason: str = "shutdown") -> None:
    """Signal a cooperative wind-down. Idempotent; the first reason sticks. Wakes every
    live async waiter on its own loop (thread-safe: callable from a signal handler)."""
    global _requested, _reason
    with _lock:
        if not _requested:
            _reason = str(reason or "shutdown")
        _requested = True
        waiters: List[Tuple[asyncio.AbstractEventLoop, asyncio.Event]] = list(_waiters)
    for loop, ev in waiters:
        try:
            loop.call_soon_threadsafe(ev.set)
        except Exception:  # noqa: BLE001 -- best-effort wake (loop may be closing)
            pass


def is_requested() -> bool:
    with _lock:
        return _requested


def reason() -> str:
    with _lock:
        return _reason


def reset() -> None:
    """Test / re-arm hook -- clear the signal + drop any registered waiters (so the
    next run/loop starts clean, not bound to a dead loop)."""
    global _requested, _reason
    with _lock:
        _requested = False
        _reason = ""
        _waiters.clear()
