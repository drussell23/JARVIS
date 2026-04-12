"""Tests for BackgroundAgentPool pause/resume (HIBERNATION_MODE step 1).

Validates that pause() and resume():
1. Are idempotent and only transition when state changes
2. No-op on a stopped pool
3. Gate dequeuing without draining the queue
4. Do NOT interrupt in-flight operations
5. Preserve queue state across the pause window
6. Report paused state in health()
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, List, Optional, cast

import pytest  # type: ignore[import-not-found]

from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
)


# ---------------------------------------------------------------------------
# Minimal fake orchestrator + op context
# ---------------------------------------------------------------------------


@dataclass
class _FakeOpContext:
    op_id: str
    goal: str = "fake"
    provider_route: str = "background"


class _FakeOrchestrator:
    """Executes each op after a tunable delay. Tracks start/completion order."""

    def __init__(self, per_op_delay_s: float = 0.05) -> None:
        self.per_op_delay_s = per_op_delay_s
        self.started: List[str] = []
        self.completed: List[str] = []
        # Optional gate — when set, run() waits on it before completing.
        self.hold_event: Optional[asyncio.Event] = None

    async def run(self, ctx: _FakeOpContext) -> Any:
        self.started.append(ctx.op_id)
        if self.hold_event is not None:
            await self.hold_event.wait()
        else:
            await asyncio.sleep(self.per_op_delay_s)
        self.completed.append(ctx.op_id)
        return ctx


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def pool_and_orch():
    orch = _FakeOrchestrator(per_op_delay_s=0.02)
    pool = BackgroundAgentPool(orchestrator=cast(Any, orch), pool_size=2, queue_size=8)
    await pool.start()
    try:
        yield pool, orch
    finally:
        await pool.stop()


# ---------------------------------------------------------------------------
# 1. pause() / resume() basic transitions
# ---------------------------------------------------------------------------


class TestPauseResumeTransitions:
    async def test_pause_from_running_transitions_to_paused(self, pool_and_orch):
        pool, _ = pool_and_orch
        assert pool.is_paused is False
        assert pool.pause(reason="test") is True
        assert pool.is_paused is True

    async def test_pause_is_idempotent(self, pool_and_orch):
        pool, _ = pool_and_orch
        assert pool.pause() is True
        assert pool.pause() is False  # already paused
        assert pool.is_paused is True

    async def test_resume_from_paused_transitions_to_running(self, pool_and_orch):
        pool, _ = pool_and_orch
        pool.pause()
        assert pool.resume(reason="test") is True
        assert pool.is_paused is False

    async def test_resume_is_idempotent(self, pool_and_orch):
        pool, _ = pool_and_orch
        assert pool.resume() is False  # not paused
        pool.pause()
        assert pool.resume() is True
        assert pool.resume() is False  # already resumed


# ---------------------------------------------------------------------------
# 2. Stopped pool — pause/resume are no-ops
# ---------------------------------------------------------------------------


class TestPauseResumeOnStoppedPool:
    async def test_pause_on_stopped_pool_is_noop(self):
        orch = _FakeOrchestrator()
        pool = BackgroundAgentPool(orchestrator=cast(Any, orch), pool_size=1, queue_size=4)
        # Never started.
        assert pool.pause(reason="no-op") is False
        assert pool.is_paused is False

    async def test_resume_on_stopped_pool_is_noop(self):
        orch = _FakeOrchestrator()
        pool = BackgroundAgentPool(orchestrator=cast(Any, orch), pool_size=1, queue_size=4)
        assert pool.resume(reason="no-op") is False
        assert pool.is_paused is False


# ---------------------------------------------------------------------------
# 3. Dequeue gating — queued items remain queued while paused
# ---------------------------------------------------------------------------


class TestPauseBlocksDequeue:
    async def test_queued_op_not_picked_up_during_pause(self, pool_and_orch):
        pool, orch = pool_and_orch
        # Pause BEFORE submitting so workers never pick the item up.
        pool.pause(reason="block dequeue")

        ctx = _FakeOpContext(op_id="op-1")
        await pool.submit(ctx)

        # Give workers several loop iterations; they should not dequeue.
        await asyncio.sleep(0.3)

        assert orch.started == []
        assert pool.health()["queue_depth"] == 1

    async def test_resume_releases_queued_op(self, pool_and_orch):
        pool, orch = pool_and_orch
        pool.pause()
        ctx = _FakeOpContext(op_id="op-release")
        await pool.submit(ctx)
        await asyncio.sleep(0.1)
        assert orch.started == []

        pool.resume()
        # Worker should pick it up promptly.
        for _ in range(50):
            if "op-release" in orch.completed:
                break
            await asyncio.sleep(0.05)
        assert "op-release" in orch.completed


# ---------------------------------------------------------------------------
# 4. In-flight ops complete naturally during pause
# ---------------------------------------------------------------------------


class TestInFlightOpsSurvivePause:
    async def test_running_op_finishes_when_paused_mid_execution(self):
        orch = _FakeOrchestrator()
        orch.hold_event = asyncio.Event()  # block run() until explicitly released
        pool = BackgroundAgentPool(orchestrator=cast(Any, orch), pool_size=1, queue_size=4)
        await pool.start()
        try:
            op_id = await pool.submit(cast(Any, _FakeOpContext(op_id="long-op")))
            # Wait until the worker has picked it up.
            for _ in range(50):
                if "long-op" in orch.started:
                    break
                await asyncio.sleep(0.02)
            assert "long-op" in orch.started

            # Pause while op is mid-execution. It must NOT be cancelled.
            pool.pause(reason="mid-flight")
            assert pool.is_paused is True

            # Release the hold — op should complete naturally.
            orch.hold_event.set()
            for _ in range(50):
                bg = pool.get_result(op_id)
                if bg and bg.status == "completed":
                    break
                await asyncio.sleep(0.02)

            bg = pool.get_result(op_id)
            assert bg is not None
            assert bg.status == "completed"
            assert "long-op" in orch.completed
        finally:
            await pool.stop()


# ---------------------------------------------------------------------------
# 5. Queue state preserved across pause window
# ---------------------------------------------------------------------------


class TestQueuePreservedAcrossPause:
    async def test_items_submitted_during_pause_run_on_resume(self, pool_and_orch):
        pool, orch = pool_and_orch
        pool.pause(reason="state preservation")

        ids = [f"bulk-{i}" for i in range(4)]
        for op_id in ids:
            await pool.submit(_FakeOpContext(op_id=op_id))

        # No workers should have touched them.
        await asyncio.sleep(0.2)
        assert orch.started == []
        assert pool.health()["queue_depth"] == 4

        pool.resume()
        # Wait for drain.
        for _ in range(100):
            if len(orch.completed) >= 4:
                break
            await asyncio.sleep(0.05)
        assert set(orch.completed) == set(ids)


# ---------------------------------------------------------------------------
# 6. health() reports pause state
# ---------------------------------------------------------------------------


class TestHealthReporting:
    async def test_health_reflects_paused_state(self, pool_and_orch):
        pool, _ = pool_and_orch
        h1 = pool.health()
        assert h1["paused"] is False
        assert h1["pause_count"] == 0
        assert h1["paused_for_s"] is None

        pool.pause(reason="health-check")
        await asyncio.sleep(0.05)
        h2 = pool.health()
        assert h2["paused"] is True
        assert h2["pause_count"] == 1
        assert h2["paused_for_s"] is not None
        assert h2["paused_for_s"] >= 0.0

        pool.resume()
        h3 = pool.health()
        assert h3["paused"] is False
        assert h3["paused_for_s"] is None
        assert h3["pause_count"] == 1  # count tracks pauses, not resumes

    async def test_multiple_pause_cycles_increment_count(self, pool_and_orch):
        pool, _ = pool_and_orch
        for _ in range(3):
            pool.pause()
            pool.resume()
        assert pool.health()["pause_count"] == 3
