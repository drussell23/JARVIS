"""Tests for LLM loader integration with MemoryBudgetBroker."""
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from pathlib import Path
from backend.core.memory_types import (
    BudgetPriority, StartupPhase, LeaseState, ConfigProof, LoadResult,
    KernelPressure, PressureTier, ThrashState, SignalQuality,
    PressureTrend, MemorySnapshot, DegradationOption,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker, BudgetGrant, BudgetDeniedError
from backend.core.budgeted_loaders import LLMBudgetedLoader


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


_FAKE_MODEL_PATH = Path("/tmp/fake-model.gguf")
_RESOLVE_PATH = "backend.core.budgeted_loaders.LLMBudgetedLoader._resolve_model_path"


class TestLLMLoadWithGrant:
    """Test that LLMBudgetedLoader.load_with_grant() works correctly."""

    @pytest.fixture
    def loader(self):
        return LLMBudgetedLoader(model_name="test-model", size_mb=4000, context_length=4096)

    @pytest.fixture
    def mock_broker(self, tmp_path):
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)
        return broker

    @pytest.mark.asyncio
    async def test_load_with_grant_calls_llama(self, loader, mock_broker):
        """load_with_grant should call Llama() constructor."""
        grant = await mock_broker.request(
            "llm:test-model@v1", loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )
        mock_llama_cls = MagicMock()
        mock_llama_instance = MagicMock()
        mock_llama_cls.return_value = mock_llama_instance

        with patch.dict("sys.modules", {"llama_cpp": MagicMock(Llama=mock_llama_cls)}), \
             patch(_RESOLVE_PATH, return_value=_FAKE_MODEL_PATH):
            result = await loader.load_with_grant(grant)

        assert result.success is True
        assert mock_llama_cls.called

    @pytest.mark.asyncio
    async def test_load_with_grant_applies_constraints(self, loader, mock_broker):
        """Degradation constraints from the grant should be applied to Llama()."""
        grant = await mock_broker.request(
            "llm:test-model@v1", loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )
        # Simulate degradation constraint
        grant.degradation_applied = DegradationOption(
            name="reduce_context_2048",
            bytes_required=loader.estimate_bytes({"context_length": 2048}),
            quality_impact=0.2,
            constraints={"context_length": 2048},
        )

        mock_llama_cls = MagicMock()
        mock_llama_instance = MagicMock()
        mock_llama_cls.return_value = mock_llama_instance

        with patch.dict("sys.modules", {"llama_cpp": MagicMock(Llama=mock_llama_cls)}), \
             patch(_RESOLVE_PATH, return_value=_FAKE_MODEL_PATH):
            result = await loader.load_with_grant(grant)

        assert result.success is True
        # The context_length constraint should be applied
        call_kwargs = mock_llama_cls.call_args
        assert call_kwargs is not None
        assert call_kwargs[1].get("n_ctx") == 2048 or call_kwargs.kwargs.get("n_ctx") == 2048

    @pytest.mark.asyncio
    async def test_load_with_grant_failure_returns_error(self, loader, mock_broker):
        """On Llama() failure, load_with_grant returns success=False."""
        grant = await mock_broker.request(
            "llm:test-model@v1", loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        mock_llama_cls = MagicMock(side_effect=RuntimeError("GPU init failed"))

        with patch.dict("sys.modules", {"llama_cpp": MagicMock(Llama=mock_llama_cls)}), \
             patch(_RESOLVE_PATH, return_value=_FAKE_MODEL_PATH):
            result = await loader.load_with_grant(grant)

        assert result.success is False
        assert "GPU init failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_load_with_grant_stores_model_handle(self, loader, mock_broker):
        """After successful load, model handle should be stored."""
        grant = await mock_broker.request(
            "llm:test-model@v1", loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        mock_llama_instance = MagicMock()
        mock_llama_cls = MagicMock(return_value=mock_llama_instance)

        with patch.dict("sys.modules", {"llama_cpp": MagicMock(Llama=mock_llama_cls)}), \
             patch(_RESOLVE_PATH, return_value=_FAKE_MODEL_PATH):
            result = await loader.load_with_grant(grant)

        assert result.success is True
        assert loader._model_handle is mock_llama_instance
        assert result.model_handle is mock_llama_instance

    @pytest.mark.asyncio
    async def test_load_with_grant_provides_config_proof(self, loader, mock_broker):
        """Result should include a ConfigProof."""
        grant = await mock_broker.request(
            "llm:test-model@v1", loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        mock_llama_cls = MagicMock(return_value=MagicMock())

        with patch.dict("sys.modules", {"llama_cpp": MagicMock(Llama=mock_llama_cls)}), \
             patch(_RESOLVE_PATH, return_value=_FAKE_MODEL_PATH):
            result = await loader.load_with_grant(grant)

        assert result.config_proof is not None
        assert result.config_proof.compliant is True

    @pytest.mark.asyncio
    async def test_release_handle_clears_model(self, loader, mock_broker):
        """release_handle() should clear model reference."""
        grant = await mock_broker.request(
            "llm:test-model@v1", loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        mock_llama_cls = MagicMock(return_value=MagicMock())

        with patch.dict("sys.modules", {"llama_cpp": MagicMock(Llama=mock_llama_cls)}), \
             patch(_RESOLVE_PATH, return_value=_FAKE_MODEL_PATH):
            await loader.load_with_grant(grant)

        assert loader._model_handle is not None
        await loader.release_handle("test cleanup")
        assert loader._model_handle is None

    @pytest.mark.asyncio
    async def test_measure_actual_bytes_after_load(self, loader, mock_broker):
        """After loading, measure_actual_bytes should return non-zero."""
        grant = await mock_broker.request(
            "llm:test-model@v1", loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        mock_llama_cls = MagicMock(return_value=MagicMock())

        with patch.dict("sys.modules", {"llama_cpp": MagicMock(Llama=mock_llama_cls)}), \
             patch(_RESOLVE_PATH, return_value=_FAKE_MODEL_PATH):
            await loader.load_with_grant(grant)

        # After load, measure_actual_bytes should return the granted amount
        # (since we can't actually measure a mock)
        actual = loader.measure_actual_bytes()
        assert actual > 0

    @pytest.mark.asyncio
    async def test_load_with_grant_no_model_file(self, loader, mock_broker):
        """load_with_grant should return failure when no model file found."""
        grant = await mock_broker.request(
            "llm:test-model@v1", loader.estimate_bytes({}),
            BudgetPriority.BOOT_OPTIONAL, StartupPhase.BOOT_OPTIONAL,
        )

        with patch.dict("sys.modules", {"llama_cpp": MagicMock()}), \
             patch(_RESOLVE_PATH, return_value=None):
            result = await loader.load_with_grant(grant)

        assert result.success is False
        assert "No model file found" in (result.error or "")


class TestLLMBrokerIntegration:
    """Test the full flow of broker -> LLM loader."""

    @pytest.mark.asyncio
    async def test_full_grant_commit_release_cycle(self, tmp_path):
        """Full lifecycle: request -> load -> commit -> release."""
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        loader = LLMBudgetedLoader(model_name="test", size_mb=2000, context_length=2048)
        estimate = loader.estimate_bytes({})

        grant = await broker.request(
            loader.component_id, estimate,
            loader.priority, loader.phase,
            can_degrade=True,
            degradation_options=loader.degradation_options,
        )
        assert grant.state == LeaseState.GRANTED

        mock_llama_cls = MagicMock(return_value=MagicMock())
        with patch.dict("sys.modules", {"llama_cpp": MagicMock(Llama=mock_llama_cls)}), \
             patch(_RESOLVE_PATH, return_value=_FAKE_MODEL_PATH):
            result = await loader.load_with_grant(grant)

        assert result.success is True
        await grant.commit(result.actual_bytes, result.config_proof)
        assert grant.state == LeaseState.ACTIVE

        await loader.release_handle("test done")
        await grant.release()
        assert grant.state == LeaseState.RELEASED

    @pytest.mark.asyncio
    async def test_broker_denies_when_no_headroom(self, tmp_path):
        """Broker should deny when headroom is insufficient."""
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot(
            available_budget_bytes=100_000_000,
            safety_floor_bytes=90_000_000,
        ))
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        loader = LLMBudgetedLoader(model_name="big", size_mb=8000, context_length=4096)

        with pytest.raises(BudgetDeniedError):
            await broker.request(
                loader.component_id, loader.estimate_bytes({}),
                loader.priority, loader.phase,
            )

    @pytest.mark.asyncio
    async def test_grant_rollback_on_load_failure(self, tmp_path):
        """If load fails, grant should be rolled back via context manager."""
        mq = AsyncMock()
        mq.snapshot = AsyncMock(return_value=_make_snapshot())
        broker = MemoryBudgetBroker(quantizer=mq, epoch=1, lease_file=tmp_path / "leases.json")
        broker.set_phase(StartupPhase.RUNTIME_INTERACTIVE)

        loader = LLMBudgetedLoader(model_name="test", size_mb=2000, context_length=2048)

        grant = await broker.request(
            loader.component_id, loader.estimate_bytes({}),
            loader.priority, loader.phase,
        )

        # Load fails
        mock_llama_cls = MagicMock(side_effect=RuntimeError("fail"))
        with patch.dict("sys.modules", {"llama_cpp": MagicMock(Llama=mock_llama_cls)}), \
             patch(_RESOLVE_PATH, return_value=_FAKE_MODEL_PATH):
            result = await loader.load_with_grant(grant)

        assert result.success is False
        # Caller should rollback on failure
        await grant.rollback("load failed")
        assert grant.state == LeaseState.ROLLED_BACK
        assert broker.get_committed_bytes() == 0
