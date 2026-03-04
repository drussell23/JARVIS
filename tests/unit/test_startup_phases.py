"""Tests for Memory Control Plane startup phase transitions."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.memory_types import (
    BudgetPriority, StartupPhase,
    KernelPressure, PressureTier, ThrashState, SignalQuality,
    PressureTrend, MemorySnapshot,
)
from backend.core.memory_budget_broker import (
    MemoryBudgetBroker, init_memory_budget_broker, get_memory_budget_broker,
)


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


class TestPhaseTransitions:
    @pytest.fixture
    def mock_quantizer(self):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        return mq

    @pytest.fixture
    def broker(self, mock_quantizer, tmp_path):
        b = MemoryBudgetBroker(quantizer=mock_quantizer, epoch=1, lease_file=tmp_path / "leases.json")
        return b

    def test_initial_phase_is_boot_critical(self, broker):
        assert broker.current_phase == StartupPhase.BOOT_CRITICAL

    def test_phase_transition_to_boot_optional(self, broker):
        broker.set_phase(StartupPhase.BOOT_OPTIONAL)
        assert broker.current_phase == StartupPhase.BOOT_OPTIONAL

    def test_phase_transition_to_runtime(self, broker):
        broker.set_phase(StartupPhase.BOOT_OPTIONAL)
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        assert broker.current_phase == StartupPhase.RUNTIME_INTERACTIVE

    def test_phase_transition_to_background(self, broker):
        broker.set_phase(StartupPhase.BACKGROUND)
        assert broker.current_phase == StartupPhase.BACKGROUND

    @pytest.mark.asyncio
    async def test_boot_critical_only_allows_boot_critical_priority(self, broker, mock_quantizer):
        """In BOOT_CRITICAL phase, only BOOT_CRITICAL priority is allowed."""
        from backend.core.memory_budget_broker import BudgetDeniedError
        with pytest.raises(BudgetDeniedError, match="not allowed"):
            await broker.request(
                "test:v1", 100_000_000,
                BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
            )

    @pytest.mark.asyncio
    async def test_boot_critical_allows_boot_critical_priority(self, broker, mock_quantizer):
        grant = await broker.request(
            "test:v1", 100_000_000,
            BudgetPriority.BOOT_CRITICAL, StartupPhase.BOOT_CRITICAL,
        )
        assert grant is not None

    @pytest.mark.asyncio
    async def test_runtime_allows_multiple_priorities(self, broker, mock_quantizer):
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        g1 = await broker.request(
            "test1:v1", 100_000_000,
            BudgetPriority.BOOT_CRITICAL, StartupPhase.RUNTIME_INTERACTIVE,
        )
        g2 = await broker.request(
            "test2:v1", 100_000_000,
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.RUNTIME_INTERACTIVE,
        )
        g3 = await broker.request(
            "test3:v1", 100_000_000,
            BudgetPriority.RUNTIME_INTERACTIVE, StartupPhase.RUNTIME_INTERACTIVE,
        )
        assert g1 is not None
        assert g2 is not None
        assert g3 is not None


class TestBrokerStatusInAPI:
    @pytest.fixture
    def broker(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        b = MemoryBudgetBroker(quantizer=mq, epoch=42, lease_file=tmp_path / "leases.json")
        b.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        return b

    def test_get_status_has_required_fields(self, broker):
        status = broker.get_status()
        assert "epoch" in status
        assert "phase" in status
        assert "committed_bytes" in status
        assert "active_leases" in status

    def test_get_status_epoch(self, broker):
        status = broker.get_status()
        assert status["epoch"] == 42

    def test_get_status_phase(self, broker):
        status = broker.get_status()
        assert status["phase"] == "RUNTIME_INTERACTIVE"


class TestSingletonInit:
    @pytest.mark.asyncio
    async def test_init_and_get(self, tmp_path):
        import backend.core.memory_budget_broker as mod
        old = mod._broker_instance
        try:
            mod._broker_instance = None
            mq = AsyncMock()
            mq.snapshot = AsyncMock(return_value=_make_snapshot())
            broker = await init_memory_budget_broker(mq, epoch=99, lease_file=tmp_path / "leases.json")
            assert get_memory_budget_broker() is broker
            assert broker._epoch == 99
        finally:
            mod._broker_instance = old

    @pytest.mark.asyncio
    async def test_init_sets_quantizer_broker_ref(self, tmp_path):
        import backend.core.memory_budget_broker as mod
        old = mod._broker_instance
        try:
            mod._broker_instance = None
            mq = AsyncMock()
            mq.snapshot = AsyncMock(return_value=_make_snapshot())
            mq.set_broker_ref = MagicMock()
            broker = await init_memory_budget_broker(mq, epoch=1, lease_file=tmp_path / "leases.json")
            mq.set_broker_ref.assert_called_once_with(broker)
        finally:
            mod._broker_instance = old
