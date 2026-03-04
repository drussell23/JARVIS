"""Tests for ecapa_cloud_service MCP broker integration (Task 12).

Verifies that ``ECAPAModelManager`` can register with the MCP broker,
uses ``PressureTier`` for the memory-pressure routing decision when the
broker is active, and falls back to the legacy psutil path when the
broker has no snapshot or is not registered.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from backend.core.memory_types import (
    MemorySnapshot,
    PressureTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_broker(epoch: int = 1) -> MagicMock:
    """Create a mock broker with the attributes ECAPAModelManager accesses."""
    broker = MagicMock()
    broker.register_pressure_observer = MagicMock()
    broker.current_epoch = epoch
    broker.current_sequence = 5
    broker.latest_snapshot = None  # default: no snapshot yet
    return broker


def _mock_snapshot(
    tier: PressureTier = PressureTier.OPTIMAL,
    snapshot_id: str = "snap-test-001",
    physical_total: int = 16 * (1024 ** 3),
    physical_free: int = 4 * (1024 ** 3),
) -> MagicMock:
    """Create a mock MemorySnapshot with the fields the manager reads."""
    snap = MagicMock(spec=MemorySnapshot)
    snap.pressure_tier = tier
    snap.snapshot_id = snapshot_id
    snap.physical_total = physical_total
    snap.physical_free = physical_free
    return snap


def _make_manager():
    """Create an ECAPAModelManager for testing."""
    from backend.cloud_services.ecapa_cloud_service import ECAPAModelManager
    return ECAPAModelManager()


# ---------------------------------------------------------------------------
# Default attributes
# ---------------------------------------------------------------------------

class TestECAPAModelManagerDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        mgr = _make_manager()
        assert mgr._mcp_active is False

    def test_broker_default_none(self):
        mgr = _make_manager()
        assert mgr._broker is None


# ---------------------------------------------------------------------------
# register_with_broker
# ---------------------------------------------------------------------------

class TestRegisterWithBroker:
    """Verify register_with_broker sets state correctly."""

    def test_sets_mcp_active_true(self):
        mgr = _make_manager()
        broker = _mock_broker()
        mgr.register_with_broker(broker)
        assert mgr._mcp_active is True

    def test_stores_broker_reference(self):
        mgr = _make_manager()
        broker = _mock_broker()
        mgr.register_with_broker(broker)
        assert mgr._broker is broker

    def test_idempotent_re_registration(self):
        mgr = _make_manager()
        broker1 = _mock_broker()
        broker2 = _mock_broker()
        mgr.register_with_broker(broker1)
        mgr.register_with_broker(broker2)
        assert mgr._broker is broker2
        assert mgr._mcp_active is True


# ---------------------------------------------------------------------------
# _load_model: broker path (PressureTier routing)
# ---------------------------------------------------------------------------

class TestLoadModelBrokerPath:
    """Verify _load_model uses PressureTier when broker is active."""

    @pytest.mark.asyncio
    async def test_constrained_skips_local_load(self):
        """When broker reports CONSTRAINED, should skip local and go cloud-only."""
        mgr = _make_manager()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CONSTRAINED)
        mgr.register_with_broker(broker)

        await mgr._load_model()

        assert mgr._cloud_only_mode is True
        assert mgr._ready is False

    @pytest.mark.asyncio
    async def test_critical_skips_local_load(self):
        """When broker reports CRITICAL, should skip local and go cloud-only."""
        mgr = _make_manager()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CRITICAL)
        mgr.register_with_broker(broker)

        await mgr._load_model()

        assert mgr._cloud_only_mode is True

    @pytest.mark.asyncio
    async def test_emergency_skips_local_load(self):
        """When broker reports EMERGENCY, should skip local and go cloud-only."""
        mgr = _make_manager()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.EMERGENCY)
        mgr.register_with_broker(broker)

        await mgr._load_model()

        assert mgr._cloud_only_mode is True

    @pytest.mark.asyncio
    async def test_optimal_allows_local_load(self):
        """When broker reports OPTIMAL, should attempt local loading."""
        mgr = _make_manager()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.OPTIMAL)
        mgr.register_with_broker(broker)

        with patch.object(mgr, "_load_model_process_isolated", new_callable=AsyncMock, return_value=True) as mock_load:
            await mgr._load_model()

        # Should have attempted local load, not gone to cloud-only
        mock_load.assert_called_once()
        assert mgr._cloud_only_mode is False

    @pytest.mark.asyncio
    async def test_elevated_allows_local_load(self):
        """When broker reports ELEVATED (below CONSTRAINED), should attempt local loading."""
        mgr = _make_manager()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ELEVATED)
        mgr.register_with_broker(broker)

        with patch.object(mgr, "_load_model_process_isolated", new_callable=AsyncMock, return_value=True) as mock_load:
            await mgr._load_model()

        mock_load.assert_called_once()
        assert mgr._cloud_only_mode is False

    @pytest.mark.asyncio
    async def test_abundant_allows_local_load(self):
        """When broker reports ABUNDANT, should attempt local loading."""
        mgr = _make_manager()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ABUNDANT)
        mgr.register_with_broker(broker)

        with patch.object(mgr, "_load_model_process_isolated", new_callable=AsyncMock, return_value=True) as mock_load:
            await mgr._load_model()

        mock_load.assert_called_once()
        assert mgr._cloud_only_mode is False

    @pytest.mark.asyncio
    async def test_does_not_call_psutil_when_broker_has_snapshot(self):
        """When MCP active with a valid snapshot, psutil should NOT be called."""
        mgr = _make_manager()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CONSTRAINED)
        mgr.register_with_broker(broker)

        with patch("psutil.virtual_memory") as mock_vm:
            await mgr._load_model()
            mock_vm.assert_not_called()


# ---------------------------------------------------------------------------
# _load_model: fallback to psutil
# ---------------------------------------------------------------------------

class TestLoadModelFallback:
    """Verify fallback to legacy psutil when broker is not active or has no snapshot."""

    @pytest.mark.asyncio
    async def test_falls_back_when_no_broker(self):
        """When MCP not active, should use psutil for memory check."""
        mgr = _make_manager()
        assert mgr._mcp_active is False

        mock_mem = MagicMock()
        mock_mem.available = 2 * (1024 ** 3)  # 2 GB - below 4 GB threshold

        with patch("psutil.virtual_memory", return_value=mock_mem) as mock_vm:
            await mgr._load_model()

        mock_vm.assert_called()
        assert mgr._cloud_only_mode is True

    @pytest.mark.asyncio
    async def test_falls_back_when_broker_has_no_snapshot(self):
        """When broker has no snapshot, should fall through to psutil."""
        mgr = _make_manager()
        broker = _mock_broker()
        broker.latest_snapshot = None
        mgr.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.available = 2 * (1024 ** 3)  # 2 GB - below threshold

        with patch("psutil.virtual_memory", return_value=mock_mem) as mock_vm:
            await mgr._load_model()

        mock_vm.assert_called()
        assert mgr._cloud_only_mode is True

    @pytest.mark.asyncio
    async def test_psutil_above_threshold_allows_local_load(self):
        """When psutil reports sufficient memory, should attempt local loading."""
        mgr = _make_manager()
        assert mgr._mcp_active is False

        mock_mem = MagicMock()
        mock_mem.available = 8 * (1024 ** 3)  # 8 GB - well above 4 GB threshold

        with patch("psutil.virtual_memory", return_value=mock_mem), \
             patch.object(mgr, "_load_model_process_isolated", new_callable=AsyncMock, return_value=True) as mock_load:
            await mgr._load_model()

        mock_load.assert_called_once()
        assert mgr._cloud_only_mode is False


# ---------------------------------------------------------------------------
# Design intent tests (source code inspection)
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify that the source code contains the expected integration points."""

    def test_source_imports_memory_budget_broker(self):
        """Module should import MemoryBudgetBroker under TYPE_CHECKING."""
        from backend.cloud_services import ecapa_cloud_service as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_budget_broker import MemoryBudgetBroker" in source

    def test_source_imports_pressure_tier_at_module_level(self):
        """Module should import PressureTier at module level."""
        from backend.cloud_services import ecapa_cloud_service as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_types import PressureTier" in source

    def test_source_contains_type_checking_guard(self):
        """Module should use TYPE_CHECKING guard for broker import."""
        from backend.cloud_services import ecapa_cloud_service as mod
        source = inspect.getsource(mod)
        assert "TYPE_CHECKING" in source

    def test_register_with_broker_method_exists(self):
        mgr = _make_manager()
        assert hasattr(mgr, "register_with_broker")
        assert callable(mgr.register_with_broker)

    def test_register_with_broker_docstring_mentions_pressure_tier(self):
        mgr = _make_manager()
        doc = mgr.register_with_broker.__doc__ or ""
        assert "PressureTier" in doc

    def test_load_model_uses_mcp_active_guard(self):
        """_load_model must guard broker usage with _mcp_active."""
        from backend.cloud_services.ecapa_cloud_service import ECAPAModelManager
        source = inspect.getsource(ECAPAModelManager._load_model)
        assert "self._mcp_active" in source

    def test_load_model_uses_latest_snapshot(self):
        """_load_model must read broker.latest_snapshot."""
        from backend.cloud_services.ecapa_cloud_service import ECAPAModelManager
        source = inspect.getsource(ECAPAModelManager._load_model)
        assert "self._broker.latest_snapshot" in source

    def test_load_model_uses_pressure_tier_constrained(self):
        """_load_model must reference PressureTier.CONSTRAINED."""
        from backend.cloud_services.ecapa_cloud_service import ECAPAModelManager
        source = inspect.getsource(ECAPAModelManager._load_model)
        assert "PressureTier.CONSTRAINED" in source

    def test_load_model_uses_snap_pressure_tier(self):
        """_load_model must reference snap.pressure_tier."""
        from backend.cloud_services.ecapa_cloud_service import ECAPAModelManager
        source = inspect.getsource(ECAPAModelManager._load_model)
        assert "_snap.pressure_tier" in source
