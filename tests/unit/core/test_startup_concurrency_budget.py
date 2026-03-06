"""Tests for StartupConcurrencyBudget — named-slot semaphore for heavy tasks.

Disease 10 — Startup Sequencing, Task 3.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.startup_concurrency_budget import (
    CompletedTask,
    HeavyTaskCategory,
    StartupConcurrencyBudget,
    TaskSlot,
)


# ---------------------------------------------------------------------------
# TestHeavyTaskCategory
# ---------------------------------------------------------------------------


class TestHeavyTaskCategory:
    """Verify enum members and weight semantics."""

    def test_all_categories_exist(self):
        expected = {"MODEL_LOAD", "GCP_PROVISION", "REACTOR_LAUNCH", "ML_INIT", "SUBPROCESS_SPAWN"}
        actual = {member.name for member in HeavyTaskCategory}
        assert actual == expected
        assert len(HeavyTaskCategory) == 5

    def test_categories_have_default_weight(self):
        for category in HeavyTaskCategory:
            assert category.weight >= 1, (
                f"{category.name} has weight {category.weight}, expected >= 1"
            )


# ---------------------------------------------------------------------------
# TestTaskSlotDataclass
# ---------------------------------------------------------------------------


class TestTaskSlotDataclass:
    """Verify TaskSlot is frozen and stores expected fields."""

    def test_task_slot_is_frozen(self):
        slot = TaskSlot(category=HeavyTaskCategory.MODEL_LOAD, name="test-slot")
        with pytest.raises(AttributeError):
            slot.name = "changed"  # type: ignore[misc]

    def test_task_slot_has_acquired_at(self):
        before = time.monotonic()
        slot = TaskSlot(category=HeavyTaskCategory.ML_INIT, name="ml-slot")
        after = time.monotonic()
        assert before <= slot.acquired_at <= after


# ---------------------------------------------------------------------------
# TestBudgetAcquisition
# ---------------------------------------------------------------------------


class TestBudgetAcquisition:
    """Core acquisition and concurrency-limit behaviour."""

    @pytest.fixture
    def budget(self) -> StartupConcurrencyBudget:
        return StartupConcurrencyBudget(max_concurrent=2)

    async def test_acquire_within_budget(self, budget: StartupConcurrencyBudget):
        async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, "load-llm") as slot:
            assert isinstance(slot, TaskSlot)
            assert slot.category == HeavyTaskCategory.MODEL_LOAD
            assert slot.name == "load-llm"
            assert budget.active_count == 1

        # After exit, slot is released
        assert budget.active_count == 0

    async def test_concurrent_within_limit(self, budget: StartupConcurrencyBudget):
        """Two tasks in parallel should both acquire without blocking."""
        acquired = asyncio.Event()
        both_running = asyncio.Event()

        async def task_a():
            async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, "model-a"):
                acquired.set()
                await both_running.wait()

        async def task_b():
            await acquired.wait()  # Wait until A is holding a slot
            async with budget.acquire(HeavyTaskCategory.GCP_PROVISION, "gcp-b"):
                both_running.set()
                # Both tasks are running concurrently
                assert budget.active_count == 2

        a = asyncio.create_task(task_a())
        b = asyncio.create_task(task_b())
        await asyncio.gather(a, b)

    async def test_third_task_blocks_until_slot_free(self, budget: StartupConcurrencyBudget):
        """With max=2, a third task must block until one finishes."""
        gate = asyncio.Event()
        third_acquired = asyncio.Event()
        order: list[str] = []

        async def holder(name: str):
            async with budget.acquire(HeavyTaskCategory.REACTOR_LAUNCH, name):
                order.append(f"acquired-{name}")
                await gate.wait()
                order.append(f"released-{name}")

        async def waiter():
            async with budget.acquire(HeavyTaskCategory.ML_INIT, "waiter"):
                order.append("acquired-waiter")
                third_acquired.set()

        h1 = asyncio.create_task(holder("h1"))
        h2 = asyncio.create_task(holder("h2"))
        # Give holders time to acquire
        await asyncio.sleep(0.01)

        w = asyncio.create_task(waiter())
        # Give waiter time to attempt acquisition
        await asyncio.sleep(0.01)

        # Waiter should still be blocked
        assert not third_acquired.is_set()
        assert budget.active_count == 2

        # Release holders
        gate.set()
        await asyncio.gather(h1, h2, w)

        # Waiter should have acquired after a holder released
        assert third_acquired.is_set()
        assert "acquired-waiter" in order

    async def test_active_count_tracks_slots(self, budget: StartupConcurrencyBudget):
        """active_count rises and falls with acquisitions."""
        assert budget.active_count == 0

        async with budget.acquire(HeavyTaskCategory.SUBPROCESS_SPAWN, "proc-1"):
            assert budget.active_count == 1
            async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, "model-1"):
                assert budget.active_count == 2
            assert budget.active_count == 1

        assert budget.active_count == 0


# ---------------------------------------------------------------------------
# TestBudgetTimeout
# ---------------------------------------------------------------------------


class TestBudgetTimeout:
    """Timeout behaviour when budget is exhausted."""

    async def test_acquire_timeout_raises(self):
        budget = StartupConcurrencyBudget(max_concurrent=1)

        async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, "blocker"):
            # Second acquire with a short timeout should raise
            with pytest.raises(TimeoutError):
                async with budget.acquire(
                    HeavyTaskCategory.GCP_PROVISION,
                    "timeout-victim",
                    timeout=0.05,
                ):
                    pass  # Should never reach here


# ---------------------------------------------------------------------------
# TestBudgetObservability
# ---------------------------------------------------------------------------


class TestBudgetObservability:
    """History and peak tracking."""

    async def test_history_records_completed_tasks(self):
        budget = StartupConcurrencyBudget(max_concurrent=2)

        async with budget.acquire(HeavyTaskCategory.MODEL_LOAD, "task-a"):
            await asyncio.sleep(0.01)  # Ensure non-zero duration

        async with budget.acquire(HeavyTaskCategory.GCP_PROVISION, "task-b"):
            await asyncio.sleep(0.01)

        history = budget.history
        assert len(history) == 2

        # Verify first completed task
        first = history[0]
        assert isinstance(first, CompletedTask)
        assert first.category == HeavyTaskCategory.MODEL_LOAD
        assert first.name == "task-a"
        assert first.duration_s > 0
        assert first.ended_at > first.started_at

        # Verify second completed task
        second = history[1]
        assert second.category == HeavyTaskCategory.GCP_PROVISION
        assert second.name == "task-b"

    async def test_peak_concurrent_tracked(self):
        """Running 3 parallel tasks with max=3 should record peak >= 2."""
        budget = StartupConcurrencyBudget(max_concurrent=3)
        # Use an event + counter to simulate a barrier (asyncio.Barrier is 3.11+)
        arrived = 0
        all_arrived = asyncio.Event()

        async def worker(cat: HeavyTaskCategory, name: str):
            nonlocal arrived
            async with budget.acquire(cat, name):
                arrived += 1
                if arrived >= 3:
                    all_arrived.set()
                else:
                    await all_arrived.wait()
                # All three are acquired simultaneously at this point

        await asyncio.gather(
            worker(HeavyTaskCategory.MODEL_LOAD, "w1"),
            worker(HeavyTaskCategory.GCP_PROVISION, "w2"),
            worker(HeavyTaskCategory.ML_INIT, "w3"),
        )

        assert budget.peak_concurrent >= 2

    async def test_history_is_a_copy(self):
        """Mutating the returned history list must not affect internal state."""
        budget = StartupConcurrencyBudget(max_concurrent=1)

        async with budget.acquire(HeavyTaskCategory.SUBPROCESS_SPAWN, "sp"):
            pass

        history = budget.history
        assert len(history) == 1
        history.clear()
        # Internal history should be unaffected
        assert len(budget.history) == 1
