"""One loop for every IPC event, not one loop per event.

`ipc_server` created `asyncio.new_event_loop()` per event and closed it. The
note said "macOS subprocess contexts make call_soon_threadsafe unreliable" —
almost certainly a symptom of the main loop being blocked for 34s by the
synchronous invariant audit, which makes a correctly-queued callback simply
never run.

The workaround outlived its cause and broke three things, all measured in one
boot on 2026-08-03 08:47:

    <asyncio.locks.Lock object ...> is bound to a different event loop   (x3)
    Task was destroyed but it is pending!
    Loop <...closed=True> that handles pid 24496 is closed
"""
from __future__ import annotations

import asyncio
import threading

import pytest

import backend.hud.ipc_server as IPC


@pytest.fixture(autouse=True)
def _clean():
    IPC.shutdown_dispatch_loop()
    yield
    IPC.shutdown_dispatch_loop()


def _run(coro):
    loop = IPC._dispatch_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=20)


# ── The three measured failures ─────────────────────────────────────────────

def test_a_lock_survives_between_dispatches():
    """THE TTS MUTEX. It is what makes JARVIS speak with one voice, and under
    per-event loops it bound to the first dispatch and refused every one
    after — so two replies played over each other."""
    holder = {}

    async def dispatch(n):
        if "mutex" not in holder:
            holder["mutex"] = asyncio.Lock()
        async with holder["mutex"]:      # raised on the 2nd event, before
            await asyncio.sleep(0.01)
        return n

    assert [_run(dispatch(i)) for i in range(3)] == [0, 1, 2]


def test_a_background_task_survives_the_dispatch_that_started_it():
    """The fire-and-forget acknowledgement. Under per-event loops it was
    destroyed the instant `run_until_complete` returned — so the fix that took
    command latency from 15s to 37ms was silently killing its own audio."""
    done = []

    async def dispatch():
        async def later():
            await asyncio.sleep(0.2)
            done.append(True)
        asyncio.create_task(later())

    _run(dispatch())
    import time
    time.sleep(0.6)
    assert done == [True], "the background task was destroyed with its loop"


def test_every_dispatch_shares_one_loop():
    ids = {id(IPC._dispatch_loop()) for _ in range(5)}
    assert len(ids) == 1


def test_the_loop_is_not_the_callers_loop():
    """Isolation is the one thing the old design got right: a dispatch must
    not stall because the MAIN loop is busy. That is kept."""
    async def outer():
        here = id(asyncio.get_running_loop())
        there = id(IPC._dispatch_loop())
        return here, there

    here, there = asyncio.run(outer())
    assert here != there


def test_it_runs_off_the_calling_thread():
    async def dispatch():
        return threading.current_thread().name

    assert _run(dispatch()) != threading.current_thread().name


# ── Lifecycle ───────────────────────────────────────────────────────────────

def test_the_loop_is_recreated_after_shutdown():
    """A HUD that reconnects after a shutdown must not find a dead loop."""
    first = id(IPC._dispatch_loop())
    IPC.shutdown_dispatch_loop()
    second = id(IPC._dispatch_loop())
    assert second and second != first


def test_shutdown_is_idempotent():
    IPC._dispatch_loop()
    IPC.shutdown_dispatch_loop()
    IPC.shutdown_dispatch_loop()          # must not raise


def test_shutdown_without_a_loop_is_fine():
    IPC.shutdown_dispatch_loop()


def test_a_dispatch_that_raises_is_reported_not_swallowed():
    """`run_coroutine_threadsafe` returns a future nobody awaits, and an
    unretrieved exception on one of those is silent — the same black hole
    `TaskHarvester` exists to close, arriving through a different door."""
    from backend.core.ouroboros.telemetry import task_harvester as TH
    TH.reset_task_harvester()
    h = TH.get_task_harvester()
    before = h.stats()["harvested"]

    async def boom():
        raise RuntimeError("dispatch exploded")

    fut = asyncio.run_coroutine_threadsafe(boom(), IPC._dispatch_loop())
    fut.add_done_callback(IPC._report_dispatch_outcome)
    with pytest.raises(RuntimeError):
        fut.result(timeout=10)
    import time
    time.sleep(0.2)
    assert h.stats()["harvested"] > before


def test_concurrency_is_preserved():
    """Per-event loops ran events in parallel threads. One loop must still run
    them concurrently, or a slow command would serialise the socket."""
    order = []

    async def slow():
        await asyncio.sleep(0.3)
        order.append("slow")

    async def quick():
        await asyncio.sleep(0.05)
        order.append("quick")

    loop = IPC._dispatch_loop()
    a = asyncio.run_coroutine_threadsafe(slow(), loop)
    b = asyncio.run_coroutine_threadsafe(quick(), loop)
    b.result(timeout=10)
    a.result(timeout=10)
    assert order == ["quick", "slow"], "the slow event blocked the quick one"
