"""Tests for parallel_initializer MCP broker integration (Task 10).

Verifies that ``ParallelInitializer`` can register with the MCP broker,
use ``PressureTier`` for force-sequential and critical-RAM decisions
instead of raw psutil, and fall back to legacy psutil when the broker
has no snapshot.

The 3 migrated call sites:
  Site 1 (line ~623): Admission gate ``available_gb`` — uses ``snapshot.physical_free``.
  Site 2 (line ~899): Force sequential when <4GB — uses ``PressureTier >= CONSTRAINED``.
  Site 3 (line ~1101): RAM check between sequential components — uses
         ``PressureTier >= CRITICAL``.
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
    """Create a mock broker with attributes the initializer accesses."""
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


def _make_initializer():
    """Create a ParallelInitializer with a mock app for testing.

    Avoids touching real FastAPI app or registering real components.
    """
    from backend.core.parallel_initializer import ParallelInitializer

    mock_app = MagicMock()
    mock_app.state = MagicMock()
    # Patch _register_components to avoid heavy init dependency setup
    with patch.object(ParallelInitializer, "_register_components"):
        init = ParallelInitializer(mock_app)
    return init


# ---------------------------------------------------------------------------
# Default attributes
# ---------------------------------------------------------------------------

class TestParallelInitializerDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        init = _make_initializer()
        assert init._mcp_active is False

    def test_broker_default_none(self):
        init = _make_initializer()
        assert init._broker is None


# ---------------------------------------------------------------------------
# register_with_broker
# ---------------------------------------------------------------------------

class TestRegisterWithBroker:
    """Verify register_with_broker sets state correctly."""

    def test_sets_mcp_active_true(self):
        init = _make_initializer()
        broker = _mock_broker()
        init.register_with_broker(broker)
        assert init._mcp_active is True

    def test_stores_broker_reference(self):
        init = _make_initializer()
        broker = _mock_broker()
        init.register_with_broker(broker)
        assert init._broker is broker

    def test_idempotent_re_registration(self):
        init = _make_initializer()
        broker1 = _mock_broker()
        broker2 = _mock_broker()
        init.register_with_broker(broker1)
        init.register_with_broker(broker2)
        assert init._broker is broker2
        assert init._mcp_active is True


# ---------------------------------------------------------------------------
# Site 2: Force sequential init — PressureTier path
# ---------------------------------------------------------------------------

class TestForceSequentialBrokerPath:
    """Verify _force_sequential_on_bridge_unavailable uses PressureTier
    when broker is active instead of raw psutil < 4GB check."""

    def test_constrained_forces_sequential(self):
        """CONSTRAINED tier (>= CONSTRAINED) should force sequential init."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CONSTRAINED)
        init.register_with_broker(broker)

        # Simulate the bridge unavailable path
        init._force_sequential = False
        init._handle_bridge_unavailable_ram_check("test_reason")
        assert init._force_sequential is True

    def test_critical_forces_sequential(self):
        """CRITICAL tier should force sequential init."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CRITICAL)
        init.register_with_broker(broker)

        init._force_sequential = False
        init._handle_bridge_unavailable_ram_check("test_reason")
        assert init._force_sequential is True

    def test_emergency_forces_sequential(self):
        """EMERGENCY tier should force sequential init."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.EMERGENCY)
        init.register_with_broker(broker)

        init._force_sequential = False
        init._handle_bridge_unavailable_ram_check("test_reason")
        assert init._force_sequential is True

    def test_optimal_allows_parallel(self):
        """OPTIMAL tier (< CONSTRAINED) should NOT force sequential init."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.OPTIMAL)
        init.register_with_broker(broker)

        init._force_sequential = False
        init._handle_bridge_unavailable_ram_check("test_reason")
        assert init._force_sequential is False

    def test_abundant_allows_parallel(self):
        """ABUNDANT tier should NOT force sequential init."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ABUNDANT)
        init.register_with_broker(broker)

        init._force_sequential = False
        init._handle_bridge_unavailable_ram_check("test_reason")
        assert init._force_sequential is False

    def test_elevated_allows_parallel(self):
        """ELEVATED tier (< CONSTRAINED) should NOT force sequential init."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ELEVATED)
        init.register_with_broker(broker)

        init._force_sequential = False
        init._handle_bridge_unavailable_ram_check("test_reason")
        assert init._force_sequential is False


# ---------------------------------------------------------------------------
# Site 2: Fallback — psutil when broker has no snapshot
# ---------------------------------------------------------------------------

class TestForceSequentialFallback:
    """Verify fallback to legacy psutil when broker has no snapshot."""

    def test_falls_back_when_no_snapshot(self):
        """When broker is active but has no snapshot, fall back to psutil."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = None
        init.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.available = 3 * (1024 ** 3)  # 3 GB < 4 GB threshold

        init._force_sequential = False
        with patch("psutil.virtual_memory", return_value=mock_mem):
            init._handle_bridge_unavailable_ram_check("test_reason")

        assert init._force_sequential is True  # 3 GB < 4 GB -> sequential

    def test_falls_back_when_mcp_not_active(self):
        """When MCP is not active, use legacy psutil path."""
        init = _make_initializer()
        assert init._mcp_active is False

        mock_mem = MagicMock()
        mock_mem.available = 5 * (1024 ** 3)  # 5 GB >= 4 GB

        init._force_sequential = False
        with patch("psutil.virtual_memory", return_value=mock_mem):
            init._handle_bridge_unavailable_ram_check("test_reason")

        assert init._force_sequential is False  # 5 GB >= 4 GB -> parallel ok

    def test_psutil_fallback_forces_sequential_under_4gb(self):
        """Legacy psutil with < 4 GB should force sequential."""
        init = _make_initializer()

        mock_mem = MagicMock()
        mock_mem.available = 2 * (1024 ** 3)  # 2 GB < 4 GB

        init._force_sequential = False
        with patch("psutil.virtual_memory", return_value=mock_mem):
            init._handle_bridge_unavailable_ram_check("test_reason")

        assert init._force_sequential is True

    def test_psutil_unavailable_forces_sequential(self):
        """When psutil also fails, should still force sequential (fail-closed)."""
        init = _make_initializer()

        init._force_sequential = False
        with patch("psutil.virtual_memory", side_effect=Exception("psutil error")):
            init._handle_bridge_unavailable_ram_check("test_reason")

        assert init._force_sequential is True


