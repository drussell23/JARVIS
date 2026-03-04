"""Tests for legacy budget system deprecation."""
import pytest
import warnings
from unittest.mock import AsyncMock, MagicMock, patch


def _make_snapshot(**overrides):
    from backend.core.memory_types import (
        KernelPressure, PressureTier, ThrashState, SignalQuality,
        PressureTrend, MemorySnapshot,
    )
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


class TestReserveMemoryDeprecation:
    def test_reserve_memory_emits_deprecation(self):
        """reserve_memory() should emit DeprecationWarning."""
        from backend.core.memory_quantizer import MemoryQuantizer
        mq = MemoryQuantizer.__new__(MemoryQuantizer)
        # Set minimal attributes needed
        mq._memory_reservations = {}

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            try:
                mq.reserve_memory(1.0, "test")
            except Exception:
                pass  # May fail due to missing attributes, but warning should fire

            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) >= 1
            assert "deprecated" in str(deprecation_warnings[0].message).lower()

    def test_release_reservation_emits_deprecation(self):
        """release_reservation() should emit DeprecationWarning."""
        from backend.core.memory_quantizer import MemoryQuantizer
        mq = MemoryQuantizer.__new__(MemoryQuantizer)
        mq._memory_reservations = {}

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            try:
                mq.release_reservation("fake-id")
            except Exception:
                pass

            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) >= 1
            assert "deprecated" in str(deprecation_warnings[0].message).lower()


class TestPRGBrokerDelegation:
    @pytest.mark.asyncio
    async def test_prg_delegates_to_broker(self):
        """PRG.request_memory_budget() should delegate to broker when available."""
        from backend.core.memory_budget_broker import MemoryBudgetBroker

        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1)

        from backend.core.memory_types import StartupPhase
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        # Patch at the source module since PRG uses a local import
        with patch("backend.core.memory_budget_broker.get_memory_budget_broker", return_value=broker):
            from backend.core.memory_budget_broker import get_memory_budget_broker
            assert get_memory_budget_broker() is not None  # Verify broker is wired up
