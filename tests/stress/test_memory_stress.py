"""Stress tests for the Memory Control Plane.

These tests validate broker behavior under concurrent load,
rapid grant/release cycles, and edge cases.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock
from backend.core.memory_types import (
    BudgetPriority, StartupPhase, LeaseState,
    KernelPressure, PressureTier, ThrashState, SignalQuality,
    PressureTrend, MemorySnapshot, ConfigProof,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker, BudgetDeniedError


def _make_snapshot(**overrides):
    defaults = dict(
        physical_total=17_179_869_184, physical_wired=3_000_000_000,
        physical_active=5_000_000_000, physical_inactive=2_000_000_000,
        physical_compressed=1_000_000_000, physical_free=6_000_000_000,
        swap_total=8_000_000_000, swap_used=500_000_000,
        swap_growth_rate_bps=0.0, usable_bytes=13_000_000_000,
        committed_bytes=0, available_budget_bytes=13_000_000_000,
        kernel_pressure=KernelPressure.NORMAL, pressure_tier=PressureTier.ABUNDANT,
        thrash_state=ThrashState.HEALTHY, pageins_per_sec=0.0,
        host_rss_slope_bps=0.0, jarvis_tree_rss_slope_bps=0.0,
        swap_slope_bps=0.0, pressure_trend=PressureTrend.STABLE,
        safety_floor_bytes=1_600_000_000, compressed_trend_bytes=500_000_000,
        signal_quality=SignalQuality.GOOD, timestamp=1000.0, max_age_ms=0,
        epoch=1, snapshot_id="test-001",
    )
    defaults.update(overrides)
    return MemorySnapshot(**defaults)


class TestRapidGrantRelease:
    """Test rapid grant/commit/release cycles."""

    @pytest.mark.asyncio
    async def test_100_rapid_grants(self, tmp_path):
        """Broker handles 100 rapid grant-commit-release cycles."""
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        for i in range(100):
            grant = await broker.request(
                f"stress-test:{i}@v1", 10_000_000,
                BudgetPriority.RUNTIME_INTERACTIVE,
                StartupPhase.RUNTIME_INTERACTIVE,
            )
            await grant.commit(10_000_000, ConfigProof(
                f"stress-test:{i}@v1", {}, {}, True, "stress test",
            ))
            await grant.release()

        assert broker.get_committed_bytes() == 0
        assert len(broker.get_active_leases()) == 0

    @pytest.mark.asyncio
    async def test_concurrent_limit_enforcement(self, tmp_path):
        """Broker enforces concurrent grant limits under load."""
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        # RUNTIME_INTERACTIVE allows max 3 concurrent
        grants = []
        for i in range(3):
            grant = await broker.request(
                f"concurrent:{i}@v1", 100_000_000,
                BudgetPriority.RUNTIME_INTERACTIVE,
                StartupPhase.RUNTIME_INTERACTIVE,
            )
            grants.append(grant)

        # 4th should be denied
        with pytest.raises(BudgetDeniedError, match="Concurrent grant limit"):
            await broker.request(
                "concurrent:3@v1", 100_000_000,
                BudgetPriority.RUNTIME_INTERACTIVE,
                StartupPhase.RUNTIME_INTERACTIVE,
            )

        # Release one, then 4th should succeed
        await grants[0].rollback("make room")
        grant = await broker.request(
            "concurrent:3@v1", 100_000_000,
            BudgetPriority.RUNTIME_INTERACTIVE,
            StartupPhase.RUNTIME_INTERACTIVE,
        )
        assert grant is not None

    @pytest.mark.asyncio
    async def test_committed_bytes_never_negative(self, tmp_path):
        """committed_bytes should never go negative after rapid operations."""
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        for i in range(50):
            grant = await broker.request(
                f"test:{i}@v1", 50_000_000,
                BudgetPriority.RUNTIME_INTERACTIVE,
                StartupPhase.RUNTIME_INTERACTIVE,
            )
            if i % 2 == 0:
                await grant.commit(50_000_000, ConfigProof(
                    f"test:{i}@v1", {}, {}, True, "ok",
                ))
                await grant.release()
            else:
                await grant.rollback("alternate")

            assert broker.get_committed_bytes() >= 0


class TestPhaseTransitionUnderLoad:
    """Test phase transitions while grants are active."""

    @pytest.mark.asyncio
    async def test_phase_change_with_active_grants(self, tmp_path):
        """Phase change should work even with active grants."""
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.BOOT_OPTIONAL)

        grant = await broker.request(
            "test:v1", 100_000_000,
            BudgetPriority.BOOT_OPTIONAL,
            StartupPhase.BOOT_OPTIONAL,
        )

        # Transition while grant is active
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        assert broker.current_phase == StartupPhase.RUNTIME_INTERACTIVE

        # Grant should still be usable
        await grant.commit(100_000_000, ConfigProof("test:v1", {}, {}, True, "ok"))
        assert grant.state == LeaseState.ACTIVE


class TestEpochFencing:
    """Test epoch validation prevents stale grant operations."""

    @pytest.mark.asyncio
    async def test_stale_epoch_commit_rejected(self, tmp_path):
        """Commits from a prior epoch should be rejected."""
        from backend.core.memory_budget_broker import StaleEpochError

        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        grant = await broker.request(
            "test:v1", 100_000_000,
            BudgetPriority.RUNTIME_INTERACTIVE,
            StartupPhase.RUNTIME_INTERACTIVE,
        )

        # Simulate epoch advancement (new supervisor run)
        broker._epoch = 2

        with pytest.raises(StaleEpochError):
            await grant.commit(100_000_000)

    @pytest.mark.asyncio
    async def test_stale_epoch_heartbeat_rejected(self, tmp_path):
        from backend.core.memory_budget_broker import StaleEpochError

        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        grant = await broker.request(
            "test:v1", 100_000_000,
            BudgetPriority.RUNTIME_INTERACTIVE,
            StartupPhase.RUNTIME_INTERACTIVE,
        )

        broker._epoch = 2

        with pytest.raises(StaleEpochError):
            await grant.heartbeat()


class TestIdempotency:
    """Test that all operations are idempotent on terminal states."""

    @pytest.mark.asyncio
    async def test_double_commit_idempotent(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        grant = await broker.request(
            "test:v1", 100_000_000,
            BudgetPriority.RUNTIME_INTERACTIVE,
            StartupPhase.RUNTIME_INTERACTIVE,
        )
        await grant.commit(100_000_000)
        await grant.commit(100_000_000)  # Should not raise
        assert grant.state == LeaseState.ACTIVE

    @pytest.mark.asyncio
    async def test_double_rollback_idempotent(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        grant = await broker.request(
            "test:v1", 100_000_000,
            BudgetPriority.RUNTIME_INTERACTIVE,
            StartupPhase.RUNTIME_INTERACTIVE,
        )
        await grant.rollback("first")
        await grant.rollback("second")  # Should not raise
        assert grant.state == LeaseState.ROLLED_BACK

    @pytest.mark.asyncio
    async def test_double_release_idempotent(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        grant = await broker.request(
            "test:v1", 100_000_000,
            BudgetPriority.RUNTIME_INTERACTIVE,
            StartupPhase.RUNTIME_INTERACTIVE,
        )
        await grant.commit(100_000_000)
        await grant.release()
        await grant.release()  # Should not raise
        assert grant.state == LeaseState.RELEASED

    @pytest.mark.asyncio
    async def test_operations_on_released_are_noop(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        grant = await broker.request(
            "test:v1", 100_000_000,
            BudgetPriority.RUNTIME_INTERACTIVE,
            StartupPhase.RUNTIME_INTERACTIVE,
        )
        await grant.commit(100_000_000)
        await grant.release()

        # All operations should be no-ops
        await grant.heartbeat()
        await grant.commit(200_000_000)
        await grant.rollback("too late")
        assert grant.state == LeaseState.RELEASED
