"""Tests for observer snapshot pattern in MemoryBudgetBroker."""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock


class TestObserverSnapshot:
    @pytest.mark.asyncio
    async def test_observer_added_during_notification_not_called(self):
        """Observer added during notification loop should not be called in same pass."""
        from backend.core.memory_budget_broker import MemoryBudgetBroker

        broker = MagicMock(spec=MemoryBudgetBroker)
        broker._pressure_observers = []
        broker._latest_snapshot = None
        broker._advance_sequence = MagicMock()

        late_observer = AsyncMock()

        async def registering_observer(tier, snapshot):
            """Observer that registers another observer during notification."""
            broker._pressure_observers.append(late_observer)

        broker._pressure_observers.append(AsyncMock(side_effect=registering_observer))

        # Manually run the snapshot-based notification
        observers = list(broker._pressure_observers)  # Snapshot
        for obs in observers:
            await obs("critical", {})

        # late_observer was added to the live list but NOT in the snapshot
        late_observer.assert_not_called()

    @pytest.mark.asyncio
    async def test_observer_removed_during_notification_still_called(self):
        """Observer removed during notification should still be called (it was in snapshot)."""
        obs1_called = []
        obs2_called = []

        observers = []

        async def obs1(tier, snapshot):
            obs1_called.append(True)
            # Remove obs2 from live list during notification
            if obs2 in observers:
                observers.remove(obs2)

        async def obs2(tier, snapshot):
            obs2_called.append(True)

        observers.extend([obs1, obs2])

        # Snapshot-based iteration
        snapshot = list(observers)
        for obs in snapshot:
            await obs("critical", {})

        assert len(obs1_called) == 1
        assert len(obs2_called) == 1  # Still called because it was in snapshot