# ---------------------------------------------------------------------------
# Site 3: RAM check between sequential components — PressureTier path
# ---------------------------------------------------------------------------

class TestSequentialRamCheckBrokerPath:
    """Verify that the RAM check between sequential components uses
    PressureTier >= CRITICAL when broker is active."""

    def test_critical_tier_signals_abort(self):
        """When pressure tier is CRITICAL, _should_abort_sequential should return True."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CRITICAL)
        init.register_with_broker(broker)

        assert init._should_abort_sequential() is True

    def test_emergency_tier_signals_abort(self):
        """EMERGENCY tier should also signal abort."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.EMERGENCY)
        init.register_with_broker(broker)

        assert init._should_abort_sequential() is True

    def test_constrained_tier_does_not_abort(self):
        """CONSTRAINED is below CRITICAL, should NOT abort sequential."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.CONSTRAINED)
        init.register_with_broker(broker)

        assert init._should_abort_sequential() is False

    def test_optimal_tier_does_not_abort(self):
        """OPTIMAL tier should NOT abort sequential."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.OPTIMAL)
        init.register_with_broker(broker)

        assert init._should_abort_sequential() is False


# ---------------------------------------------------------------------------
# Site 3: Fallback — psutil when broker has no snapshot
# ---------------------------------------------------------------------------

