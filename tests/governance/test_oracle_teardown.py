"""Oracle teardown deadlock fix — bounded + escalating AST pool shutdown.

Closes the bt-2026-06-16 "Shutting down The Oracle..." wedge: an in-flight index
left ProcessPoolExecutor workers running, blocking clean exit so session_outcome
never reached "complete".
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import time
from concurrent.futures import ProcessPoolExecutor

import pytest

import backend.core.ouroboros.governance.ast_compile_helper as A


# --------------------------------------------------------------------------- fakes
class _FakeProc:
    """A worker that stays alive through join() (simulates a stuck AST parse)."""

    def __init__(self, drains: bool = False):
        self._alive = True
        self._drains = drains
        self.terminated = False
        self.killed = False

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        if self._drains:
            self._alive = False          # finishes in-flight within the deadline

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False


class _FakePool:
    def __init__(self, procs):
        self._processes = {i: p for i, p in enumerate(procs)}
        self.shutdown_args = None

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_args = (wait, cancel_futures)


# --------------------------------------------------------------------------- logic
def test_shutdown_pool_idle(monkeypatch):
    monkeypatch.setattr(A, "_pool", None)
    assert A.shutdown_pool() == "idle"


def test_shutdown_pool_graceful(monkeypatch):
    procs = [_FakeProc(drains=True), _FakeProc(drains=True)]
    fake = _FakePool(procs)
    monkeypatch.setattr(A, "_pool", fake)
    assert A.shutdown_pool(deadline_s=1.0) == "graceful"
    assert fake.shutdown_args == (False, True)     # queue dropped, non-blocking
    assert not any(p.terminated for p in procs)    # no force needed
    assert A._pool is None                          # singleton cleared


def test_shutdown_pool_escalates_on_stuck(monkeypatch):
    procs = [_FakeProc(drains=False), _FakeProc(drains=False)]
    fake = _FakePool(procs)
    monkeypatch.setattr(A, "_pool", fake)
    assert A.shutdown_pool(deadline_s=0.05) == "escalated"
    assert all(p.terminated for p in procs)        # force-terminated the stragglers
    assert A._pool is None


def test_shutdown_pool_never_raises(monkeypatch):
    class _BadPool:
        _processes = {0: _FakeProc()}
        def shutdown(self, **k):
            raise RuntimeError("nope")
    monkeypatch.setattr(A, "_pool", _BadPool())
    # must not raise even when shutdown() throws; still escalates the stuck proc
    assert A.shutdown_pool(deadline_s=0.05) == "escalated"


# --------------------------------------------------------------------------- real pool
def test_shutdown_pool_real_hung_workers_bounded(monkeypatch):
    """The load-bearing proof: a REAL pool with genuinely-hung 30s workers is torn
    down in well under 5s (escalated) — it does NOT block on the 30s sleeps."""
    pool = ProcessPoolExecutor(max_workers=2, mp_context=mp.get_context("spawn"))
    try:
        pool.submit(time.sleep, 30)
        pool.submit(time.sleep, 30)
        time.sleep(1.2)                            # let workers actually start running
        monkeypatch.setattr(A, "_pool", pool)
        t0 = time.monotonic()
        verdict = A.shutdown_pool(deadline_s=0.5)
        elapsed = time.monotonic() - t0
        assert verdict == "escalated"
        assert elapsed < 5.0, f"teardown took {elapsed:.1f}s — did NOT bound the hang"
        assert A._pool is None
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- oracle wiring
def test_oracle_shutdown_invokes_pool_teardown(monkeypatch):
    import backend.core.ouroboros.oracle as O
    oracle = O.TheOracle()

    async def _noop_save():
        return None

    monkeypatch.setattr(oracle, "_save_cache", _noop_save)
    called = {}

    def _spy(*, deadline_s):
        called["deadline"] = deadline_s
        return "graceful"

    monkeypatch.setattr(A, "shutdown_pool", _spy)
    asyncio.run(oracle.shutdown())
    assert oracle._shutting_down is True            # cancellation token set
    assert "deadline" in called                     # pool teardown invoked
    assert oracle._running is False                  # clean completion


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
