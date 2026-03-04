"""Tests for embedding loader integration with MemoryBudgetBroker."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.memory_types import (
    BudgetPriority, StartupPhase, LeaseState,
    KernelPressure, PressureTier, ThrashState, SignalQuality,
    PressureTrend, MemorySnapshot,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker, BudgetDeniedError
from backend.core.budgeted_loaders import EmbeddingBudgetedLoader


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


class TestEmbeddingLoadWithGrant:
    @pytest.fixture
    def loader(self):
        return EmbeddingBudgetedLoader()

    @pytest.fixture
    def mock_broker(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        return broker

    @pytest.mark.asyncio
    async def test_load_embedding_success(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )
        mock_model = MagicMock()
        with patch.object(loader, "_load_embedding_model", return_value=mock_model):
            result = await loader.load_with_grant(grant)
        assert result.success is True
        assert result.model_handle is mock_model
        assert loader._model_handle is mock_model

    @pytest.mark.asyncio
    async def test_load_embedding_failure(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )
        with patch.object(loader, "_load_embedding_model", side_effect=RuntimeError("OOM")):
            result = await loader.load_with_grant(grant)
        assert result.success is False
        assert "OOM" in (result.error or "")

    @pytest.mark.asyncio
    async def test_load_embedding_config_proof(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )
        with patch.object(loader, "_load_embedding_model", return_value=MagicMock()):
            result = await loader.load_with_grant(grant)
        assert result.config_proof is not None
        assert result.config_proof.compliant is True

    @pytest.mark.asyncio
    async def test_release_handle_clears_model(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )
        with patch.object(loader, "_load_embedding_model", return_value=MagicMock()):
            await loader.load_with_grant(grant)
        assert loader._model_handle is not None
        await loader.release_handle("done")
        assert loader._model_handle is None

    @pytest.mark.asyncio
    async def test_measure_actual_bytes(self, loader, mock_broker):
        assert loader.measure_actual_bytes() == 0
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )
        with patch.object(loader, "_load_embedding_model", return_value=MagicMock()):
            await loader.load_with_grant(grant)
        assert loader.measure_actual_bytes() > 0


class TestEmbeddingBrokerLifecycle:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        loader = EmbeddingBudgetedLoader()
        grant = await broker.request(
            loader.component_id, loader.estimate_bytes({}),
            loader.priority, loader.phase,
        )

        with patch.object(loader, "_load_embedding_model", return_value=MagicMock()):
            result = await loader.load_with_grant(grant)

        assert result.success
        await grant.commit(result.actual_bytes, result.config_proof)
        assert grant.state == LeaseState.ACTIVE

        await loader.release_handle("done")
        await grant.release()
        assert grant.state == LeaseState.RELEASED
        assert broker.get_committed_bytes() == 0

    @pytest.mark.asyncio
    async def test_broker_denial(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot(
            available_budget_bytes=100_000_000,
            safety_floor_bytes=90_000_000,
        ))
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        loader = EmbeddingBudgetedLoader()
        with pytest.raises(BudgetDeniedError):
            await broker.request(
                loader.component_id, loader.estimate_bytes({}),
                loader.priority, loader.phase,
            )
