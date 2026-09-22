"""A broken process pool is replaced, not inherited by every later call.

bt-2026-09-22-201845: 16 minutes into the soak a worker of cooperative_fs_io's
process pool died abruptly. ``concurrent.futures`` marks the whole pool BROKEN
on that, permanently, and nothing replaced it -- so for the remaining hours
every ``offload(cpu_bound=True)`` raised ``BrokenProcessPool`` (strategic
direction's prompt sections, the coverage index), which callers absorbed as
"degraded". ``worker_lifeline`` kills workers deliberately on the premise that
the pool rebuilds; this pins that it does.

Real spawned workers, really killed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

import pytest

from backend.core.ouroboros.governance import cooperative_fs_io as cfi


# Module-level: spawn workers import them by reference.
def _pid() -> int:
    return os.getpid()


def _slow_pid(seconds: float) -> int:
    time.sleep(seconds)
    return os.getpid()


def _kill_own_worker() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


@pytest.fixture(autouse=True)
def _fresh_pool(monkeypatch):
    monkeypatch.delenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", raising=False)
    cfi.shutdown_fs_process_pool()
    yield
    cfi.shutdown_fs_process_pool()


def _kill_workers(pool) -> None:
    for pid in cfi._pool_pids(pool):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


async def _until_broken(pool, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not getattr(pool, "_broken", False):
        assert time.monotonic() < deadline, "the pool never noticed its dead worker"
        await asyncio.sleep(0.05)


async def test_a_pool_broken_between_calls_heals_on_the_next(caplog):
    first = await cfi.offload(_pid, cpu_bound=True)
    assert isinstance(first, int)
    broken = cfi._FS_PROCESS_POOL
    _kill_workers(broken)
    await _until_broken(broken)

    with caplog.at_level(logging.WARNING, logger="Ouroboros.CooperativeFSIO"):
        got = await cfi.offload(_pid, cpu_bound=True)

    assert isinstance(got, int), f"the broken pool was inherited: {got!r}"
    assert cfi._FS_PROCESS_POOL is not broken, "the broken pool is still installed"
    assert "process pool BROKEN" in caplog.text


async def test_a_worker_killed_mid_task_is_retried_on_a_fresh_pool():
    await cfi.offload(_pid, cpu_bound=True)
    pool = cfi._FS_PROCESS_POOL
    task = asyncio.ensure_future(cfi.offload(_slow_pid, 2.0, cpu_bound=True))
    await asyncio.sleep(0.8)
    _kill_workers(pool)
    got = await task
    assert isinstance(got, int), f"mid-task break reached the caller: {got!r}"


async def test_a_fn_that_kills_its_own_worker_gets_two_attempts_not_a_loop():
    got = await cfi.offload(_kill_own_worker, cpu_bound=True)
    assert cfi.is_offload_error(got), "offload raised instead of returning its sentinel"
    assert got.exc_type == "BrokenProcessPool"
    after = await cfi.offload(_pid, cpu_bound=True)
    assert isinstance(after, int), "the pool stayed dead after the bounded retry"


async def test_retiring_an_old_pool_never_clears_its_replacement():
    await cfi.offload(_pid, cpu_bound=True)
    old = cfi._FS_PROCESS_POOL
    cfi._retire_pool(old)
    await cfi.offload(_pid, cpu_bound=True)
    new = cfi._FS_PROCESS_POOL
    assert new is not None and new is not old
    cfi._retire_pool(old)  # a late second retire of the same broken pool
    assert cfi._FS_PROCESS_POOL is new, "a stale retire took the fresh pool down"
