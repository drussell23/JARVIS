"""Tests for memory_fault_guard MCP broker integration (Task 8).

Verifies that ``MemoryFaultGuard`` can register with the MCP broker,
read memory from the broker's cached snapshot instead of raw psutil,
use ``PressureTier >= CONSTRAINED`` for the cloud offload decision,
and fall back to the legacy psutil path when the broker has no snapshot.
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
    """Create a mock broker with attributes the fault guard accesses."""
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


def _make_guard():
    """Create a fresh MemoryFaultGuard for testing.

    Bypasses the singleton by resetting the class-level _instance before
    each call so every test gets a clean guard.
    """
    from backend.core.memory_fault_guard import MemoryFaultGuard
    MemoryFaultGuard._instance = None
    guard = MemoryFaultGuard()
    return guard


# ---------------------------------------------------------------------------
# Default attributes
# ---------------------------------------------------------------------------

class TestMemoryFaultGuardDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        guard = _make_guard()
        assert guard._mcp_active is False

    def test_broker_default_none(self):
        guard = _make_guard()
        assert guard._broker is None


# ---------------------------------------------------------------------------
# register_with_broker
# ---------------------------------------------------------------------------

class TestRegisterWithBroker:
    """Verify register_with_broker sets state correctly."""

    def test_sets_mcp_active_true(self):
        guard = _make_guard()
        broker = _mock_broker()
        guard.register_with_broker(broker)
        assert guard._mcp_active is True

    def test_stores_broker_reference(self):
        guard = _make_guard()
        broker = _mock_broker()
        guard.register_with_broker(broker)
        assert guard._broker is broker

    def test_idempotent_re_registration(self):
        guard = _make_guard()
        broker1 = _mock_broker()
        broker2 = _mock_broker()
        guard.register_with_broker(broker1)
        guard.register_with_broker(broker2)
        assert guard._broker is broker2
        assert guard._mcp_active is True


# ---------------------------------------------------------------------------
# should_offload_to_cloud: pressure tier path (key test)
# ---------------------------------------------------------------------------

class TestShouldOffloadToCloudBrokerPath:
    """Verify should_offload_to_cloud uses PressureTier when broker active."""

    def test_constrained_triggers_offload(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CONSTRAINED)
        guard.register_with_broker(broker)

        should_offload, reason = guard.should_offload_to_cloud()
        assert should_offload is True
        assert "CONSTRAINED" in reason

    def test_critical_triggers_offload(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CRITICAL)
        guard.register_with_broker(broker)

        should_offload, reason = guard.should_offload_to_cloud()
        assert should_offload is True
        assert "CRITICAL" in reason

    def test_emergency_triggers_offload(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.EMERGENCY)
        guard.register_with_broker(broker)

        should_offload, reason = guard.should_offload_to_cloud()
        assert should_offload is True
        assert "EMERGENCY" in reason

    def test_optimal_does_not_trigger_offload(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.OPTIMAL)
        guard.register_with_broker(broker)

        should_offload, reason = guard.should_offload_to_cloud()
        assert should_offload is False
        assert "OPTIMAL" in reason

    def test_abundant_does_not_trigger_offload(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ABUNDANT)
        guard.register_with_broker(broker)

        should_offload, reason = guard.should_offload_to_cloud()
        assert should_offload is False
        assert "ABUNDANT" in reason

    def test_elevated_does_not_trigger_offload(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ELEVATED)
        guard.register_with_broker(broker)

        should_offload, reason = guard.should_offload_to_cloud()
        assert should_offload is False
        assert "ELEVATED" in reason

    def test_recent_faults_take_priority_over_tier(self):
        """Recent faults should trigger offload regardless of pressure tier."""
        from datetime import datetime

        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.OPTIMAL)
        guard.register_with_broker(broker)

        # Add a recent fault to history
        from backend.core.memory_fault_guard import FaultEvent, FaultType, FaultSeverity
        event = FaultEvent(
            fault_type=FaultType.SIGBUS,
            severity=FaultSeverity.FATAL,
            timestamp=datetime.now(),
        )
        guard._fault_history.append(event)

        should_offload, reason = guard.should_offload_to_cloud()
        assert should_offload is True
        assert "Recent memory faults" in reason

    def test_reserve_released_takes_priority_over_tier(self):
        """Released reserve should trigger offload regardless of tier."""
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.OPTIMAL)
        guard.register_with_broker(broker)
        guard._reserve_released = True

        should_offload, reason = guard.should_offload_to_cloud()
        assert should_offload is True
        assert "Emergency reserve" in reason


# ---------------------------------------------------------------------------
# should_offload_to_cloud: fallback when broker has no snapshot
# ---------------------------------------------------------------------------

class TestShouldOffloadToCloudFallback:
    """Verify fallback to legacy psutil when broker has no snapshot."""

    def test_falls_back_when_no_snapshot(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = None
        guard.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.percent = 50.0
        mock_mem.available = 8 * (1024 ** 3)  # 8 GB
        mock_mem.total = 16 * (1024 ** 3)

        with patch("backend.core.memory_fault_guard.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            should_offload, reason = guard.should_offload_to_cloud()

        # With 8 GB available and 500 MB needed, should NOT offload
        assert should_offload is False
        assert "Memory healthy" in reason

    def test_falls_back_when_mcp_not_active(self):
        guard = _make_guard()
        assert guard._mcp_active is False

        mock_mem = MagicMock()
        mock_mem.percent = 50.0
        mock_mem.available = 8 * (1024 ** 3)
        mock_mem.total = 16 * (1024 ** 3)

        with patch("backend.core.memory_fault_guard.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            should_offload, reason = guard.should_offload_to_cloud()

        assert should_offload is False


# ---------------------------------------------------------------------------
# get_status: broker snapshot path
# ---------------------------------------------------------------------------

class TestGetStatusBrokerPath:
    """Verify get_status uses broker snapshot when active."""

    def test_uses_broker_snapshot_when_active(self):
        guard = _make_guard()
        broker = _mock_broker()

        total = 16 * (1024 ** 3)
        free = 4 * (1024 ** 3)  # 75% used
        broker.latest_snapshot = _mock_snapshot(
            physical_total=total,
            physical_free=free,
        )
        guard.register_with_broker(broker)

        with patch("backend.core.memory_fault_guard.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 99.0
            mock_mem.available = 1 * (1024 ** 3)
            mock_psutil.virtual_memory.return_value = mock_mem

            status = guard.get_status()

        expected_avail_mb = free / (1024 * 1024)
        expected_pct = (total - free) / total * 100.0

        assert abs(status["memory_available_mb"] - expected_avail_mb) < 0.1
        assert abs(status["memory_percent"] - expected_pct) < 0.1
        assert status["mcp_active"] is True

    def test_does_not_call_psutil_when_broker_has_snapshot(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot()
        guard.register_with_broker(broker)

        with patch("backend.core.memory_fault_guard.psutil") as mock_psutil:
            guard.get_status()
            mock_psutil.virtual_memory.assert_not_called()

    def test_falls_back_to_psutil_when_no_snapshot(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = None
        guard.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.percent = 82.0
        mock_mem.available = 3 * (1024 ** 3)

        with patch("backend.core.memory_fault_guard.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            status = guard.get_status()

        assert abs(status["memory_percent"] - 82.0) < 0.1

    def test_mcp_active_false_when_not_registered(self):
        guard = _make_guard()
        with patch("backend.core.memory_fault_guard.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 50.0
            mock_mem.available = 8 * (1024 ** 3)
            mock_psutil.virtual_memory.return_value = mock_mem
            status = guard.get_status()
        assert status["mcp_active"] is False


# ---------------------------------------------------------------------------
# check_memory_available: broker path
# ---------------------------------------------------------------------------

class TestCheckMemoryAvailableBrokerPath:
    """Verify check_memory_available uses broker snapshot when active."""

    def test_uses_broker_snapshot_when_active(self):
        guard = _make_guard()
        broker = _mock_broker()
        # 4 GB free
        broker.latest_snapshot = _mock_snapshot(
            physical_free=4 * (1024 ** 3),
        )
        guard.register_with_broker(broker)

        with patch("backend.core.memory_fault_guard.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.available = 1 * (1024 ** 3)  # 1 GB - should NOT be used
            mock_psutil.virtual_memory.return_value = mock_mem

            ok, reason = guard.check_memory_available(500)

        # With 4 GB free and 500 MB min, effective = 4096 - 500 = 3596 MB
        # Need 500 MB, so should be ok
        assert ok is True
        mock_psutil.virtual_memory.assert_not_called()

    def test_falls_back_when_no_snapshot(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = None
        guard.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.available = 2 * (1024 ** 3)

        with patch("backend.core.memory_fault_guard.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            ok, reason = guard.check_memory_available(500)

        mock_psutil.virtual_memory.assert_called()


# ---------------------------------------------------------------------------
# check_vm_region_availability: broker path
# ---------------------------------------------------------------------------

class TestCheckVmRegionAvailabilityBrokerPath:
    """Verify check_vm_region_availability uses broker snapshot when active."""

    def test_uses_broker_snapshot_when_active(self):
        guard = _make_guard()
        broker = _mock_broker()
        total = 16 * (1024 ** 3)
        free = 8 * (1024 ** 3)  # 50% used
        broker.latest_snapshot = _mock_snapshot(
            physical_total=total,
            physical_free=free,
        )
        guard.register_with_broker(broker)

        with patch("backend.core.memory_fault_guard.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 99.0  # should NOT be used
            mock_psutil.virtual_memory.return_value = mock_mem

            ok, reason = guard.check_vm_region_availability()

        assert ok is True  # 50% < 85% threshold
        assert "50.0%" in reason
        mock_psutil.virtual_memory.assert_not_called()

    def test_reports_high_usage_from_broker(self):
        guard = _make_guard()
        broker = _mock_broker()
        total = 16 * (1024 ** 3)
        free = 1 * (1024 ** 3)  # 93.75% used
        broker.latest_snapshot = _mock_snapshot(
            physical_total=total,
            physical_free=free,
        )
        guard.register_with_broker(broker)

        ok, reason = guard.check_vm_region_availability()
        assert ok is False
        assert "too high" in reason


# ---------------------------------------------------------------------------
# Design intent tests (source code inspection)
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify that the source code contains the expected integration points."""

    def test_source_imports_memory_budget_broker(self):
        """Module should import MemoryBudgetBroker under TYPE_CHECKING."""
        from backend.core import memory_fault_guard as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_budget_broker import MemoryBudgetBroker" in source

    def test_source_imports_pressure_tier_at_module_level(self):
        """Module should import PressureTier at module level."""
        from backend.core import memory_fault_guard as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_types import PressureTier" in source

    def test_source_contains_type_checking_guard(self):
        """Module should use TYPE_CHECKING guard for broker import."""
        from backend.core import memory_fault_guard as mod
        source = inspect.getsource(mod)
        assert "TYPE_CHECKING" in source

    def test_register_with_broker_method_exists(self):
        guard = _make_guard()
        assert hasattr(guard, "register_with_broker")
        assert callable(guard.register_with_broker)

    def test_should_offload_uses_pressure_tier(self):
        """should_offload_to_cloud must reference PressureTier.CONSTRAINED."""
        from backend.core.memory_fault_guard import MemoryFaultGuard
        source = inspect.getsource(MemoryFaultGuard.should_offload_to_cloud)
        assert "PressureTier.CONSTRAINED" in source

    def test_should_offload_uses_mcp_active_guard(self):
        """should_offload_to_cloud must guard broker usage with _mcp_active."""
        from backend.core.memory_fault_guard import MemoryFaultGuard
        source = inspect.getsource(MemoryFaultGuard.should_offload_to_cloud)
        assert "self._mcp_active" in source

    def test_should_offload_uses_latest_snapshot(self):
        """should_offload_to_cloud must read broker.latest_snapshot."""
        from backend.core.memory_fault_guard import MemoryFaultGuard
        source = inspect.getsource(MemoryFaultGuard.should_offload_to_cloud)
        assert "self._broker.latest_snapshot" in source

    def test_get_status_uses_mcp_active(self):
        """get_status must contain _mcp_active or broker snapshot logic."""
        from backend.core.memory_fault_guard import MemoryFaultGuard
        source = inspect.getsource(MemoryFaultGuard.get_status)
        assert "_get_snapshot_available_mb" in source or "_mcp_active" in source

    def test_check_memory_available_uses_broker_helper(self):
        """check_memory_available must use broker snapshot helper."""
        from backend.core.memory_fault_guard import MemoryFaultGuard
        source = inspect.getsource(MemoryFaultGuard.check_memory_available)
        assert "_get_snapshot_available_mb" in source

    def test_check_vm_region_uses_broker_helper(self):
        """check_vm_region_availability must use broker snapshot helper."""
        from backend.core.memory_fault_guard import MemoryFaultGuard
        source = inspect.getsource(MemoryFaultGuard.check_vm_region_availability)
        assert "_get_snapshot_usage_percent" in source

    def test_source_contains_physical_free(self):
        """The snapshot helpers should use snap.physical_free."""
        from backend.core.memory_fault_guard import MemoryFaultGuard
        source = inspect.getsource(MemoryFaultGuard._get_snapshot_available_mb)
        assert "snap.physical_free" in source

    def test_source_contains_physical_total(self):
        """The snapshot helpers should use snap.physical_total."""
        from backend.core.memory_fault_guard import MemoryFaultGuard
        source = inspect.getsource(MemoryFaultGuard._get_snapshot_usage_percent)
        assert "snap.physical_total" in source


