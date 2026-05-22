"""Priority-aware asyncio semaphore — Slice 12F.

Drop-in replacement for ``asyncio.Semaphore`` that orders waiters by
priority instead of FIFO. Built specifically to solve the wedge
observed in Phase 3A acceptance (``bt-2026-05-22-184422``):

    Fallback sem acquire: slots_free=1/3 ... route=immediate ...
    sem_wait_total_s=142.2  (urgency=high SWE-Bench-Pro op)

A foreground SWE-Bench-Pro envelope with ``urgency=high`` and
``route=IMMEDIATE`` waited 142.2 s behind low-priority background
OpportunityMiner ops on the existing FIFO semaphore. By the time
Claude was finally reachable, the op's wall budget was exhausted
and the request immediately ruptured. That's a starvation pattern
the FIFO contract cannot fix — operator binding (Slice 12F-A):
upgrade the gate.

## Design

  * **Drop-in shape** — exposes the same ``async with sem:`` /
    ``acquire()`` / ``release()`` surface the existing
    ``asyncio.Semaphore`` provided, so consumers that don't care
    about priority keep working byte-equivalent.
  * **Priority API** — ``acquire_for(priority)`` async context
    manager. Lower priority value = higher dispatch precedence.
  * **Stable ordering** — within the same priority bucket, waiters
    fire FIFO (monotonic ``_seq`` counter breaks ties), so
    same-class ops can't reorder amongst themselves.
  * **Cancel-safe** — a cancelled waiter is silently dropped from
    the heap so the next ``release()`` doesn't try to wake a
    dead future.
  * **Counter accurate** — slots transfer directly from
    ``release()`` to the highest-priority waiter (counter stays
    at 0 during transfer), so observers like
    ``_fallback_sem._value`` see real-time slot occupancy.
  * **Pure stdlib** — ``heapq`` + ``asyncio``. No new
    dependencies. Compatible with Python 3.9+ per project
    convention (no ``asyncio.timeout``).

## Priority mapping (from ProviderRoute, derived deterministically)

The canonical priority numbers below match the operator-bound
ordering. Lower = preempts higher.

    IMMEDIATE      → 0  (critical fast reflex — voice / test fail /
                         runtime health critical / SWE-Bench-Pro
                         foreground capability probes)
    INFORMATIONAL  → 1  (interactive Q&A; user is waiting)
    STANDARD       → 2  (default cascade)
    COMPLEX        → 3  (Claude reasoning + DW execution; OK to wait)
    BACKGROUND     → 4  (OpportunityMiner / doc staleness — cheap pool)
    SPECULATIVE    → 5  (pre-compute for idle time)

Unknown / unset route falls into ``DEFAULT_PRIORITY`` (the
``STANDARD`` bucket) so misconfigured callers don't accidentally
preempt foreground traffic.

## Discipline

  * NEVER raises into the caller from the release path (a
    cancellation race must not leak into other waiters).
  * The slot-transfer protocol on ``release()`` is the single
    invariant: when waiters exist, decrement count + wake head
    of heap atomically; when no waiters, increment count.
  * Cancellation cleanup is bounded — at most one ``heapify``
    pass per cancellation, no scanning costs paid by the hot
    path.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Tuple


logger = logging.getLogger("Ouroboros.PrioritySemaphore")


# ============================================================================
# Closed priority map — derived deterministically from ProviderRoute
# ============================================================================
#
# These integers are the structural priority of each route. They
# are NOT env-tunable on purpose — the operator binding pinned the
# IMMEDIATE→BACKGROUND ordering as a structural contract. Operators
# can disable Slice 12F via ``JARVIS_PRIORITY_SEM_ENABLED=false``,
# which falls back to the legacy FIFO ``asyncio.Semaphore`` shape.


DEFAULT_PRIORITY: int = 2  # corresponds to STANDARD route


_ROUTE_PRIORITY_MAP: dict = {
    "immediate":     0,
    "informational": 1,
    "standard":      2,
    "complex":       3,
    "background":    4,
    "speculative":   5,
}


def priority_for_route(route: object) -> int:
    """Resolve a ProviderRoute (or its string value) into a
    structural priority integer. Falls through to
    ``DEFAULT_PRIORITY`` (STANDARD bucket) for unknown values.
    NEVER raises."""
    try:
        raw = (
            route.value if hasattr(route, "value") else route
        )
        key = str(raw or "").strip().lower()
    except Exception:  # noqa: BLE001 — defensive
        return DEFAULT_PRIORITY
    return _ROUTE_PRIORITY_MAP.get(key, DEFAULT_PRIORITY)


# ============================================================================
# PrioritySemaphore
# ============================================================================


class PrioritySemaphore:
    """Priority-aware asyncio semaphore.

    Behaves like ``asyncio.Semaphore`` when no priority is supplied
    (default ``STANDARD`` bucket). When callers use
    ``acquire_for(priority=...)`` or the ``acquire_for_route(...)``
    helper, the lowest priority value wins on slot release —
    foreground (IMMEDIATE) ops preempt waiting BACKGROUND ops
    without violating the hard concurrency cap.
    """

    __slots__ = ("_value", "_initial", "_waiters", "_seq", "_name")

    def __init__(
        self,
        value: int = 1,
        *,
        name: str = "fallback_sem",
    ) -> None:
        if value < 0:
            raise ValueError("PrioritySemaphore value must be >= 0")
        self._value: int = int(value)
        self._initial: int = int(value)
        # Heap of (priority, seq, future). Lower priority pops first;
        # within same priority, lower seq pops first (FIFO tiebreak).
        self._waiters: List[Tuple[int, int, asyncio.Future]] = []
        self._seq: int = 0
        self._name: str = name

    # ---- introspection (mirrors asyncio.Semaphore.locked / _value)

    @property
    def _value_compat(self) -> int:
        """Legacy callers read ``sem._value`` to compute slots-free.
        Preserve that shape exactly."""
        return self._value

    def locked(self) -> bool:
        """Mirrors ``asyncio.Semaphore.locked``: True iff no slots
        are free (a new acquire would wait)."""
        return self._value <= 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def waiter_count(self) -> int:
        """Number of currently-waiting acquirers."""
        return len(self._waiters)

    # ---- the core acquire / release pair ---------------------------

    async def acquire(self, priority: int = DEFAULT_PRIORITY) -> None:
        """Acquire a slot, possibly waiting. Lower ``priority``
        value preempts higher-value waiters on slot release.

        Raises ``asyncio.CancelledError`` if cancelled while
        waiting. The waiter is silently removed from the heap so
        the next release doesn't try to wake a dead future."""
        if self._value > 0:
            # Fast path — slot available, no need to queue.
            self._value -= 1
            return
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        seq = self._seq
        self._seq += 1
        heapq.heappush(self._waiters, (int(priority), seq, fut))
        try:
            await fut
        except asyncio.CancelledError:
            self._drop_waiter(fut)
            raise
        # When release() set our future, the slot was transferred
        # directly to us — counter stays at 0 (no double-decrement).

    def release(self) -> None:
        """Release a slot. When waiters exist, slot transfers
        directly to the highest-priority waiter (counter stays at
        0). When no waiters, counter increments. NEVER raises."""
        while self._waiters:
            try:
                _priority, _seq, fut = heapq.heappop(self._waiters)
            except IndexError:
                break
            if fut.done():
                # Cancellation race — this waiter went away before
                # we got to it. Drop + try the next one.
                continue
            try:
                fut.set_result(None)
                return  # slot transferred; counter stays at 0
            except (asyncio.InvalidStateError, Exception):  # noqa: BLE001
                # Future raced to "done" between done() check and
                # set_result. Drop + try next waiter.
                continue
        # No waiter to hand the slot to — increment counter.
        self._value = min(self._value + 1, self._initial)

    def _drop_waiter(self, fut: asyncio.Future) -> None:
        """Remove a cancelled waiter's tuple from the heap.
        Bounded one-pass scan + heapify."""
        try:
            self._waiters = [
                w for w in self._waiters if w[2] is not fut
            ]
            heapq.heapify(self._waiters)
        except Exception:  # noqa: BLE001 — never raise on cleanup
            pass

    # ---- context-manager surface (legacy FIFO shape) ---------------

    async def __aenter__(self) -> "PrioritySemaphore":
        """Legacy ``async with sem:`` path — uses
        ``DEFAULT_PRIORITY`` (STANDARD bucket). Callers that
        care about priority should use ``acquire_for`` instead."""
        await self.acquire(DEFAULT_PRIORITY)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    # ---- priority-aware acquisition --------------------------------

    @asynccontextmanager
    async def acquire_for(self, priority: int):
        """Priority-aware context manager. Lower priority value
        preempts higher-value waiters on slot release.

            async with sem.acquire_for(priority=0) as gate:
                # urgent / IMMEDIATE work runs here
                ...
        """
        await self.acquire(priority)
        try:
            yield self
        finally:
            self.release()

    @asynccontextmanager
    async def acquire_for_route(self, route: object):
        """Convenience wrapper: derive priority from a
        ``ProviderRoute`` instance (or its string ``value``).
        Unknown routes fall through to ``DEFAULT_PRIORITY``."""
        priority = priority_for_route(route)
        async with self.acquire_for(priority):
            yield self


