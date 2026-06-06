"""Slice 112 — Process-Isolated Oracle IPC: fault-tolerance matrix.

Deterministic, no real 1.1 GB subprocess: we inject a ``spawn_fn`` that returns
a real ``multiprocessing`` Pipe whose CHILD end the test drives directly to
simulate the worker — sending ``ready``, answering requests, and simulating a
crash by closing the pipe. This proves the AsyncOracleProxy's protocol +
graceful degradation + crash→respawn logic with zero heavyweight dependency.
"""

from __future__ import annotations

import asyncio
import multiprocessing

import pytest

from backend.core.ouroboros import oracle_ipc as OIPC
from backend.core.ouroboros.oracle_ipc import (
    AsyncOracleProxy,
    OracleNotReady,
    OracleRemoteError,
    OracleCrash,
    process_isolation_enabled,
)


class _FakeProc:
    def __init__(self):
        self._alive = True
    def is_alive(self):
        return self._alive
    def terminate(self):
        self._alive = False
    def join(self, timeout=None):
        pass


class _PipeFactory:
    """Hands out real spawn-context duplex pipes; keeps the CHILD ends so the
    test can drive the 'worker' side. Tracks spawn count for respawn assertions."""

    def __init__(self):
        self.ctx = multiprocessing.get_context("spawn")
        self.children = []
        self.procs = []
        self.spawn_count = 0

    def spawn(self):
        self.spawn_count += 1
        parent, child = self.ctx.Pipe(duplex=True)
        self.children.append(child)
        proc = _FakeProc()
        self.procs.append(proc)
        return parent, proc

    @property
    def child(self):
        return self.children[-1]


async def _wait_until(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def _recv_child(child, timeout=3.0):
    """Read one message from the child end (off the loop so we don't block)."""
    return await asyncio.wait_for(asyncio.to_thread(child.recv), timeout=timeout)


# ===========================================================================
# Master flag
# ===========================================================================


def test_master_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED", raising=False)
    assert process_isolation_enabled() is False
    monkeypatch.setenv("JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED", "1")
    assert process_isolation_enabled() is True


# ===========================================================================
# Graceful degradation — OracleNotReady while hydrating / init-failed
# ===========================================================================


@pytest.mark.asyncio
async def test_not_ready_returns_structured_sentinel_while_hydrating():
    f = _PipeFactory()
    proxy = AsyncOracleProxy(spawn_fn=f.spawn)
    await proxy.start()
    try:
        # Worker has NOT signaled ready → every call degrades gracefully.
        out = await proxy.get_metrics()
        assert isinstance(out, OracleNotReady)
        assert out.reason == "hydrating"
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_init_failed_surfaces_as_not_ready():
    f = _PipeFactory()
    proxy = AsyncOracleProxy(spawn_fn=f.spawn)
    await proxy.start()
    try:
        f.child.send({"control": "init_failed", "error": "boom"})
        assert await _wait_until(lambda: proxy._init_failed)
        out = await proxy.get_metrics()
        assert isinstance(out, OracleNotReady) and out.reason == "init_failed"
    finally:
        await proxy.shutdown()


# ===========================================================================
# Happy path — ready → request/response roundtrip
# ===========================================================================


@pytest.mark.asyncio
async def test_ready_then_call_roundtrip():
    f = _PipeFactory()
    proxy = AsyncOracleProxy(spawn_fn=f.spawn)
    await proxy.start()
    try:
        f.child.send({"control": "ready"})
        assert await _wait_until(lambda: proxy.is_ready)

        call = asyncio.ensure_future(proxy.get_metrics())
        req = await _recv_child(f.child)
        assert req["method"] == "get_metrics"
        f.child.send({"id": req["id"], "ok": True, "result": {"total_nodes": 42}})
        result = await asyncio.wait_for(call, timeout=3)
        assert result == {"total_nodes": 42}
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_remote_method_error_raises_oracleremoteerror():
    f = _PipeFactory()
    proxy = AsyncOracleProxy(spawn_fn=f.spawn)
    await proxy.start()
    try:
        f.child.send({"control": "ready"})
        assert await _wait_until(lambda: proxy.is_ready)
        call = asyncio.ensure_future(proxy.get_context_for_improvement("x"))
        req = await _recv_child(f.child)
        f.child.send({"id": req["id"], "ok": False, "error": "KeyError('x')"})
        with pytest.raises(OracleRemoteError):
            await asyncio.wait_for(call, timeout=3)
    finally:
        await proxy.shutdown()


# ===========================================================================
# Resilience — crash detection → narrate → respawn (the marquee invariant)
# ===========================================================================


@pytest.mark.asyncio
async def test_crash_is_detected_narrated_and_respawned():
    narrated = []

    class _Narrator:
        async def on_event(self, kind, payload):
            narrated.append((kind, payload))

    f = _PipeFactory()
    proxy = AsyncOracleProxy(spawn_fn=f.spawn, narrator=_Narrator(),
                             max_respawns=2, respawn_backoff_s=0.0)
    await proxy.start()
    try:
        f.child.send({"control": "ready"})
        assert await _wait_until(lambda: proxy.is_ready)
        assert f.spawn_count == 1

        # Simulate an OOM kill: the worker process dies → its pipe end closes.
        f.children[0].close()

        # Proxy must: detect crash → narrate OracleCrash → respawn (spawn #2).
        assert await _wait_until(lambda: f.spawn_count == 2, timeout=4), "no respawn"
        assert any(k == OracleCrash.KIND for k, _ in narrated), "crash not narrated"
        # After respawn, not-ready again until the NEW worker signals ready.
        assert proxy.is_ready is False
        out = await proxy.get_metrics()
        assert isinstance(out, OracleNotReady)
        # New worker hydrates → ready again.
        f.children[1].send({"control": "ready"})
        assert await _wait_until(lambda: proxy.is_ready)
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_respawn_budget_exhausts_to_degraded_not_crash():
    f = _PipeFactory()
    proxy = AsyncOracleProxy(spawn_fn=f.spawn, max_respawns=1, respawn_backoff_s=0.0)
    await proxy.start()
    try:
        f.child.send({"control": "ready"})
        assert await _wait_until(lambda: proxy.is_ready)
        # Crash #1 → respawn (budget=1 used).
        f.children[0].close()
        assert await _wait_until(lambda: f.spawn_count == 2, timeout=4)
        # Crash #2 → budget exhausted → stays DEGRADED (no spawn #3), no raise.
        f.children[1].close()
        await asyncio.sleep(0.3)
        assert f.spawn_count == 2  # no further respawn
        out = await proxy.get_metrics()
        assert isinstance(out, OracleNotReady)  # engine still runs, degraded
    finally:
        await proxy.shutdown()
