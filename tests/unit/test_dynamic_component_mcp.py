"""Tests for dynamic_component_manager MCP broker integration (Task 9).

Verifies that ``MemoryPressureMonitor`` can register with the MCP broker,
maps ``PressureTier`` to ``MemoryPressure`` when the broker is active,
computes ``memory_available_mb()`` from the broker snapshot, and falls
back to the legacy psutil path when the broker has no snapshot.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from backend.core.memory_types import (
    MemorySnapshot,
    PressureTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_broker(epoch: int = 1) -> MagicMock:
    """Create a mock broker with attributes the monitor accesses."""
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


def _make_monitor():
    """Create a fresh MemoryPressureMonitor for testing."""
    from backend.core.dynamic_component_manager import MemoryPressureMonitor
    return MemoryPressureMonitor()


# ---------------------------------------------------------------------------
# Default attributes
# ---------------------------------------------------------------------------

class TestMemoryPressureMonitorDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        monitor = _make_monitor()
        assert monitor._mcp_active is False

    def test_broker_default_none(self):
        monitor = _make_monitor()
        assert monitor._broker is None


# ---------------------------------------------------------------------------
# register_with_broker
# ---------------------------------------------------------------------------

class TestRegisterWithBroker:
    """Verify register_with_broker sets state correctly."""

    def test_sets_mcp_active_true(self):
        monitor = _make_monitor()
        broker = _mock_broker()
        monitor.register_with_broker(broker)
        assert monitor._mcp_active is True

    def test_stores_broker_reference(self):
        monitor = _make_monitor()
        broker = _mock_broker()
        monitor.register_with_broker(broker)
        assert monitor._broker is broker

    def test_idempotent_re_registration(self):
        monitor = _make_monitor()
        broker1 = _mock_broker()
        broker2 = _mock_broker()
        monitor.register_with_broker(broker1)
        monitor.register_with_broker(broker2)
        assert monitor._broker is broker2
        assert monitor._mcp_active is True


# ---------------------------------------------------------------------------
# current_pressure: PressureTier mapping path
# ---------------------------------------------------------------------------

class TestCurrentPressureBrokerPath:
    """Verify current_pressure maps PressureTier to MemoryPressure."""

    def test_abundant_maps_to_low(self):
        from backend.core.dynamic_component_manager import MemoryPressure
        monitor = _make_monitor()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ABUNDANT)
        monitor.register_with_broker(broker)

        assert monitor.current_pressure() == MemoryPressure.LOW

    def test_optimal_maps_to_low(self):
        from backend.core.dynamic_component_manager import MemoryPressure
        monitor = _make_monitor()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.OPTIMAL)
        monitor.register_with_broker(broker)

        assert monitor.current_pressure() == MemoryPressure.LOW

    def test_elevated_maps_to_medium(self):
        from backend.core.dynamic_component_manager import MemoryPressure
        monitor = _make_monitor()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ELEVATED)
        monitor.register_with_broker(broker)

        assert monitor.current_pressure() == MemoryPressure.MEDIUM

    def test_constrained_maps_to_high(self):
        from backend.core.dynamic_component_manager import MemoryPressure
        monitor = _make_monitor()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CONSTRAINED)
        monitor.register_with_broker(broker)

        assert monitor.current_pressure() == MemoryPressure.HIGH

    def test_critical_maps_to_critical(self):
        from backend.core.dynamic_component_manager import MemoryPressure
        monitor = _make_monitor()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CRITICAL)
        monitor.register_with_broker(broker)

        assert monitor.current_pressure() == MemoryPressure.CRITICAL

    def test_emergency_maps_to_emergency(self):
        from backend.core.dynamic_component_manager import MemoryPressure
        monitor = _make_monitor()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.EMERGENCY)
        monitor.register_with_broker(broker)

        assert monitor.current_pressure() == MemoryPressure.EMERGENCY

    def test_does_not_call_psutil_when_broker_has_snapshot(self):
        monitor = _make_monitor()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.OPTIMAL)
        monitor.register_with_broker(broker)

        with patch("backend.core.dynamic_component_manager.psutil") as mock_psutil:
            monitor.current_pressure()
            mock_psutil.virtual_memory.assert_not_called()


# ---------------------------------------------------------------------------
# current_pressure: fallback when broker has no snapshot
# ---------------------------------------------------------------------------

class TestCurrentPressureFallback:
    """Verify fallback to legacy psutil when broker has no snapshot."""

    def test_falls_back_when_no_snapshot(self):
        from backend.core.dynamic_component_manager import MemoryPressure
        monitor = _make_monitor()
        broker = _mock_broker()
        broker.latest_snapshot = None
        monitor.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.available = 5 * (1024 ** 3)  # 5 GB -> LOW

        with patch("backend.core.dynamic_component_manager.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            result = monitor.current_pressure()

        assert result == MemoryPressure.LOW
        mock_psutil.virtual_memory.assert_called()

    def test_falls_back_when_mcp_not_active(self):
        from backend.core.dynamic_component_manager import MemoryPressure
        monitor = _make_monitor()
        assert monitor._mcp_active is False

        mock_mem = MagicMock()
        mock_mem.available = 3 * (1024 ** 3)  # 3 GB -> MEDIUM

        with patch("backend.core.dynamic_component_manager.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            result = monitor.current_pressure()

        assert result == MemoryPressure.MEDIUM


# ---------------------------------------------------------------------------
# memory_available_mb: broker path
# ---------------------------------------------------------------------------

class TestMemoryAvailableMbBrokerPath:
    """Verify memory_available_mb uses broker snapshot when active."""

    def test_uses_broker_snapshot_when_active(self):
        monitor = _make_monitor()
        broker = _mock_broker()
        free_bytes = 4 * (1024 ** 3)
        broker.latest_snapshot = _mock_snapshot(physical_free=free_bytes)
        monitor.register_with_broker(broker)

        with patch("backend.core.dynamic_component_manager.psutil") as mock_psutil:
            result = monitor.memory_available_mb()
            mock_psutil.virtual_memory.assert_not_called()

        expected = int(free_bytes // (1024 * 1024))
        assert result == expected

    def test_returns_correct_value_from_snapshot(self):
        monitor = _make_monitor()
        broker = _mock_broker()
        # 2.5 GB free
        free_bytes = int(2.5 * (1024 ** 3))
        broker.latest_snapshot = _mock_snapshot(physical_free=free_bytes)
        monitor.register_with_broker(broker)

        result = monitor.memory_available_mb()
        expected = int(free_bytes // (1024 * 1024))
        assert result == expected

    def test_falls_back_when_no_snapshot(self):
        monitor = _make_monitor()
        broker = _mock_broker()
        broker.latest_snapshot = None
        monitor.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.available = 6 * (1024 ** 3)

        with patch("backend.core.dynamic_component_manager.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            result = monitor.memory_available_mb()

        mock_psutil.virtual_memory.assert_called()
        expected = 6 * (1024 ** 3) // (1024 * 1024)
        assert result == expected

    def test_falls_back_when_mcp_not_active(self):
        monitor = _make_monitor()

        mock_mem = MagicMock()
        mock_mem.available = 8 * (1024 ** 3)

        with patch("backend.core.dynamic_component_manager.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            result = monitor.memory_available_mb()

        expected = 8 * (1024 ** 3) // (1024 * 1024)
        assert result == expected


# ---------------------------------------------------------------------------
# Design intent tests (source code inspection)
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify that the source code contains the expected integration points."""

    def test_source_imports_memory_budget_broker(self):
        """Module should import MemoryBudgetBroker under TYPE_CHECKING."""
        from backend.core import dynamic_component_manager as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_budget_broker import MemoryBudgetBroker" in source

    def test_source_imports_pressure_tier_at_module_level(self):
        """Module should import PressureTier at module level."""
        from backend.core import dynamic_component_manager as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_types import PressureTier" in source

    def test_source_contains_type_checking_guard(self):
        """Module should use TYPE_CHECKING guard for broker import."""
        from backend.core import dynamic_component_manager as mod
        source = inspect.getsource(mod)
        assert "TYPE_CHECKING" in source

    def test_register_with_broker_method_exists(self):
        monitor = _make_monitor()
        assert hasattr(monitor, "register_with_broker")
        assert callable(monitor.register_with_broker)

    def test_current_pressure_uses_mcp_active_guard(self):
        """current_pressure must guard broker usage with _mcp_active."""
        from backend.core.dynamic_component_manager import MemoryPressureMonitor
        source = inspect.getsource(MemoryPressureMonitor.current_pressure)
        assert "self._mcp_active" in source

    def test_current_pressure_uses_latest_snapshot(self):
        """current_pressure must read broker.latest_snapshot."""
        from backend.core.dynamic_component_manager import MemoryPressureMonitor
        source = inspect.getsource(MemoryPressureMonitor.current_pressure)
        assert "self._broker.latest_snapshot" in source

    def test_current_pressure_uses_pressure_tier(self):
        """current_pressure must reference snap.pressure_tier."""
        from backend.core.dynamic_component_manager import MemoryPressureMonitor
        source = inspect.getsource(MemoryPressureMonitor.current_pressure)
        assert "snap.pressure_tier" in source

    def test_memory_available_mb_uses_mcp_active_guard(self):
        """memory_available_mb must guard broker usage with _mcp_active."""
        from backend.core.dynamic_component_manager import MemoryPressureMonitor
        source = inspect.getsource(MemoryPressureMonitor.memory_available_mb)
        assert "self._mcp_active" in source

    def test_memory_available_mb_uses_physical_free(self):
        """memory_available_mb must use snap.physical_free."""
        from backend.core.dynamic_component_manager import MemoryPressureMonitor
        source = inspect.getsource(MemoryPressureMonitor.memory_available_mb)
        assert "snap.physical_free" in source

    def test_tier_to_pressure_mapping_exists(self):
        """MemoryPressureMonitor must have _TIER_TO_PRESSURE class attribute."""
        from backend.core.dynamic_component_manager import MemoryPressureMonitor
        assert hasattr(MemoryPressureMonitor, "_TIER_TO_PRESSURE")
        mapping = MemoryPressureMonitor._TIER_TO_PRESSURE
        assert len(mapping) == 6  # All 6 PressureTier values

    def test_tier_to_pressure_mapping_covers_all_tiers(self):
        """Mapping must cover every PressureTier value."""
        from backend.core.dynamic_component_manager import MemoryPressureMonitor
        mapping = MemoryPressureMonitor._TIER_TO_PRESSURE
        for tier in PressureTier:
            assert tier in mapping, f"PressureTier.{tier.name} missing from mapping"

    def test_log_enrichment_uses_broker_snapshot(self):
        """_handle_memory_pressure log section must reference broker snapshot."""
        from backend.core.dynamic_component_manager import DynamicComponentManager
        source = inspect.getsource(DynamicComponentManager._handle_memory_pressure)
        assert "monitor._mcp_active" in source or "_broker" in source
        assert "snap.physical_free" in source
