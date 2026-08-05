"""The Hydra: a system that spawns during teardown cannot be torn down.

`FailoverLifecycle` was observed STARTING new recovery work while shutdown was
already under way — measured 2026-08-05, in the log of a backend that then
needed a hard `os._exit`. Cancelling the tasks you know about is useless
against something that grows new ones behind you.

These pin the two halves of the fix: the token refuses late spawns, and the
registry hands every task it did create to a shutdown phase that cancels it.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.app_lifecycle import (
    LIFECYCLE, REGISTRY, ShutdownInProgress, cancel_all_managed,
    shutdown_requested, sleep_or_shutdown, spawn_managed_task, stats,
    total_budget_s,
)


@pytest.fixture(autouse=True)
def _clean():
    LIFECYCLE.reset()
    yield
    LIFECYCLE.reset()


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def test_the_token_is_readable_without_an_event_loop():
    """Signal handlers, executor threads and the parent watch all ask this
    question, and none of them has a loop. An asyncio.Event could not answer."""
    assert shutdown_requested() is False
    LIFECYCLE.request_shutdown("test")
    assert shutdown_requested() is True


def test_the_first_reason_is_the_one_kept():
    """Anything after the first is a consequence, not a cause. Overwriting
    would replace 'launcher died' with 'database closed'."""
    assert LIFECYCLE.request_shutdown("launcher died") is True
    assert LIFECYCLE.request_shutdown("database closed") is False
    assert LIFECYCLE.reason == "launcher died"


@pytest.mark.asyncio
async def test_sleep_or_shutdown_returns_early():
    """The migration primitive. A daemon parked in `asyncio.sleep(60)` cannot
    notice anything for a minute; this makes the wait a race."""
    async def _fire():
        await asyncio.sleep(0.05)
        LIFECYCLE.request_shutdown("early")

    asyncio.ensure_future(_fire())
    t0 = time.monotonic()
    hit = await sleep_or_shutdown(10.0)
    elapsed = time.monotonic() - t0

    assert hit is True, "must report that shutdown, not the delay, ended the wait"
    assert elapsed < 2.0, f"waited {elapsed:.2f}s of a 10s sleep — did not race"


@pytest.mark.asyncio
async def test_sleep_or_shutdown_sleeps_when_nothing_happens():
    """It must still be a sleep. A primitive that returns immediately would
    turn every daemon loop into a busy-wait."""
    t0 = time.monotonic()
    hit = await sleep_or_shutdown(0.3)
    assert hit is False
    assert time.monotonic() - t0 >= 0.25


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_managed_task_is_registered_and_cancellable():
    async def _daemon():
        while not await sleep_or_shutdown(60):
            pass

    task = spawn_managed_task(_daemon(), name="test-daemon")
    await asyncio.sleep(0.05)
    assert task in REGISTRY.all_tasks()

    cancelled = await cancel_all_managed(timeout=2.0)
    assert cancelled >= 1
    assert task.done()


@pytest.mark.asyncio
async def test_a_finished_task_leaves_the_registry():
    """A registry that only grows is a leak wearing the costume of a
    lifecycle manager."""
    async def _brief():
        return 1

    t = spawn_managed_task(_brief(), name="brief")
    await t
    await asyncio.sleep(0)          # let the done-callback run
    assert t not in REGISTRY.all_tasks()


@pytest.mark.asyncio
async def test_the_hydra_is_refused():
    """THE MANDATED CASE. A subsystem tries to spawn recovery work after
    shutdown has begun; the factory must refuse it."""
    LIFECYCLE.request_shutdown("phase 1 started")

    async def _recovery():
        await asyncio.sleep(300)

    coro = _recovery()
    with pytest.raises(ShutdownInProgress) as exc:
        spawn_managed_task(coro, name="failover-recovery")

    assert "failover-recovery" in str(exc.value)
    assert "phase 1 started" in str(exc.value)
    assert REGISTRY.stats()["refused_late_spawn"] >= 1


@pytest.mark.asyncio
async def test_a_refused_coroutine_is_closed_not_leaked():
    """Dropping it would surface later as a 'never awaited' warning during
    interpreter teardown — a confusing second symptom of a correct refusal."""
    LIFECYCLE.request_shutdown("teardown")

    async def _never_runs():
        raise AssertionError("the refused coroutine must never execute")

    coro = _never_runs()
    with pytest.raises(ShutdownInProgress):
        spawn_managed_task(coro, name="doomed")

    # A closed coroutine cannot be started again; awaiting it raises.
    with pytest.raises(RuntimeError):
        await coro


# ---------------------------------------------------------------------------
# The whole point: a clean, fast close under an active Hydra.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hydra_under_shutdown_closes_cleanly_in_under_a_second():
    """A mock FailoverLifecycle that respawns on every cycle — the exact
    behaviour observed in the real log — must not be able to keep the loop
    alive, and teardown must finish well inside the budget.
    """
    spawn_attempts = {"blocked": 0, "allowed": 0}

    async def _recovery_worker(depth: int):
        """A head that grows another.

        Deliberately does NOT consult the token — it sleeps on plain
        `asyncio.sleep` and keeps spawning. That is faithful to the observed
        bug: `FailoverLifecycle` knows nothing about any of this, and the
        factory has to hold the line on its behalf. A cooperative Hydra would
        stop itself and prove nothing about the guard.
        """
        while True:
            await asyncio.sleep(0.05)
            try:
                spawn_managed_task(_recovery_worker(depth + 1),
                                   name=f"hydra-{depth + 1}")
                spawn_attempts["allowed"] += 1
            except ShutdownInProgress:
                spawn_attempts["blocked"] += 1
                raise

    # Let it grow while healthy, so there is a real population to tear down.
    spawn_managed_task(_recovery_worker(0), name="hydra-0")
    await asyncio.sleep(0.3)
    assert spawn_attempts["allowed"] > 0, "the Hydra never grew; test is vacuous"
    assert len(REGISTRY.all_tasks()) > 1

    t0 = time.monotonic()
    LIFECYCLE.request_shutdown("phase 1")

    # The window that matters. Shutdown is not instantaneous — phases run,
    # hooks execute, connections drain — and it is DURING that window that the
    # observed Hydra kept spawning. Cancelling in the same breath as requesting
    # would mean the guard was never asked anything, and the test would pass
    # because nothing had time to misbehave.
    await asyncio.sleep(0.15)

    await cancel_all_managed(timeout=total_budget_s())
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, (
        f"teardown took {elapsed:.2f}s under an active Hydra — the mandate is "
        f"a clean close in under a second")
    assert not REGISTRY.all_tasks(), (
        f"{len(REGISTRY.all_tasks())} managed task(s) survived teardown")
    assert spawn_attempts["blocked"] > 0, (
        "no spawn was refused — the Hydra was never actually blocked, so this "
        "passed for the wrong reason")


@pytest.mark.asyncio
async def test_stats_expose_the_state():
    """Absolute observability: whether the ban is armed must be inspectable,
    not inferred from behaviour."""
    s = stats()
    assert s["shutdown_requested"] is False
    assert s["block_late_spawn"] is True
    assert s["total_budget_s"] > 0
