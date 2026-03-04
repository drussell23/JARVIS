"""Tests for voice model loader integration with MemoryBudgetBroker."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.memory_types import (
    BudgetPriority, StartupPhase, LeaseState, ConfigProof, LoadResult,
    KernelPressure, PressureTier, ThrashState, SignalQuality,
    PressureTrend, MemorySnapshot, DegradationOption,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker, BudgetDeniedError
from backend.core.budgeted_loaders import WhisperBudgetedLoader, EcapaBudgetedLoader


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


class TestWhisperLoadWithGrant:
    @pytest.fixture
    def loader(self):
        return WhisperBudgetedLoader(model_size="base")

    @pytest.fixture
    def mock_broker(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        return broker

    @pytest.mark.asyncio
    async def test_load_whisper_with_grant_success(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        mock_whisper = MagicMock()
        mock_whisper.load_model = MagicMock()

        with patch("backend.core.budgeted_loaders.WhisperBudgetedLoader._load_whisper_model", return_value=mock_whisper):
            result = await loader.load_with_grant(grant)

        assert result.success is True
        assert result.model_handle is mock_whisper

    @pytest.mark.asyncio
    async def test_load_whisper_with_grant_failure(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        with patch("backend.core.budgeted_loaders.WhisperBudgetedLoader._load_whisper_model", side_effect=RuntimeError("Whisper init failed")):
            result = await loader.load_with_grant(grant)

        assert result.success is False
        assert "Whisper init failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_whisper_stores_model_handle(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        mock_model = MagicMock()
        with patch("backend.core.budgeted_loaders.WhisperBudgetedLoader._load_whisper_model", return_value=mock_model):
            await loader.load_with_grant(grant)

        assert loader._model_handle is mock_model
        await loader.release_handle("done")
        assert loader._model_handle is None

    @pytest.mark.asyncio
    async def test_whisper_degradation_changes_model_size(self, loader, mock_broker):
        """When degraded to tiny, should use tiny model."""
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )
        grant.degradation_applied = DegradationOption(
            name="whisper_tiny",
            bytes_required=loader.estimate_bytes({"model_size": "tiny"}),
            quality_impact=0.5,
            constraints={"model_size": "tiny"},
        )

        mock_model = MagicMock()
        with patch("backend.core.budgeted_loaders.WhisperBudgetedLoader._load_whisper_model", return_value=mock_model) as mock_load:
            result = await loader.load_with_grant(grant)

        assert result.success is True
        # The method should have been called with "tiny" model size
        mock_load.assert_called_once_with("tiny")


class TestEcapaLoadWithGrant:
    @pytest.fixture
    def loader(self):
        return EcapaBudgetedLoader()

    @pytest.fixture
    def mock_broker(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        return broker

    @pytest.mark.asyncio
    async def test_load_ecapa_with_grant_success(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        mock_model = MagicMock()
        with patch("backend.core.budgeted_loaders.EcapaBudgetedLoader._load_ecapa_model", return_value=mock_model):
            result = await loader.load_with_grant(grant)

        assert result.success is True
        assert result.model_handle is mock_model

    @pytest.mark.asyncio
    async def test_load_ecapa_with_grant_failure(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        with patch("backend.core.budgeted_loaders.EcapaBudgetedLoader._load_ecapa_model", side_effect=RuntimeError("ECAPA load failed")):
            result = await loader.load_with_grant(grant)

        assert result.success is False
        assert "ECAPA load failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_ecapa_config_proof(self, loader, mock_broker):
        grant = await mock_broker.request(
            loader.component_id, loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        mock_model = MagicMock()
        with patch("backend.core.budgeted_loaders.EcapaBudgetedLoader._load_ecapa_model", return_value=mock_model):
            result = await loader.load_with_grant(grant)

        assert result.config_proof is not None
        assert result.config_proof.compliant is True


class TestVoiceBrokerLifecycle:
    @pytest.mark.asyncio
    async def test_sequential_voice_grants(self, tmp_path):
        """Whisper and ECAPA should be able to get grants sequentially."""
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        whisper = WhisperBudgetedLoader("base")
        ecapa = EcapaBudgetedLoader()

        # Both should get grants
        w_grant = await broker.request(
            whisper.component_id, whisper.estimate_bytes({}),
            whisper.priority, whisper.phase,
        )
        e_grant = await broker.request(
            ecapa.component_id, ecapa.estimate_bytes({}),
            ecapa.priority, ecapa.phase,
        )

        assert w_grant.state == LeaseState.GRANTED
        assert e_grant.state == LeaseState.GRANTED
        assert broker.get_committed_bytes() == whisper.estimate_bytes({}) + ecapa.estimate_bytes({})

    @pytest.mark.asyncio
    async def test_voice_grants_rollback_on_failure(self, tmp_path):
        """If voice load fails, the grant should be rolled back."""
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        whisper = WhisperBudgetedLoader("base")
        grant = await broker.request(
            whisper.component_id, whisper.estimate_bytes({}),
            whisper.priority, whisper.phase,
        )

        # Load fails
        with patch("backend.core.budgeted_loaders.WhisperBudgetedLoader._load_whisper_model", side_effect=RuntimeError("fail")):
            result = await whisper.load_with_grant(grant)

        assert result.success is False
        await grant.rollback("load failed")
        assert broker.get_committed_bytes() == 0
