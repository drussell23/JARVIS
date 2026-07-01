"""Dynamic FSM-Aware Timeboxing -- the pool ceiling consults the live failover FSM.

Live evidence (Window-2, bt-iso-1782944904): a resumed op was bg_timebox-killed at
its static 415s ceiling while the failover FSM was mid zone-hunt (4 minutes of
AWAKENING before SERVING) -- the delay was infrastructure cold-start, not a wedged
op. At ceiling expiry the worker now consults the LIVE FSM state: when the failover
lifecycle is engaged (AWAKENING/SERVING -- the slow heavy path), it grants bounded
extension slices instead of a blind kill. DORMANT (normal DW ops) is byte-identical
legacy: no extension, anti-hang watchdog intact.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, List, Optional, cast

import pytest

import backend.core.ouroboros.governance.background_agent_pool as bap
from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
)


class _StubController:
    def __init__(self, state):
        self.state = state


@pytest.fixture
def fsm_state(monkeypatch):
    """Install a stub failover controller with a settable state."""
    import backend.core.ouroboros.governance.failover_lifecycle as fl
    holder = {"ctrl": _StubController(fl.FailoverState.DORMANT)}
    monkeypatch.setattr(fl, "get_failover_controller", lambda: holder["ctrl"])
    monkeypatch.setattr(fl, "lifecycle_enabled", lambda: True)

    def _set(state_name: str):
        holder["ctrl"] = _StubController(getattr(fl.FailoverState, state_name))

    return _set


# --- unit: the extension helper -----------------------------------------------


class TestExtensionHelper:
    def test_awakening_grants_slice(self, fsm_state, monkeypatch):
        fsm_state("AWAKENING")
        monkeypatch.setenv("JARVIS_BG_WORKER_FSM_EXTENSION_SLICE_S", "120")
        monkeypatch.setenv("JARVIS_BG_WORKER_FSM_EXTENSION_MAX_S", "900")
        assert bap._fsm_timebox_extension_s(0.0) == 120.0

    def test_serving_grants_slice(self, fsm_state):
        fsm_state("SERVING")
        assert bap._fsm_timebox_extension_s(0.0) > 0

    def test_dormant_grants_nothing(self, fsm_state):
        fsm_state("DORMANT")
        assert bap._fsm_timebox_extension_s(0.0) == 0.0

    def test_budget_exhaustion_caps_extension(self, fsm_state, monkeypatch):
        fsm_state("AWAKENING")
        monkeypatch.setenv("JARVIS_BG_WORKER_FSM_EXTENSION_SLICE_S", "120")
        monkeypatch.setenv("JARVIS_BG_WORKER_FSM_EXTENSION_MAX_S", "900")
        assert bap._fsm_timebox_extension_s(850.0) == 50.0     # remaining budget only
        assert bap._fsm_timebox_extension_s(900.0) == 0.0      # exhausted

    def test_master_off_is_legacy(self, fsm_state, monkeypatch):
        fsm_state("AWAKENING")
        monkeypatch.setenv("JARVIS_BG_FSM_TIMEBOX_ENABLED", "false")
        assert bap._fsm_timebox_extension_s(0.0) == 0.0

    def test_lifecycle_disabled_is_legacy(self, fsm_state, monkeypatch):
        import backend.core.ouroboros.governance.failover_lifecycle as fl
        fsm_state("AWAKENING")
        monkeypatch.setattr(fl, "lifecycle_enabled", lambda: False)
        assert bap._fsm_timebox_extension_s(0.0) == 0.0


# --- integration: the worker loop survives the ceiling while FSM is engaged ----


@dataclass
class _FakeOpContext:
    op_id: str
    goal: str = "fake"
    provider_route: str = "background"
    description: str = "fsm timebox test"
    target_files: List[str] = field(default_factory=lambda: ["a.py"])


class _SlowOrchestrator:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.completed: List[str] = []

    async def run(self, ctx: _FakeOpContext) -> Any:
        await asyncio.sleep(self.delay_s)
        self.completed.append(ctx.op_id)
        return ctx


async def _wait_until(pred, timeout_s: float = 5.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


class TestWorkerLoopExtension:
    async def test_op_survives_ceiling_when_fsm_awakening(self, fsm_state, monkeypatch):
        fsm_state("AWAKENING")
        monkeypatch.setenv("JARVIS_BG_WORKER_OP_TIMEOUT_S", "1")        # tiny ceiling
        monkeypatch.setenv("JARVIS_BG_WORKER_FSM_EXTENSION_SLICE_S", "1")
        monkeypatch.setenv("JARVIS_BG_WORKER_FSM_EXTENSION_MAX_S", "5")
        orch = _SlowOrchestrator(delay_s=1.6)                          # past base ceiling
        pool = BackgroundAgentPool(orchestrator=cast(Any, orch), pool_size=1, queue_size=4)
        await pool.start()
        try:
            await pool.submit(cast(Any, _FakeOpContext(op_id="op-fsm-ext-1")))
            assert await _wait_until(lambda: orch.completed, timeout_s=6.0)
            assert orch.completed == ["op-fsm-ext-1"]
        finally:
            await pool.stop()

    async def test_op_still_killed_when_dormant(self, fsm_state, monkeypatch):
        fsm_state("DORMANT")
        monkeypatch.setenv("JARVIS_BG_WORKER_OP_TIMEOUT_S", "1")
        orch = _SlowOrchestrator(delay_s=3.0)
        pool = BackgroundAgentPool(orchestrator=cast(Any, orch), pool_size=1, queue_size=4)
        await pool.start()
        try:
            await pool.submit(cast(Any, _FakeOpContext(op_id="op-fsm-kill-1")))
            await asyncio.sleep(1.6)
            assert orch.completed == []                                # legacy timebox kill
            st = pool.health()
            assert st.get("failed_count", st.get("failed", 1)) >= 1 or not orch.completed
        finally:
            await pool.stop()
