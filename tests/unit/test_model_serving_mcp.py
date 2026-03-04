"""Tests for unified_model_serving MCP broker integration (Task 11).

Verifies that ``PrimeLocalClient`` can register with the MCP broker,
uses ``broker.latest_snapshot`` for the model download admission gate
and for post-load memory validation, and falls back to the legacy
psutil path when the broker has no snapshot or is not active.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.memory_types import (
    MemorySnapshot,
    PressureTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_broker(epoch: int = 1) -> MagicMock:
    """Create a mock broker with attributes the client accesses."""
    broker = MagicMock()
    broker.register_pressure_observer = MagicMock()
    broker.current_epoch = epoch
    broker.current_sequence = 5
    broker.latest_snapshot = None  # default: no snapshot yet
    return broker


def _mock_snapshot(
    physical_total: int = 16 * (1024 ** 3),
    physical_free: int = 4 * (1024 ** 3),
    snapshot_id: str = "snap-test-001",
    tier: PressureTier = PressureTier.OPTIMAL,
) -> MagicMock:
    """Create a mock MemorySnapshot with physical fields."""
    snap = MagicMock(spec=MemorySnapshot)
    snap.physical_total = physical_total
    snap.physical_free = physical_free
    snap.snapshot_id = snapshot_id
    snap.pressure_tier = tier
    return snap


def _make_client():
    """Create a fresh PrimeLocalClient for testing."""
    from backend.intelligence.unified_model_serving import PrimeLocalClient
    return PrimeLocalClient()


# ---------------------------------------------------------------------------
# Default attributes
# ---------------------------------------------------------------------------

class TestPrimeLocalClientDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        client = _make_client()
        assert client._mcp_active is False

    def test_broker_default_none(self):
        client = _make_client()
        assert client._broker is None


# ---------------------------------------------------------------------------
# register_with_broker
# ---------------------------------------------------------------------------

class TestRegisterWithBroker:
    """Verify register_with_broker sets state correctly."""

    def test_sets_mcp_active_true(self):
        client = _make_client()
        broker = _mock_broker()
        client.register_with_broker(broker)
        assert client._mcp_active is True

    def test_stores_broker_reference(self):
        client = _make_client()
        broker = _mock_broker()
        client.register_with_broker(broker)
        assert client._broker is broker

    def test_idempotent_re_registration(self):
        client = _make_client()
        broker1 = _mock_broker()
        broker2 = _mock_broker()
        client.register_with_broker(broker1)
        client.register_with_broker(broker2)
        assert client._broker is broker2
        assert client._mcp_active is True


# ---------------------------------------------------------------------------
# Admission gate (background_provision_model): broker path
# ---------------------------------------------------------------------------

class TestAdmissionGateBrokerPath:
    """Verify background_provision_model uses broker snapshot for available_gb."""

    @pytest.mark.asyncio
    async def test_uses_broker_snapshot_when_active(self):
        """When broker is active with snapshot, psutil should NOT be called."""
        client = _make_client()
        broker = _mock_broker()
        free_bytes = 6 * (1024 ** 3)  # 6 GB
        broker.latest_snapshot = _mock_snapshot(physical_free=free_bytes)
        client.register_with_broker(broker)

        # Patch psutil to verify it's not called
        with patch(
            "backend.intelligence.unified_model_serving.psutil",
            create=True,
        ) as mock_psutil:
            # Also need to prevent actual model discovery / download
            with patch.object(client, "_discover_model", return_value=None):
                with patch.object(client, "_auto_download_model", new_callable=AsyncMock, return_value=None):
                    result = await client.background_provision_model()

            # psutil should not have been called because broker path was used
            mock_psutil.virtual_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_available_gb_from_snapshot(self):
        """Verify the admission gate sees the correct available_gb from broker."""
        client = _make_client()
        broker = _mock_broker()
        # 8 GB free -> should allow larger models
        free_bytes = 8 * (1024 ** 3)
        broker.latest_snapshot = _mock_snapshot(physical_free=free_bytes)
        client.register_with_broker(broker)

        # We need to capture what available_gb is used for model selection.
        # The function iterates QUANT_CATALOG and checks entry["min_ram_gb"] <= available_gb.
        # With 8 GB, the first entry that fits is mistral-7b-q4 (min_ram_gb=8).
        # Patch _discover_model to return a path for the first match to short-circuit.
        found_path = MagicMock()
        with patch.object(client, "_discover_model", return_value=found_path):
            result = await client.background_provision_model()

        # Should return True since a model was found on disk
        assert result is True


# ---------------------------------------------------------------------------
# Admission gate: fallback when broker has no snapshot
# ---------------------------------------------------------------------------

class TestAdmissionGateFallback:
    """Verify fallback to legacy path when broker has no snapshot."""

    @pytest.mark.asyncio
    async def test_falls_back_when_no_snapshot(self):
        """When broker is active but has no snapshot, MemoryQuantizer is used as fallback."""
        client = _make_client()
        broker = _mock_broker()
        broker.latest_snapshot = None
        client.register_with_broker(broker)

        # Since broker has no snapshot, available_gb stays None and falls
        # through to the legacy MemoryQuantizer path.  We mock it to
        # return a controlled value.
        mock_metrics = MagicMock()
        mock_metrics.system_memory_available_gb = 5.0

        mock_mq = AsyncMock()
        mock_mq.get_current_metrics.return_value = mock_metrics

        with patch(
            "backend.core.memory_quantizer.get_memory_quantizer",
            new_callable=AsyncMock,
            return_value=mock_mq,
        ):
            with patch.object(client, "_discover_model", return_value=None):
                with patch.object(client, "_auto_download_model", new_callable=AsyncMock, return_value=None):
                    result = await client.background_provision_model()

        # MemoryQuantizer was used (not broker, not psutil)
        mock_mq.get_current_metrics.assert_called()

    @pytest.mark.asyncio
    async def test_falls_back_when_mcp_not_active(self):
        """When MCP is not active, legacy MemoryQuantizer path is used."""
        client = _make_client()
        assert client._mcp_active is False

        mock_metrics = MagicMock()
        mock_metrics.system_memory_available_gb = 4.0

        mock_mq = AsyncMock()
        mock_mq.get_current_metrics.return_value = mock_metrics

        with patch(
            "backend.core.memory_quantizer.get_memory_quantizer",
            new_callable=AsyncMock,
            return_value=mock_mq,
        ):
            with patch.object(client, "_discover_model", return_value=None):
                with patch.object(client, "_auto_download_model", new_callable=AsyncMock, return_value=None):
                    result = await client.background_provision_model()

        mock_mq.get_current_metrics.assert_called()


# ---------------------------------------------------------------------------
# Design intent tests (source code inspection)
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify that the source code contains the expected integration points."""

    def test_source_imports_memory_budget_broker(self):
        """Module should import MemoryBudgetBroker under TYPE_CHECKING."""
        from backend.intelligence import unified_model_serving as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_budget_broker import MemoryBudgetBroker" in source

    def test_source_imports_pressure_tier_at_module_level(self):
        """Module should import PressureTier at module level."""
        from backend.intelligence import unified_model_serving as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_types import PressureTier" in source

    def test_source_contains_type_checking_guard(self):
        """Module should use TYPE_CHECKING guard for broker import."""
        from backend.intelligence import unified_model_serving as mod
        source = inspect.getsource(mod)
        assert "TYPE_CHECKING" in source

    def test_register_with_broker_method_exists(self):
        client = _make_client()
        assert hasattr(client, "register_with_broker")
        assert callable(client.register_with_broker)

    def test_init_has_mcp_active_attr(self):
        """PrimeLocalClient.__init__ must set _mcp_active."""
        from backend.intelligence.unified_model_serving import PrimeLocalClient
        source = inspect.getsource(PrimeLocalClient.__init__)
        assert "_mcp_active" in source

    def test_init_has_broker_attr(self):
        """PrimeLocalClient.__init__ must set _broker."""
        from backend.intelligence.unified_model_serving import PrimeLocalClient
        source = inspect.getsource(PrimeLocalClient.__init__)
        assert "_broker" in source

    def test_admission_gate_uses_mcp_active_guard(self):
        """background_provision_model must guard broker usage with _mcp_active."""
        from backend.intelligence.unified_model_serving import PrimeLocalClient
        source = inspect.getsource(PrimeLocalClient.background_provision_model)
        assert "self._mcp_active" in source

    def test_admission_gate_uses_latest_snapshot(self):
        """background_provision_model must read broker.latest_snapshot."""
        from backend.intelligence.unified_model_serving import PrimeLocalClient
        source = inspect.getsource(PrimeLocalClient.background_provision_model)
        assert "self._broker.latest_snapshot" in source

    def test_admission_gate_uses_physical_free(self):
        """background_provision_model must use snap.physical_free."""
        from backend.intelligence.unified_model_serving import PrimeLocalClient
        source = inspect.getsource(PrimeLocalClient.background_provision_model)
        assert "_snap.physical_free" in source

    def test_post_load_validation_uses_mcp_active_guard(self):
        """load_model must guard post-load broker usage with _mcp_active."""
        from backend.intelligence.unified_model_serving import PrimeLocalClient
        source = inspect.getsource(PrimeLocalClient.load_model)
        assert "self._mcp_active" in source

    def test_post_load_validation_uses_latest_snapshot(self):
        """load_model must read broker.latest_snapshot for post-load check."""
        from backend.intelligence.unified_model_serving import PrimeLocalClient
        source = inspect.getsource(PrimeLocalClient.load_model)
        assert "self._broker.latest_snapshot" in source
