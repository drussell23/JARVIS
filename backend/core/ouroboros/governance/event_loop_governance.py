"""
Autonomous Event-Loop Governance Substrate (Task #102, 2026-05-14).

Closes the final-mile bottleneck after Tasks #95-#101 sealed budget /
lifecycle / phase isolation / monotonic deadlines and Task #101's
diagnostic matrix falsified H7/H8/H9 (prompt size, thinking budget,
system prompt, TLS, idle-pool-staleness all clean).  The probe ran
the production-shape stream 3 times across 10 minutes of idle on
the same long-lived client and got first_event in 759–819ms every
time.

The only remaining variable between probe (works) and harness (fails)
is **concurrency / event-loop pressure**.  The harness has hundreds
of asyncio tasks active when GENERATE fires — Oracle's 29k-file
incremental scan, Advisor blast scans, 17 sensors, BG pool workers,
SSE broker — and their cumulative on-loop work starves the SDK
iterator's `async for event in stream` coroutine from getting
scheduled.

Per operator binding 2026-05-14 ("Autonomous Event-Loop Governance
Substrate — no brittle dedicated threading hacks that fracture the
async context"):

This module provides three composable primitives over existing
``asyncio`` infrastructure — no external dependencies:

  1. ``cooperative_yield_every_n_async(iterable, every_n)``: async
     generator that yields each item from the iterable AND inserts
     ``asyncio.sleep(0)`` every N items.  Drop-in for tight async
     loops over large collections.

  2. ``cooperative_yield()``: bare ``await asyncio.sleep(0)`` wrapped
     with a master-switch gate so operators can disable governance
     for byte-identical legacy behavior (rollback path).

  3. ``offload_blocking(fn, *args, **kwargs)``: wrapper over
     ``asyncio.to_thread`` that composes the master switch with
     telemetry-friendly logging.  Returns ``await fn(*args, **kwargs)``
     when governance is disabled (legacy synchronous path).

Master switch: ``JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED`` (BOOL,
default true, Category.SAFETY).  Yield cadence:
``JARVIS_EVENT_LOOP_YIELD_EVERY_N`` (INT, default 64, Category.TUNING).

These primitives are the SINGLE SOURCE OF TRUTH for cooperative
yielding + CPU offloading across the codebase.  Composes with
existing ``asyncio.to_thread`` usage in Oracle._index_file,
``loop.run_in_executor`` in Advisor blast scan, and the existing
phase-budget asyncio.wait_for layer.

NO new bounding primitive.  NO external dependencies.  NO hardcoded
cadences — every threshold env-tunable and FlagRegistry-seeded.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Callable, Iterable, TypeVar

logger = logging.getLogger("Ouroboros.EventLoopGovernance")

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Defaults — every threshold env-tunable per operator binding.
_YIELD_EVERY_N_DEFAULT = 64

T = TypeVar("T")


def event_loop_governance_enabled() -> bool:
    """Master switch — ``JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED``
    (default true).  When false, all primitives degrade to byte-
    identical legacy behavior — no asyncio.sleep(0) injections, no
    to_thread offload (caller's await fn(...) runs unchanged)."""
    _raw = os.environ.get(
        "JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", "true",
    ).strip().lower()
    return _raw in _TRUTHY


def resolve_yield_every_n() -> int:
    """Resolve ``JARVIS_EVENT_LOOP_YIELD_EVERY_N`` to a positive int.
    Invalid / non-positive values fall back to default (64).  Lower
    values yield more frequently (less throughput, more responsive
    event loop); higher values amortize yield cost over more work."""
    try:
        _raw = int(
            os.environ.get(
                "JARVIS_EVENT_LOOP_YIELD_EVERY_N",
                str(_YIELD_EVERY_N_DEFAULT),
            )
        )
    except (TypeError, ValueError):
        return _YIELD_EVERY_N_DEFAULT
    if _raw < 1:
        return _YIELD_EVERY_N_DEFAULT
    return _raw


async def cooperative_yield() -> None:
    """Yield control to the event loop ONCE — composes
    ``asyncio.sleep(0)`` (the canonical asyncio cooperative-yield
    primitive).  No-op when governance is disabled.

    Use from inside a tight async loop to give other coroutines
    (notably the Claude SDK stream consumer) a scheduling slot.
    """
    if event_loop_governance_enabled():
        await asyncio.sleep(0)


async def cooperative_yield_every_n_async(
    iterable: Iterable[T],
    *,
    every_n: int | None = None,
) -> AsyncIterator[T]:
    """Async generator that yields each item from ``iterable`` and
    inserts an ``asyncio.sleep(0)`` cooperative-yield after every
    ``every_n`` items (default ``JARVIS_EVENT_LOOP_YIELD_EVERY_N``,
    typically 64).

    Drop-in usage::

        # Before:
        for file_path in python_files:
            await process(file_path)

        # After:
        async for file_path in cooperative_yield_every_n_async(python_files):
            await process(file_path)

    When governance is disabled, behaves like a thin async wrapper
    over the iterable — no yield injection, no behavioral difference.

    The yield cadence is calibrated so a 29k-file scan triggers
    ~450 cooperative yields — enough to give the Claude SDK stream
    consumer dozens of scheduling slots per second, while amortizing
    yield overhead across 64-item batches.
    """
    if every_n is None:
        every_n = resolve_yield_every_n()
    every_n = max(1, every_n)
    enabled = event_loop_governance_enabled()
    counter = 0
    for item in iterable:
        yield item
        counter += 1
        if enabled and counter % every_n == 0:
            await asyncio.sleep(0)


async def offload_blocking(
    fn: Callable[..., T],
    *args: Any,
    label: str | None = None,
    **kwargs: Any,
) -> T:
    """Run a synchronous, potentially-blocking function in a worker
    thread via ``asyncio.to_thread`` — frees the event loop for
    higher-priority coroutines (notably the Claude SDK stream
    consumer).

    When governance is disabled, falls back to direct synchronous
    invocation in the caller's coroutine (legacy behavior).

    Args:
        fn: synchronous callable to offload
        *args, **kwargs: forwarded to fn
        label: optional human-readable label for logging

    Composes ``asyncio.to_thread`` — the canonical asyncio primitive
    for blocking work.  No new threading mechanism.
    """
    if not event_loop_governance_enabled():
        # Legacy path — caller assumed sync execution.
        return fn(*args, **kwargs)
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception:
        if label:
            logger.debug(
                "[EventLoopGovernance] offload_blocking(%s) raised",
                label, exc_info=True,
            )
        raise


__all__ = [
    "cooperative_yield",
    "cooperative_yield_every_n_async",
    "event_loop_governance_enabled",
    "offload_blocking",
    "resolve_yield_every_n",
]