# ============================================================================
# Env knob — hot-revert path
# ============================================================================
#
# Default TRUE per Slice 12F: the priority gate is on by default
# because the FIFO shape demonstrably starved foreground SWE-Bench-Pro
# traffic. Explicit ``=false`` returns to the legacy FIFO behaviour
# (the PrioritySemaphore still works, but defaults to DEFAULT_PRIORITY
# for every acquire, which is FIFO-equivalent).


def priority_sem_enabled() -> bool:
    """``JARVIS_PRIORITY_SEM_ENABLED`` — default TRUE.
    NEVER raises."""
    import os
    raw = os.environ.get(
        "JARVIS_PRIORITY_SEM_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw not in ("0", "false", "no", "off")


# ============================================================================
# Drop-in helper for call sites — handles both shapes uniformly
# ============================================================================


def acquire_priority_aware(sem: object, route: object):
    """Return an async context manager that acquires ``sem``
    priority-aware when the underlying semaphore exposes the
    ``acquire_for_route`` method (PrioritySemaphore), and falls
    through to the legacy ``async with sem`` shape when it does
    not (stdlib ``asyncio.Semaphore`` under the hot-revert path).

    Lets call sites stay clean:

        async with acquire_priority_aware(self._fallback_sem, route):
            ...

    No branching at the call site; no behavior change when the
    Slice 12F master flag is off."""
    method = getattr(sem, "acquire_for_route", None)
    if method is not None:
        return method(route)
    # Legacy stdlib asyncio.Semaphore — context manager IS the
    # semaphore itself.
    return sem


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "DEFAULT_PRIORITY",
    "PrioritySemaphore",
    "priority_for_route",
    "priority_sem_enabled",
]
