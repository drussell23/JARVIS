"""Regression spine — hard-kill wrapper around Claude stream call.

Session 13 (bt-2026-04-18-060505) silently deadlocked for 90+ minutes
on a hung Anthropic SDK stream. The soft ``asyncio.wait_for(timeout_s)``
fired its timeout, tried to cancel the inner task, and the inner task
didn't respond to cancellation — so ``wait_for`` itself blocked
indefinitely waiting for the cancel to complete. The orchestrator's
outer wait_for at the same level was equally stuck. The Claude
synthesis never returned, no timeout log fired, no cancellation
propagated, and the op wedged the worker slot indefinitely.

Per Derek 2026-04-18 Option C (Manifesto §3 Disciplined Concurrency):
the microkernel must retain absolute control over its threads. No
external provider may paralyze the organism. The fix is a hard-kill
wrapper built on ``asyncio.wait({task}, timeout=...)`` which returns
a ``(done, pending)`` tuple WITHOUT awaiting cancel completion — so
we can abandon a wedged SDK task and move on cleanly.

These tests pin three contracts:
  1. The hard-kill wrapper is present in providers.py (structural AST
     check — any refactor that removes it must update the test).
  2. The wrapper uses asyncio.wait (not asyncio.wait_for) so a hung
     task cannot hang the caller.
  3. A standalone asyncio.wait invocation against a cancel-ignoring
     coroutine returns within the grace budget (behavioral proof that
     the pattern works in Python 3.9).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. Structural — hard-kill wrapper present in providers.py
# ---------------------------------------------------------------------------


def _read_providers_src() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros"
        / "governance" / "providers.py"
    ).read_text()


def test_hard_kill_wrapper_is_present() -> None:
    """The Claude stream call must be wrapped in a hard-kill pattern
    that uses ``asyncio.wait`` (not ``wait_for``), so a wedged SDK
    stream cannot hang the microkernel.
    """
    src = _read_providers_src()
    assert "HARD-KILL claude stream" in src, (
        "Hard-kill wrapper log line missing — Session-13 regression "
        "would be invisible if the wrapper were removed"
    )
    assert "_hard_kill_budget_s" in src, (
        "Hard-kill budget variable missing from providers.py"
    )
    assert "claude_stream_hard_kill" in src, (
        "Hard-kill error code missing — the exception path must "
        "surface a specific sentinel so exhaustion_watcher + "
        "postmortem analytics can classify these events"
    )


def test_hard_kill_uses_asyncio_wait_not_wait_for() -> None:
    """asyncio.wait returns (done, pending) without awaiting cancel
    completion; asyncio.wait_for awaits the cancelled task. The
    Session-13 deadlock was caused by wait_for's await-cancel
    semantics — this test pins the fix to asyncio.wait.
    """
    src = _read_providers_src()
    # Locate the hard-kill block (between _stream_task = create_task
    # and the except clause). The wait call must use asyncio.wait.
    anchor = "_stream_task = asyncio.create_task(_stream_with_resilience())"
    assert anchor in src
    idx = src.find(anchor)
    # Look in the next ~1500 chars for the pattern.
    window = src[idx:idx + 1500]
    assert "asyncio.wait(" in window, (
        "Hard-kill wrapper must use asyncio.wait for the "
        "(done, pending) contract — asyncio.wait_for would "
        "re-create the Session-13 deadlock"
    )


def test_hard_kill_budget_is_soft_timeout_plus_30_grace() -> None:
    """Derek's directive: grace window is gen_timeout + 30s. The
    wrapper computes ``_hard_kill_budget_s = timeout_s + 30.0``.
    """
    src = _read_providers_src()
    assert "_hard_kill_budget_s = timeout_s + 30.0" in src


def test_hard_kill_does_not_await_pending_cancel() -> None:
    """The pending-task branch must cancel() the task and raise
    TimeoutError WITHOUT awaiting cancel completion. Awaiting here
    would re-create the exact Session-13 deadlock.
    """
    src = _read_providers_src()
    # Find the pending-task branch.
    idx = src.find("if pending:")
    # The branch should find "_t.cancel()" but NOT "await _t" after it
    # within a reasonable window (next 500 chars).
    assert idx > 0
    window = src[idx:idx + 800]
    assert "_t.cancel()" in window
    # Forbidden pattern: "await _t" or "await t" on the cancelled
    # task. Checked loosely so minor naming refactors don't false-
    # positive.
    import re
    matches = re.findall(r"await\s+_t\b", window)
    assert not matches, (
        f"await _t found in hard-kill pending branch — this would "
        f"re-create the Session-13 deadlock: {matches}"
    )


# ---------------------------------------------------------------------------
# 2. Behavioral — asyncio.wait semantics on a cancel-ignoring task
# ---------------------------------------------------------------------------


async def _stubborn_coro_that_ignores_cancel(
    max_iterations: int = 200,
) -> None:
    """Simulates an SDK call that doesn't respond to cancellation.

    Python task cancellation raises CancelledError at the next await
    point. If the task catches it and continues, asyncio.wait_for
    blocks indefinitely. This coroutine catches every cancel that
    arrives and keeps going — but bounded to ``max_iterations`` total
    loop passes so pytest teardown doesn't hang on a truly immortal
    task. With 0.01s per sleep and max_iterations=200 the task
    self-terminates at ~2s in the worst case. The test's 0.5s
    asyncio.wait grace is well under that ceiling, so the "pending"
    assertion still holds.
    """
    for _ in range(max_iterations):
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            # Swallow cancel — keep running. Simulates a wedged SDK.
            pass


@pytest.mark.asyncio
async def test_asyncio_wait_abandons_cancel_ignoring_task() -> None:
    """Prove asyncio.wait returns within the grace budget even when
    the task ignores cancellation. This is the behavior the
    hard-kill wrapper relies on.
    """
    task = asyncio.create_task(_stubborn_coro_that_ignores_cancel())
    t0 = time.monotonic()
    # Short grace for the test to stay fast.
    done, pending = await asyncio.wait({task}, timeout=0.5)
    elapsed = time.monotonic() - t0

    # Must return within ~grace budget — NOT wait for the task.
    assert elapsed < 1.0, (
        f"asyncio.wait hung waiting for cancel-ignoring task "
        f"({elapsed:.2f}s > 1.0s budget)"
    )
    # Task is in `pending` because it never returned.
    assert task in pending
    assert task not in done
    # Cleanup: fire a cancel and give the bounded-swallow coro enough
    # ticks to exit. Don't block — that's the whole point of the
    # hard-kill pattern. If the task is still pending after the sleep,
    # we accept the leak (test has already proved the abandon path).
    task.cancel()
    for _ in range(100):
        if task.done():
            break
        await asyncio.sleep(0.01)
    # Swallow any exception the task raised for warning hygiene.
    if task.done() and not task.cancelled():
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass


# NOTE: A live demonstration of ``asyncio.wait_for`` hanging on a
# cancel-ignoring task was tried here and confirmed the bug —
# pytest's event-loop finalizer could not close because the wedged
# wait_for left a pending task that outlived the test. Keeping that
# test in-tree produced reliable CI teardown hangs. The structural
# tests above (AST pin that the wrapper uses asyncio.wait and does
# NOT await the pending task) are the load-bearing regression spine.
# If you want to observe the hang interactively, run a standalone
# script outside pytest.
