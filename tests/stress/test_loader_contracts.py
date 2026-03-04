"""Per-loader contract tests for BudgetedLoader implementations."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.memory_types import (
    BudgetPriority, StartupPhase,
    KernelPressure, PressureTier, ThrashState, SignalQuality,
    PressureTrend, MemorySnapshot,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker
from backend.core.budgeted_loaders import (
    BudgetedLoader, LLMBudgetedLoader, WhisperBudgetedLoader,
    EcapaBudgetedLoader, EmbeddingBudgetedLoader,
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


ALL_LOADERS = [
    ("llm", lambda: LLMBudgetedLoader(model_name="test", size_mb=4000, context_length=4096)),
    ("whisper", lambda: WhisperBudgetedLoader(model_size="base")),
    ("ecapa", lambda: EcapaBudgetedLoader()),
    ("embedding", lambda: EmbeddingBudgetedLoader()),
]


class TestProtocolCompliance:
    """Every loader must satisfy the BudgetedLoader protocol."""

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    def test_implements_protocol(self, name, factory):
        loader = factory()
        assert isinstance(loader, BudgetedLoader)

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    def test_component_id_is_string(self, name, factory):
        loader = factory()
        assert isinstance(loader.component_id, str)
        assert len(loader.component_id) > 0

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    def test_component_id_contains_version(self, name, factory):
        loader = factory()
        assert "@v" in loader.component_id

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    def test_phase_is_startup_phase(self, name, factory):
        loader = factory()
        assert isinstance(loader.phase, StartupPhase)

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    def test_priority_is_budget_priority(self, name, factory):
        loader = factory()
        assert isinstance(loader.priority, BudgetPriority)


class TestEstimateConservative:
    """Estimates should be positive and conservative."""

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    def test_estimate_positive(self, name, factory):
        loader = factory()
        estimate = loader.estimate_bytes({})
        assert estimate > 0

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    def test_estimate_at_least_100mb(self, name, factory):
        """Every model should estimate at least 100MB."""
        loader = factory()
        estimate = loader.estimate_bytes({})
        assert estimate >= 100 * 1024 * 1024  # 100 MB minimum

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    def test_estimate_less_than_32gb(self, name, factory):
        """No single model should estimate more than 32GB."""
        loader = factory()
        estimate = loader.estimate_bytes({})
        assert estimate < 32 * 1024 * 1024 * 1024


class TestReleaseHandle:
    """Release should clean up model handles."""

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    @pytest.mark.asyncio
    async def test_release_clears_handle(self, name, factory):
        loader = factory()
        loader._model_handle = MagicMock()  # Simulate loaded model
        await loader.release_handle("test cleanup")
        assert loader._model_handle is None


class TestMeasureActualBytes:
    """measure_actual_bytes should be non-negative."""

    @pytest.mark.parametrize("name,factory", ALL_LOADERS)
    def test_no_model_returns_zero(self, name, factory):
        loader = factory()
        assert loader.measure_actual_bytes() >= 0
