"""A stop that is quiet and does not hold the loop.

2026-10-14 11:09, Ctrl+C: the stall sampler printed every thread's stack into
the operator's terminal -- twice -- because the kernel's final thread cleanup
ran on the event loop (9 s), and three unmanaged pools (advisor-blast,
jarvis_state_io, io_airlock_) were force-terminated one second each and still
held the process open, so it left through os._exit.
"""
from __future__ import annotations

import asyncio
import io
import sys
import threading

import pytest


# -- every long-lived pool is managed ----------------------------------------------
def test_the_shared_pool_class_is_the_managed_one():
    from backend.core.async_offload import managed_pool_class
    from backend.core.thread_manager import ManagedThreadPoolExecutor
    assert managed_pool_class() is ManagedThreadPoolExecutor


@pytest.mark.parametrize("make", [
    lambda: __import__("backend.core.ouroboros.governance.operation_advisor",
                       fromlist=["x"])._get_advisor_blast_executor(),
    lambda: __import__("backend.supervisor.cross_repo_startup_orchestrator",
                       fromlist=["x"])._get_io_thread_pool(),
    lambda: __import__("backend.core.cross_repo_state_initializer",
                       fromlist=["x"]).CrossRepoStateInitializer._create_state_write_executor(1),
], ids=["advisor-blast", "io_airlock", "jarvis_state_io"])
def test_pools_that_held_the_stop_open_are_managed(make):
    from backend.core.thread_manager import ManagedThreadPoolExecutor
    pool = make()
    assert isinstance(pool, ManagedThreadPoolExecutor)
    worker = pool.submit(threading.current_thread)
    assert worker.result(timeout=10).daemon is True


# -- the final cleanup runs off the loop, on a thread of its own ------------------
def test_the_final_cleanup_does_not_run_on_the_loop_or_its_executor(monkeypatch):
    from backend.core import thread_manager as tm
    seen = {}

    def cleanup(**kw):
        seen["thread"] = threading.current_thread().name
        seen["kw"] = kw
        return {"remaining_non_daemon": 0}

    monkeypatch.setattr(tm, "final_thread_cleanup", cleanup)

    async def go():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        beat = asyncio.create_task(heartbeat())
        stats = await tm.final_thread_cleanup_async(timeout=1.0)
        beat.cancel()
        return stats, threading.current_thread().name

    stats, loop_thread = asyncio.run(go())
    assert stats == {"remaining_non_daemon": 0}
    assert seen["thread"] == "final-thread-cleanup" != loop_thread
    assert seen["kw"]["timeout"] == 1.0


def test_a_failing_cleanup_reaches_the_caller(monkeypatch):
    from backend.core import thread_manager as tm

    def cleanup(**_):
        raise RuntimeError("boom")

    monkeypatch.setattr(tm, "final_thread_cleanup", cleanup)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(tm.final_thread_cleanup_async())


# -- the stall dump goes to its file, not the terminal ------------------------------
def _sampler(monkeypatch, tmp_path, path):
    from backend.hud import stall_sampler as ss
    monkeypatch.setattr(ss, "_dump_path", lambda: path)
    terminal = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal)
    calls = []
    from backend.core.ouroboros.battle_test import oob_diagnostics as oob

    def dump(file=None):
        calls.append("file" if file is not None else "stderr")
        (file or sys.stderr).write("Thread 0x1 (most recent call first):\n")
        return True

    monkeypatch.setattr(oob, "dump_all_threads", dump)
    sampler = ss.StallSampler()
    sampler._dumps = 1
    return sampler, calls, terminal


def test_a_stall_dump_is_written_to_its_file_only(monkeypatch, tmp_path):
    path = tmp_path / "loop-stalls.log"
    sampler, calls, terminal = _sampler(monkeypatch, tmp_path, path)
    sampler._capture(1.2)
    assert calls == ["file"]
    assert "Thread 0x1" in path.read_text(encoding="utf-8")
    assert terminal.getvalue() == ""


def test_without_a_file_the_frames_still_reach_the_terminal(monkeypatch, tmp_path):
    sampler, calls, terminal = _sampler(monkeypatch, tmp_path, None)
    sampler._capture(1.2)
    assert calls == ["stderr"]
    assert "Thread 0x1" in terminal.getvalue()
