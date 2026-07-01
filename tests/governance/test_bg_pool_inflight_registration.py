"""BackgroundAgentPool ops must be visible to the in-flight registry (suspend gap 6b).

Live evidence (bt-iso-1782942507): SIGTERM's ``capture_inflight()`` wrote 0 checkpoints
despite 3 in-flight pool ops -- registration only exists on the direct
``GovernedLoopService.submit()`` path (line ~3176), but the soak's autonomous ops all
execute via ``BackgroundAgentPool._worker_loop -> orchestrator.run()`` which never
registers. The registry was empty, and capture_inflight's zero was SILENT.

Proves: (1) a pool worker registers its op in the in-flight registry for the duration
of orchestrator.run() and unregisters after; (2) capture_inflight() checkpoints a
mid-run pool op; (3) a zero-capture logs loudly instead of silently returning 0.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, cast

import pytest

import backend.core.ouroboros.governance.fsm_checkpoint as ckpt
from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
)
from backend.core.ouroboros.governance.in_flight_registry import (
    get_default_registry,
    reset_default_registry,
)

_MASTER_FLAG = "JARVIS_IN_FLIGHT_REGISTRY_ENABLED"


@dataclass
class _FakeOpContext:
    op_id: str
    goal: str = "fake"
    provider_route: str = "background"
    description: str = "pool op under suspend test"
    target_files: List[str] = field(default_factory=lambda: ["a.py"])


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.started: List[str] = []
        self.completed: List[str] = []
        self.hold_event: Optional[asyncio.Event] = None

    async def run(self, ctx: _FakeOpContext) -> Any:
        self.started.append(ctx.op_id)
        if self.hold_event is not None:
            await self.hold_event.wait()
        self.completed.append(ctx.op_id)
        return ctx


async def _wait_until(pred, timeout_s: float = 2.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


@pytest.fixture
def armed_registry(monkeypatch):
    monkeypatch.setenv(_MASTER_FLAG, "true")
    reset_default_registry()
    yield
    reset_default_registry()


def _registry_op_ids() -> List[str]:
    return [str(getattr(r, "op_id", "")) for r in get_default_registry().snapshot()]


class TestPoolInFlightRegistration:
    async def test_worker_registers_op_while_running(self, armed_registry):
        orch = _FakeOrchestrator()
        orch.hold_event = asyncio.Event()
        pool = BackgroundAgentPool(orchestrator=cast(Any, orch), pool_size=1, queue_size=4)
        await pool.start()
        try:
            await pool.submit(cast(Any, _FakeOpContext(op_id="op-suspend-6b")))
            assert await _wait_until(lambda: orch.started)
            assert "op-suspend-6b" in _registry_op_ids()
        finally:
            orch.hold_event.set()
            await pool.stop()

    async def test_capture_inflight_checkpoints_running_pool_op(self, armed_registry, tmp_path):
        orch = _FakeOrchestrator()
        orch.hold_event = asyncio.Event()
        pool = BackgroundAgentPool(orchestrator=cast(Any, orch), pool_size=1, queue_size=4)
        await pool.start()
        try:
            await pool.submit(cast(Any, _FakeOpContext(op_id="op-suspend-ckpt")))
            assert await _wait_until(lambda: orch.started)
            n = ckpt.capture_inflight(base_dir=str(tmp_path), reason="sigterm")
            assert n == 1
            assert (tmp_path / ".ouroboros" / "checkpoints" / "op-suspend-ckpt.json").exists()
        finally:
            orch.hold_event.set()
            await pool.stop()

    async def test_worker_unregisters_after_completion(self, armed_registry):
        orch = _FakeOrchestrator()
        pool = BackgroundAgentPool(orchestrator=cast(Any, orch), pool_size=1, queue_size=4)
        await pool.start()
        try:
            await pool.submit(cast(Any, _FakeOpContext(op_id="op-suspend-done")))
            assert await _wait_until(lambda: orch.completed)
            assert await _wait_until(lambda: "op-suspend-done" not in _registry_op_ids())
        finally:
            await pool.stop()


class TestCaptureInflightZeroIsLoud:
    def test_zero_capture_logs_warning(self, armed_registry, tmp_path, caplog):
        """A suspend that captures NOTHING must say so -- the live run's silent 0 cost
        a full cloud window to diagnose ([[feedback_observability_over_silent_reroute]])."""
        with caplog.at_level(logging.WARNING):
            n = ckpt.capture_inflight(base_dir=str(tmp_path), reason="sigterm")
        assert n == 0
        assert any("captured 0" in r.getMessage() for r in caplog.records)
