"""Tests for broker pressure observer and lease amendment extensions."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.memory_types import (
    PressureTier, BudgetPriority, StartupPhase, LeaseState,
    MemoryBudgetEventType,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker


def _make_quantizer(tier=PressureTier.ABUNDANT):
    q = MagicMock()
    snap = MagicMock()
    snap.pressure_tier = tier
    snap.headroom_bytes = 8_000_000_000
    snap.available_budget_bytes = 10_000_000_000
    snap.safety_floor_bytes = 2_000_000_000
    snap.physical_total = 16_000_000_000
    snap.swap_hysteresis_active = False
    snap.thrash_state = MagicMock(value="healthy")
    snap.signal_quality = MagicMock(value="good")
    snap.snapshot_id = "snap_test"
    snap.max_age_ms = 5000
    snap.timestamp = 0
    snap.committed_bytes = 0
    q.snapshot = AsyncMock(return_value=snap)
    q.get_committed_bytes = MagicMock(return_value=0)
    return q, snap


class TestPressureObserver:
    @pytest.mark.asyncio
    async def test_register_and_notify(self):
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        received = []
        async def observer(tier, snapshot):
            received.append((tier, snapshot))
        broker.register_pressure_observer(observer)
        await broker.notify_pressure_observers(PressureTier.CRITICAL, snap)
        assert len(received) == 1
        assert received[0][0] == PressureTier.CRITICAL

    @pytest.mark.asyncio
    async def test_multiple_observers(self):
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        calls = {"a": 0, "b": 0}
        async def obs_a(tier, snapshot): calls["a"] += 1
        async def obs_b(tier, snapshot): calls["b"] += 1
        broker.register_pressure_observer(obs_a)
        broker.register_pressure_observer(obs_b)
        await broker.notify_pressure_observers(PressureTier.EMERGENCY, snap)
        assert calls["a"] == 1
        assert calls["b"] == 1

    @pytest.mark.asyncio
    async def test_observer_exception_does_not_break_others(self):
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        called = []
        async def bad_obs(tier, snapshot): raise RuntimeError("boom")
        async def good_obs(tier, snapshot): called.append(True)
        broker.register_pressure_observer(bad_obs)
        broker.register_pressure_observer(good_obs)
        await broker.notify_pressure_observers(PressureTier.CRITICAL, snap)
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_unregister_observer(self):
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        called = []
        async def obs(tier, snapshot): called.append(True)
        broker.register_pressure_observer(obs)
        broker.unregister_pressure_observer(obs)
        await broker.notify_pressure_observers(PressureTier.CRITICAL, snap)
        assert len(called) == 0


class TestAmendLeaseBytes:
    @pytest.mark.asyncio
    async def test_amend_active_lease(self):
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=32_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        await grant.commit(actual_bytes=32_000_000)
        old_committed = broker.get_committed_bytes()
        await broker.amend_lease_bytes(grant.lease_id, 14_000_000)
        assert broker.get_committed_bytes() == old_committed - 32_000_000 + 14_000_000

    @pytest.mark.asyncio
    async def test_amend_preserves_lease_state(self):
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=32_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        await grant.commit(actual_bytes=32_000_000)
        await broker.amend_lease_bytes(grant.lease_id, 14_000_000)
        amended = broker._leases[grant.lease_id]
        assert amended.state == LeaseState.ACTIVE

    @pytest.mark.asyncio
    async def test_amend_released_lease_raises(self):
        """Released leases are removed from _leases by _remove_lease(),
        so amend_lease_bytes raises KeyError (same as unknown lease)."""
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=32_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        await grant.commit(actual_bytes=32_000_000)
        await grant.release()
        with pytest.raises(KeyError, match="Unknown lease"):
            await broker.amend_lease_bytes(grant.lease_id, 14_000_000)

    @pytest.mark.asyncio
    async def test_amend_non_active_granted_lease_works(self):
        """Amending a GRANTED (non-terminal, non-active) lease should
        still succeed since it is not terminal."""
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=32_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        # Don't commit -- lease is still GRANTED
        await broker.amend_lease_bytes(grant.lease_id, 14_000_000)
        assert grant.granted_bytes == 14_000_000

    @pytest.mark.asyncio
    async def test_amend_unknown_lease_raises(self):
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        with pytest.raises(KeyError, match="Unknown lease"):
            await broker.amend_lease_bytes("nonexistent_lease", 14_000_000)

    @pytest.mark.asyncio
    async def test_amend_emits_event(self):
        q, snap = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=32_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        await grant.commit(actual_bytes=32_000_000)
        await broker.amend_lease_bytes(grant.lease_id, 14_000_000)
        amend_events = [e for e in broker._event_log
                        if e["type"] == MemoryBudgetEventType.GRANT_DEGRADED.value]
        assert len(amend_events) == 1
        assert amend_events[0]["old_bytes"] == 32_000_000
        assert amend_events[0]["new_bytes"] == 14_000_000
