"""Tests for StartupBudgetPolicy — tiered concurrency budget enforcement.

Disease 10 — Startup Sequencing, Task 4.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.startup_budget_policy import (
    BudgetAcquisitionError,
    PreconditionNotMetError,
    StartupBudgetPolicy,
)
from backend.core.startup_concurrency_budget import HeavyTaskCategory
from backend.core.startup_config import BudgetConfig, SoftGatePrecondition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def default_config() -> BudgetConfig:
    return BudgetConfig(
        max_hard_concurrent=1,
        max_total_concurrent=3,
        max_wait_s=5.0,
        soft_gate_preconditions={
            "ML_INIT": SoftGatePrecondition(
                require_phase="CORE_READY",
                require_memory_stable_s=0.0,
            ),
        },
    )


@pytest.fixture
def policy(default_config: BudgetConfig) -> StartupBudgetPolicy:
    return StartupBudgetPolicy(default_config)


# ---------------------------------------------------------------------------
# TestHardGateEnforcement
# ---------------------------------------------------------------------------


class TestHardGateEnforcement:
    """Hard-gate categories are serialised through _hard_sem."""

    async def test_single_hard_task_acquires(self, policy: StartupBudgetPolicy):
        """A single hard-gate task acquires and releases cleanly."""
        async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "load-llm") as slot:
            assert policy.active_count == 1
            assert slot.category == HeavyTaskCategory.MODEL_LOAD
            assert slot.name == "load-llm"
        assert policy.active_count == 0

    async def test_two_hard_tasks_serialized(self, policy: StartupBudgetPolicy):
        """Two hard-gate tasks cannot run simultaneously (max_hard=1)."""
        order: list[str] = []
        gate = asyncio.Event()

        async def first():
            async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "first"):
                order.append("first-acquired")
                await gate.wait()
                order.append("first-releasing")

        async def second():
            # Give the first task time to acquire
            await asyncio.sleep(0.02)
            async with policy.acquire(HeavyTaskCategory.REACTOR_LAUNCH, "second"):
                order.append("second-acquired")

        t1 = asyncio.create_task(first())
        t2 = asyncio.create_task(second())

        # Let both tasks start
        await asyncio.sleep(0.05)

        # At this point, first holds the hard sem, second should be blocked
        assert "first-acquired" in order
        assert "second-acquired" not in order

        # Release the first task
        gate.set()
        await asyncio.gather(t1, t2)

        # Second must have acquired after first released
        assert order.index("first-releasing") < order.index("second-acquired")

    async def test_hard_and_soft_can_overlap(self, policy: StartupBudgetPolicy):
        """A hard task and a soft task can run simultaneously."""
        hard_acquired = asyncio.Event()
        soft_acquired = asyncio.Event()
        done = asyncio.Event()
        overlap_count = 0

        async def hard_task():
            nonlocal overlap_count
            async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "hard"):
                hard_acquired.set()
                await soft_acquired.wait()
                # Both tasks hold a slot right now
                overlap_count = policy.active_count
                done.set()

        async def soft_task():
            await hard_acquired.wait()
            async with policy.acquire(HeavyTaskCategory.GCP_PROVISION, "soft"):
                soft_acquired.set()
                await done.wait()

        t1 = asyncio.create_task(hard_task())
        t2 = asyncio.create_task(soft_task())
        await asyncio.gather(t1, t2)
        assert overlap_count == 2


# ---------------------------------------------------------------------------
# TestSoftGatePreconditions
# ---------------------------------------------------------------------------


class TestSoftGatePreconditions:
    """Soft-gate categories require phase preconditions."""

    async def test_ml_init_blocked_without_phase(self, policy: StartupBudgetPolicy):
        """ML_INIT raises PreconditionNotMetError when CORE_READY not reached."""
        with pytest.raises(PreconditionNotMetError, match="CORE_READY"):
            async with policy.acquire(HeavyTaskCategory.ML_INIT, "ml-task"):
                pass  # Should never reach here

    async def test_ml_init_allowed_after_phase_signal(self, policy: StartupBudgetPolicy):
        """ML_INIT succeeds after signal_phase_reached('CORE_READY')."""
        policy.signal_phase_reached("CORE_READY")
        async with policy.acquire(HeavyTaskCategory.ML_INIT, "ml-task") as slot:
            assert slot.category == HeavyTaskCategory.ML_INIT
            assert policy.active_count == 1
        assert policy.active_count == 0

    async def test_category_without_precondition_passes(self, policy: StartupBudgetPolicy):
        """Categories not in soft_gate_preconditions acquire without phase signal."""
        # GCP_PROVISION is not in default_config's soft_gate_preconditions
        async with policy.acquire(HeavyTaskCategory.GCP_PROVISION, "gcp") as slot:
            assert slot.category == HeavyTaskCategory.GCP_PROVISION
        assert policy.active_count == 0


# ---------------------------------------------------------------------------
# TestStarvationProtection
# ---------------------------------------------------------------------------


class TestStarvationProtection:
    """Starvation protection via timeout on budget acquisition."""

    async def test_max_wait_exceeded_raises(self):
        """Second task times out with BudgetAcquisitionError when budget full."""
        tight_config = BudgetConfig(
            max_hard_concurrent=1,
            max_total_concurrent=1,
            max_wait_s=0.05,
        )
        pol = StartupBudgetPolicy(tight_config)

        async with pol.acquire(HeavyTaskCategory.MODEL_LOAD, "blocker"):
            with pytest.raises(BudgetAcquisitionError):
                async with pol.acquire(
                    HeavyTaskCategory.GCP_PROVISION,
                    "starved",
                ):
                    pass  # Should never reach here


# ---------------------------------------------------------------------------
# TestLeakHardening
# ---------------------------------------------------------------------------


class TestLeakHardening:
    """Slots are released even on exceptions and cancellation."""

    async def test_slot_released_on_exception(self, policy: StartupBudgetPolicy):
        """active_count returns to 0 after exception inside acquire block."""
        with pytest.raises(ValueError, match="boom"):
            async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "exploder"):
                raise ValueError("boom")

        assert policy.active_count == 0

    async def test_slot_released_on_cancellation(self, policy: StartupBudgetPolicy):
        """active_count returns to 0 after task cancellation."""
        started = asyncio.Event()

        async def long_task():
            async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "cancellee"):
                started.set()
                await asyncio.sleep(100)  # Will be cancelled

        task = asyncio.create_task(long_task())
        await started.wait()
        assert policy.active_count == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Give a tick for cleanup
        await asyncio.sleep(0.01)
        assert policy.active_count == 0


# ---------------------------------------------------------------------------
# TestObservability
# ---------------------------------------------------------------------------


class TestObservability:
    """History recording and peak concurrent tracking."""

    async def test_history_records_completed(self, policy: StartupBudgetPolicy):
        """Completed tasks appear in history with correct name and positive duration."""
        async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "observed-task"):
            await asyncio.sleep(0.01)  # Ensure non-zero duration

        history = policy.history
        assert len(history) == 1
        entry = history[0]
        assert entry.name == "observed-task"
        assert entry.category == HeavyTaskCategory.MODEL_LOAD
        assert entry.duration_s > 0

    async def test_peak_concurrent_tracked(self, policy: StartupBudgetPolicy):
        """Running hard + soft concurrently records peak >= 2."""
        both_running = asyncio.Event()
        gate = asyncio.Event()

        async def hard_task():
            async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "h1"):
                both_running.set()
                await gate.wait()

        async def soft_task():
            await both_running.wait()
            async with policy.acquire(HeavyTaskCategory.GCP_PROVISION, "s1"):
                # Both are now acquired
                gate.set()

        await asyncio.gather(
            asyncio.create_task(hard_task()),
            asyncio.create_task(soft_task()),
        )

        assert policy.peak_concurrent >= 2

    async def test_history_is_safe_copy(self, policy: StartupBudgetPolicy):
        """Mutating the returned history list must not affect internal state."""
        async with policy.acquire(HeavyTaskCategory.MODEL_LOAD, "ephemeral"):
            pass

        history = policy.history
        assert len(history) == 1
        history.clear()
        # Internal history should be unaffected
        assert len(policy.history) == 1
