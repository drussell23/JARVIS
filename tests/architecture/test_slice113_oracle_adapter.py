"""Slice 113 — unified async Oracle adapter.

Proves both backends present ONE await-able interface, the in-process path
degrades to safe empty values while hydrating, the isolated path normalizes
OracleNotReady → {}, and the factory selects by the isolation flag.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.oracle_adapter import (
    InProcessOracleAdapter,
    IsolatedOracleAdapter,
    make_oracle_adapter,
)
from backend.core.ouroboros.oracle_ipc import OracleNotReady


class _FakeReadiness:
    def __init__(self, ready):
        self._ready = ready
    def is_ready(self, scope=None):
        return self._ready


class _FakeOracle:
    def __init__(self, ready=True):
        if ready is not None:
            self._readiness = _FakeReadiness(ready)
        self.updated = []
    def get_metrics(self):
        return {"total_nodes": 5}
    def get_context_for_improvement(self, target, max_depth=2):
        return {"ctx": target, "depth": max_depth}
    async def initialize(self):
        return True
    async def incremental_update(self, files=None):
        self.updated.append(files)
    async def shutdown(self):
        pass


class _FakeProxy:
    def __init__(self, ready=True, result=None):
        self._ready = ready
        self._result = result
        self.started = False
    async def start(self):
        self.started = True
    @property
    def is_ready(self):
        return self._ready
    async def get_metrics(self):
        return self._result if self._ready else OracleNotReady()
    async def get_context_for_improvement(self, target, max_depth=2):
        return self._result if self._ready else OracleNotReady()
    async def incremental_update(self, files=None):
        return None if self._ready else OracleNotReady()
    async def shutdown(self):
        pass


# ===========================================================================
# In-process adapter
# ===========================================================================


class TestInProcessAdapter:
    @pytest.mark.asyncio
    async def test_ready_returns_real_values(self):
        a = InProcessOracleAdapter(_FakeOracle(ready=True))
        assert (await a.get_metrics()) == {"total_nodes": 5}
        assert (await a.get_context_for_improvement("foo", max_depth=3)) == {"ctx": "foo", "depth": 3}

    @pytest.mark.asyncio
    async def test_not_ready_degrades_to_empty(self):
        a = InProcessOracleAdapter(_FakeOracle(ready=False))
        assert (await a.get_metrics()) == {}
        assert (await a.get_context_for_improvement("foo")) == {}

    @pytest.mark.asyncio
    async def test_no_readiness_primitive_assumes_ready(self):
        a = InProcessOracleAdapter(_FakeOracle(ready=None))  # no _readiness attr
        assert (await a.get_metrics()) == {"total_nodes": 5}

    @pytest.mark.asyncio
    async def test_incremental_update_only_when_ready(self):
        o = _FakeOracle(ready=True)
        a = InProcessOracleAdapter(o)
        await a.incremental_update(["x.py"])
        assert o.updated == [["x.py"]]
        o2 = _FakeOracle(ready=False)
        a2 = InProcessOracleAdapter(o2)
        await a2.incremental_update(["y.py"])
        assert o2.updated == []  # skipped while not ready

    @pytest.mark.asyncio
    async def test_deferred_start_is_nonblocking(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ORACLE_BLOCK_BOOT", raising=False)
        a = InProcessOracleAdapter(_FakeOracle(ready=True))
        await a.start()              # returns immediately (init runs as a task)
        assert a._init_task is not None
        await a.shutdown()


# ===========================================================================
# Isolated (proxy) adapter — normalizes OracleNotReady → safe values
# ===========================================================================


class TestIsolatedAdapter:
    @pytest.mark.asyncio
    async def test_ready_passthrough(self):
        a = IsolatedOracleAdapter(_FakeProxy(ready=True, result={"total_nodes": 9}))
        assert (await a.get_metrics()) == {"total_nodes": 9}

    @pytest.mark.asyncio
    async def test_not_ready_normalizes_to_empty(self):
        a = IsolatedOracleAdapter(_FakeProxy(ready=False))
        assert (await a.get_metrics()) == {}
        assert (await a.get_context_for_improvement("t")) == {}
        # incremental_update tolerates the OracleNotReady return (no raise)
        await a.incremental_update(["z.py"])


# ===========================================================================
# Factory selection by the isolation flag
# ===========================================================================


class TestTransparentDelegation:
    @pytest.mark.asyncio
    async def test_in_process_delegates_unknown_methods(self):
        class _OracleWithExtra(_FakeOracle):
            def find_nodes_in_file(self, fp):
                return ["node:" + fp]
        a = InProcessOracleAdapter(_OracleWithExtra())
        # Not in the adapter interface → transparently hits the raw Oracle.
        assert a.find_nodes_in_file("x.py") == ["node:x.py"]

    def test_isolated_raises_clear_on_non_ipc_method(self):
        a = IsolatedOracleAdapter(_FakeProxy())
        with pytest.raises(AttributeError):
            _ = a.find_nodes_in_file


class TestFactory:
    def test_flag_off_selects_in_process(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED", raising=False)
        a = make_oracle_adapter(oracle=_FakeOracle())
        assert isinstance(a, InProcessOracleAdapter)

    def test_flag_on_selects_isolated(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED", "1")
        a = make_oracle_adapter(proxy=_FakeProxy())
        assert isinstance(a, IsolatedOracleAdapter)
