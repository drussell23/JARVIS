"""Tests for gcp_oom_prevention_bridge MCP broker integration (Task 7).

Verifies that ``GCPOOMPreventionBridge`` can register with the MCP broker,
read memory from the broker's cached snapshot as the primary path in
``_get_memory_status()``, and fall back to the legacy psutil / env-var
chain when the broker has no snapshot.

Sites 3 and 4 (before/after measurement in ``_try_aggressive_memory_optimization``)
are intentionally left as raw psutil and verified via design-intent tests.
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
    """Create a mock broker with attributes the OOM bridge accesses."""
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


def _make_bridge():
    """Create a GCPOOMPreventionBridge for testing."""
    from backend.core.gcp_oom_prevention_bridge import GCPOOMPreventionBridge
    return GCPOOMPreventionBridge()


# ---------------------------------------------------------------------------
# Default attributes
# ---------------------------------------------------------------------------

class TestGCPOOMPreventionBridgeDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        bridge = _make_bridge()
        assert bridge._mcp_active is False

    def test_broker_default_none(self):
        bridge = _make_bridge()
        assert bridge._broker is None


# ---------------------------------------------------------------------------
# register_with_broker
# ---------------------------------------------------------------------------

class TestRegisterWithBroker:
    """Verify register_with_broker sets state correctly."""

    def test_sets_mcp_active_true(self):
        bridge = _make_bridge()
        broker = _mock_broker()
        bridge.register_with_broker(broker)
        assert bridge._mcp_active is True

    def test_stores_broker_reference(self):
        bridge = _make_bridge()
        broker = _mock_broker()
        bridge.register_with_broker(broker)
        assert bridge._broker is broker

    def test_idempotent_re_registration(self):
        bridge = _make_bridge()
        broker1 = _mock_broker()
        broker2 = _mock_broker()
        bridge.register_with_broker(broker1)
        bridge.register_with_broker(broker2)
        assert bridge._broker is broker2
        assert bridge._mcp_active is True


# ---------------------------------------------------------------------------
# _get_memory_status: broker snapshot path
# ---------------------------------------------------------------------------

class TestGetMemoryStatusBrokerPath:
    """Verify _get_memory_status uses broker snapshot when active."""

    @pytest.mark.asyncio
    async def test_uses_broker_snapshot_when_active(self):
        """When MCP active and snapshot available, should return broker values."""
        bridge = _make_bridge()
        broker = _mock_broker()

        total = 16 * (1024 ** 3)     # 16 GB
        free = 4 * (1024 ** 3)       # 4 GB free => 75% used
        broker.latest_snapshot = _mock_snapshot(
            physical_total=total,
            physical_free=free,
        )
        bridge.register_with_broker(broker)

        # Patch psutil to return 99% -- if broker is used, this should NOT be used
        with patch("backend.core.gcp_oom_prevention_bridge.psutil", create=True) as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 99.0
            mock_mem.available = 1 * (1024 ** 3)
            mock_mem.total = total
            mock_psutil.virtual_memory.return_value = mock_mem

            available_gb, pressure = await bridge._get_memory_status()

        expected_available = free / (1024 ** 3)  # 4.0 GB
        expected_pressure = (total - free) / total * 100.0  # 75.0%

        assert abs(available_gb - expected_available) < 0.01
        assert abs(pressure - expected_pressure) < 0.1

    @pytest.mark.asyncio
    async def test_returns_correct_values_for_different_memory_sizes(self):
        """Verify math is correct for 32 GB total with 8 GB free."""
        bridge = _make_bridge()
        broker = _mock_broker()

        total = 32 * (1024 ** 3)
        free = 8 * (1024 ** 3)  # 75% used
        broker.latest_snapshot = _mock_snapshot(
            physical_total=total,
            physical_free=free,
        )
        bridge.register_with_broker(broker)

        available_gb, pressure = await bridge._get_memory_status()
        assert abs(available_gb - 8.0) < 0.01
        assert abs(pressure - 75.0) < 0.1

    @pytest.mark.asyncio
    async def test_handles_zero_total_gracefully(self):
        """When physical_total is 0, should return 50% pressure as default."""
        bridge = _make_bridge()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(
            physical_total=0,
            physical_free=0,
        )
        bridge.register_with_broker(broker)

        available_gb, pressure = await bridge._get_memory_status()
        assert available_gb == 0.0
        assert pressure == 50.0


# ---------------------------------------------------------------------------
# _get_memory_status: fallback to psutil when broker has no snapshot
# ---------------------------------------------------------------------------

class TestGetMemoryStatusFallback:
    """Verify fallback to legacy paths when broker is inactive or has no snapshot."""

    @pytest.mark.asyncio
    async def test_falls_back_when_broker_has_no_snapshot(self):
        """When broker has no snapshot, should fall through to legacy path."""
        bridge = _make_bridge()
        broker = _mock_broker()
        broker.latest_snapshot = None  # No snapshot yet
        bridge.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.percent = 82.0
        mock_mem.available = 3 * (1024 ** 3)
        mock_mem.total = 16 * (1024 ** 3)

        import os
        env_clean = {k: v for k, v in os.environ.items()
                     if k != "JARVIS_MEASURED_AVAILABLE_GB"}

        with patch.dict("os.environ", env_clean, clear=True), \
             patch("psutil.virtual_memory", return_value=mock_mem):
            available_gb, pressure = await bridge._get_memory_status()

        # Should use psutil fallback (82.0%), not broker
        assert pressure == 82.0

    @pytest.mark.asyncio
    async def test_falls_back_when_mcp_not_active(self):
        """When _mcp_active is False, should use legacy path."""
        bridge = _make_bridge()
        assert bridge._mcp_active is False

        mock_mem = MagicMock()
        mock_mem.percent = 78.0
        mock_mem.available = 4 * (1024 ** 3)
        mock_mem.total = 16 * (1024 ** 3)

        import os
        env_clean = {k: v for k, v in os.environ.items()
                     if k != "JARVIS_MEASURED_AVAILABLE_GB"}

        with patch.dict("os.environ", env_clean, clear=True), \
             patch("psutil.virtual_memory", return_value=mock_mem):
            available_gb, pressure = await bridge._get_memory_status()

        assert pressure == 78.0


# ---------------------------------------------------------------------------
# Design intent tests (source code inspection)
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify that the source code contains the expected integration points."""

    def test_source_imports_memory_budget_broker(self):
        """Module should import MemoryBudgetBroker under TYPE_CHECKING."""
        from backend.core import gcp_oom_prevention_bridge as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_budget_broker import MemoryBudgetBroker" in source

    def test_source_imports_pressure_tier_at_module_level(self):
        """Module should import PressureTier at module level."""
        from backend.core import gcp_oom_prevention_bridge as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_types import PressureTier" in source

    def test_source_contains_type_checking_guard(self):
        """Module should use TYPE_CHECKING guard for broker import."""
        from backend.core import gcp_oom_prevention_bridge as mod
        source = inspect.getsource(mod)
        assert "TYPE_CHECKING" in source

    def test_register_with_broker_method_exists(self):
        bridge = _make_bridge()
        assert hasattr(bridge, "register_with_broker")
        assert callable(bridge.register_with_broker)

    def test_source_contains_mcp_active_guard(self):
        """The _get_memory_status method should guard broker usage with _mcp_active."""
        from backend.core.gcp_oom_prevention_bridge import GCPOOMPreventionBridge
        source = inspect.getsource(GCPOOMPreventionBridge._get_memory_status)
        assert "self._mcp_active" in source

    def test_source_contains_latest_snapshot(self):
        """The _get_memory_status method should read broker.latest_snapshot."""
        from backend.core.gcp_oom_prevention_bridge import GCPOOMPreventionBridge
        source = inspect.getsource(GCPOOMPreventionBridge._get_memory_status)
        assert "self._broker.latest_snapshot" in source

    def test_source_contains_physical_free(self):
        """The broker path should use snap.physical_free for available_gb."""
        from backend.core.gcp_oom_prevention_bridge import GCPOOMPreventionBridge
        source = inspect.getsource(GCPOOMPreventionBridge._get_memory_status)
        assert "snap.physical_free" in source

    def test_source_contains_physical_total(self):
        """The broker path should use snap.physical_total for pressure calc."""
        from backend.core.gcp_oom_prevention_bridge import GCPOOMPreventionBridge
        source = inspect.getsource(GCPOOMPreventionBridge._get_memory_status)
        assert "snap.physical_total" in source


# ---------------------------------------------------------------------------
# Before/after measurement stays as raw psutil (design intent)
# ---------------------------------------------------------------------------

class TestBeforeAfterMeasurementDesignIntent:
    """Verify that before/after measurements in _try_aggressive_memory_optimization
    use raw psutil (not broker), since they measure a specific operation delta."""

    def test_aggressive_optimization_uses_raw_psutil(self):
        """The before/after measurement should use psutil.virtual_memory().available."""
        from backend.core.gcp_oom_prevention_bridge import GCPOOMPreventionBridge
        source = inspect.getsource(
            GCPOOMPreventionBridge._try_aggressive_memory_optimization
        )
        # Should contain raw psutil calls for measurement
        assert "psutil.virtual_memory().available" in source

    def test_aggressive_optimization_does_not_use_broker(self):
        """Before/after measurement should NOT reference the broker."""
        from backend.core.gcp_oom_prevention_bridge import GCPOOMPreventionBridge
        source = inspect.getsource(
            GCPOOMPreventionBridge._try_aggressive_memory_optimization
        )
        assert "_broker" not in source
        assert "_mcp_active" not in source