# ---------------------------------------------------------------------------
# Snapshot helper unit tests
# ---------------------------------------------------------------------------

class TestSnapshotHelpers:
    """Verify the broker snapshot helper methods."""

    def test_get_snapshot_available_mb_returns_none_when_inactive(self):
        guard = _make_guard()
        assert guard._get_snapshot_available_mb() is None

    def test_get_snapshot_available_mb_returns_none_when_no_snapshot(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = None
        guard.register_with_broker(broker)
        assert guard._get_snapshot_available_mb() is None

    def test_get_snapshot_available_mb_returns_correct_value(self):
        guard = _make_guard()
        broker = _mock_broker()
        free_bytes = 4 * (1024 ** 3)
        broker.latest_snapshot = _mock_snapshot(physical_free=free_bytes)
        guard.register_with_broker(broker)

        result = guard._get_snapshot_available_mb()
        expected = free_bytes / (1024 * 1024)
        assert result is not None
        assert abs(result - expected) < 0.01

    def test_get_snapshot_usage_percent_returns_none_when_inactive(self):
        guard = _make_guard()
        assert guard._get_snapshot_usage_percent() is None

    def test_get_snapshot_usage_percent_returns_correct_value(self):
        guard = _make_guard()
        broker = _mock_broker()
        total = 16 * (1024 ** 3)
        free = 4 * (1024 ** 3)  # 75% used
        broker.latest_snapshot = _mock_snapshot(
            physical_total=total, physical_free=free
        )
        guard.register_with_broker(broker)

        result = guard._get_snapshot_usage_percent()
        assert result is not None
        assert abs(result - 75.0) < 0.1

    def test_get_snapshot_usage_percent_handles_zero_total(self):
        guard = _make_guard()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(physical_total=0, physical_free=0)
        guard.register_with_broker(broker)

        result = guard._get_snapshot_usage_percent()
        assert result == 0.0

    def test_get_snapshot_total_gb_returns_none_when_inactive(self):
        guard = _make_guard()
        assert guard._get_snapshot_total_gb() is None

    def test_get_snapshot_total_gb_returns_correct_value(self):
        guard = _make_guard()
        broker = _mock_broker()
        total = 16 * (1024 ** 3)
        broker.latest_snapshot = _mock_snapshot(physical_total=total)
        guard.register_with_broker(broker)

        result = guard._get_snapshot_total_gb()
        assert result is not None
        assert abs(result - 16.0) < 0.01