class TestSequentialRamCheckFallback:
    """Verify fallback to legacy psutil < 2 GB check when broker inactive."""

    def test_falls_back_when_no_snapshot(self):
        """When broker has no snapshot, fall back to psutil."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = None
        init.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.available = 1 * (1024 ** 3)  # 1 GB < 2 GB

        with patch("psutil.virtual_memory", return_value=mock_mem):
            result = init._should_abort_sequential()

        assert result is True  # 1 GB < 2 GB -> abort

    def test_falls_back_when_mcp_not_active(self):
        """When MCP not active, use psutil path."""
        init = _make_initializer()
        assert init._mcp_active is False

        mock_mem = MagicMock()
        mock_mem.available = 5 * (1024 ** 3)  # 5 GB >= 2 GB

        with patch("psutil.virtual_memory", return_value=mock_mem):
            result = init._should_abort_sequential()

        assert result is False  # 5 GB >= 2 GB -> ok

    def test_psutil_failure_does_not_abort(self):
        """When psutil fails, should not abort (fail-open for inter-component check)."""
        init = _make_initializer()

        with patch("psutil.virtual_memory", side_effect=Exception("psutil error")):
            result = init._should_abort_sequential()

        assert result is False  # legacy behavior: except Exception: pass


# ---------------------------------------------------------------------------
# Site 1: Admission gate available_gb — broker snapshot path
# ---------------------------------------------------------------------------

class TestAdmissionGateAvailableGb:
    """Verify admission gate uses broker snapshot for available_gb when active."""

    def test_uses_broker_snapshot_for_available_gb(self):
        """When broker active with snapshot, available_gb from snapshot."""
        init = _make_initializer()
        broker = _mock_broker()
        free = 6 * (1024 ** 3)  # 6 GB
        broker.latest_snapshot = _mock_snapshot(physical_free=free)
        init.register_with_broker(broker)

        result = init._get_broker_available_gb()
        expected = free / (1024 ** 3)
        assert result is not None
        assert abs(result - expected) < 0.01

    def test_returns_none_when_no_snapshot(self):
        """When broker has no snapshot, return None (trigger psutil fallback)."""
        init = _make_initializer()
        broker = _mock_broker()
        broker.latest_snapshot = None
        init.register_with_broker(broker)

        result = init._get_broker_available_gb()
        assert result is None

    def test_returns_none_when_mcp_not_active(self):
        """When MCP is not active, return None."""
        init = _make_initializer()
        assert init._mcp_active is False

        result = init._get_broker_available_gb()
        assert result is None


# ---------------------------------------------------------------------------
# Design intent tests (source code inspection)
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify that the source code contains the expected integration points."""

    def test_source_imports_memory_budget_broker(self):
        """Module should import MemoryBudgetBroker under TYPE_CHECKING."""
        from backend.core import parallel_initializer as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_budget_broker import MemoryBudgetBroker" in source

    def test_source_imports_pressure_tier_at_module_level(self):
        """Module should import PressureTier at module level."""
        from backend.core import parallel_initializer as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_types import PressureTier" in source

    def test_source_contains_type_checking_guard(self):
        """Module should use TYPE_CHECKING guard for broker import."""
        from backend.core import parallel_initializer as mod
        source = inspect.getsource(mod)
        assert "TYPE_CHECKING" in source

    def test_register_with_broker_method_exists(self):
        init = _make_initializer()
        assert hasattr(init, "register_with_broker")
        assert callable(init.register_with_broker)

    def test_handle_bridge_unavailable_uses_pressure_tier(self):
        """_handle_bridge_unavailable_ram_check must reference PressureTier.CONSTRAINED."""
        from backend.core.parallel_initializer import ParallelInitializer
        source = inspect.getsource(ParallelInitializer._handle_bridge_unavailable_ram_check)
        assert "PressureTier.CONSTRAINED" in source

    def test_handle_bridge_unavailable_uses_mcp_active_guard(self):
        """_handle_bridge_unavailable_ram_check must guard with _mcp_active."""
        from backend.core.parallel_initializer import ParallelInitializer
        source = inspect.getsource(ParallelInitializer._handle_bridge_unavailable_ram_check)
        assert "self._mcp_active" in source

    def test_should_abort_sequential_uses_pressure_tier_critical(self):
        """_should_abort_sequential must reference PressureTier.CRITICAL."""
        from backend.core.parallel_initializer import ParallelInitializer
        source = inspect.getsource(ParallelInitializer._should_abort_sequential)
        assert "PressureTier.CRITICAL" in source

    def test_should_abort_sequential_uses_mcp_active_guard(self):
        """_should_abort_sequential must guard with _mcp_active."""
        from backend.core.parallel_initializer import ParallelInitializer
        source = inspect.getsource(ParallelInitializer._should_abort_sequential)
        assert "self._mcp_active" in source

    def test_get_broker_available_gb_uses_physical_free(self):
        """_get_broker_available_gb must use snap.physical_free."""
        from backend.core.parallel_initializer import ParallelInitializer
        source = inspect.getsource(ParallelInitializer._get_broker_available_gb)
        assert "snap.physical_free" in source

    def test_get_broker_available_gb_uses_latest_snapshot(self):
        """_get_broker_available_gb must read broker.latest_snapshot."""
        from backend.core.parallel_initializer import ParallelInitializer
        source = inspect.getsource(ParallelInitializer._get_broker_available_gb)
        assert "self._broker.latest_snapshot" in source

    def test_source_contains_mcp_active_in_init(self):
        """__init__ should set _mcp_active = False."""
        from backend.core.parallel_initializer import ParallelInitializer
        source = inspect.getsource(ParallelInitializer.__init__)
        assert "_mcp_active" in source

    def test_source_contains_broker_in_init(self):
        """__init__ should set _broker = None."""
        from backend.core.parallel_initializer import ParallelInitializer
        source = inspect.getsource(ParallelInitializer.__init__)
        assert "_broker" in source
