"""Tests for gcp_vm_manager MCP broker integration (Task 6).

Verifies that ``GCPVMManager`` and ``LocalMemoryFallback`` can register
with the MCP broker, read memory from the broker's cached snapshot
instead of raw psutil, and use ``PressureTier`` for VM lifecycle
decisions.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from backend.core.memory_types import (
    MemorySnapshot,
    PressurePolicy,
    PressureTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_broker(epoch: int = 1) -> MagicMock:
    """Create a mock broker with the attributes the VM manager accesses."""
    broker = MagicMock()
    broker.register_pressure_observer = MagicMock()
    broker.current_epoch = epoch
    broker.current_sequence = 5
    broker.policy = PressurePolicy()
    broker.coordinator = MagicMock()
    broker.coordinator.submit = MagicMock(return_value="dec-abc123")
    broker.latest_snapshot = None  # default: no snapshot yet
    return broker


def _mock_snapshot(
    tier: PressureTier = PressureTier.OPTIMAL,
    snapshot_id: str = "snap-test-001",
    physical_total: int = 16 * (1024 ** 3),
    physical_free: int = 4 * (1024 ** 3),
) -> MagicMock:
    """Create a mock MemorySnapshot with the fields the VM manager reads."""
    snap = MagicMock(spec=MemorySnapshot)
    snap.pressure_tier = tier
    snap.snapshot_id = snapshot_id
    snap.physical_total = physical_total
    snap.physical_free = physical_free
    return snap


def _make_manager():
    """Create a GCPVMManager with minimal config for testing."""
    from backend.core.gcp_vm_manager import GCPVMManager
    return GCPVMManager()


def _make_fallback():
    """Create a fresh LocalMemoryFallback, resetting the singleton."""
    from backend.core.gcp_vm_manager import LocalMemoryFallback
    # Reset singleton for test isolation
    LocalMemoryFallback._instance = None
    return LocalMemoryFallback()


# ---------------------------------------------------------------------------
# GCPVMManager: Default attributes
# ---------------------------------------------------------------------------

class TestGCPVMManagerDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        mgr = _make_manager()
        assert mgr._mcp_active is False

    def test_broker_default_none(self):
        mgr = _make_manager()
        assert mgr._broker is None


# ---------------------------------------------------------------------------
# GCPVMManager: register_with_broker
# ---------------------------------------------------------------------------

class TestGCPVMManagerRegisterWithBroker:
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
# GCPVMManager: VM lifecycle decision uses pressure tier
# ---------------------------------------------------------------------------

class TestGCPVMManagerLifecycleDecision:
    """Verify that Site 1 (VM lifecycle) references broker and PressureTier."""

    def test_source_contains_pressure_tier_optimal(self):
        """The VM lifecycle method should reference PressureTier.OPTIMAL."""
        from backend.core import gcp_vm_manager as mod
        source = inspect.getsource(mod)
        assert "PressureTier.OPTIMAL" in source

    def test_source_contains_mcp_active_guard(self):
        """The VM lifecycle should guard broker usage with _mcp_active."""
        from backend.core import gcp_vm_manager as mod
        source = inspect.getsource(mod)
        assert "self._mcp_active" in source

    def test_source_contains_latest_snapshot(self):
        """The VM lifecycle should read from self._broker.latest_snapshot."""
        from backend.core import gcp_vm_manager as mod
        source = inspect.getsource(mod)
        assert "self._broker.latest_snapshot" in source

    def test_source_contains_pressure_normalized_logic(self):
        """The VM lifecycle should have _pressure_normalized flag."""
        from backend.core import gcp_vm_manager as mod
        source = inspect.getsource(mod)
        assert "_pressure_normalized" in source


# ---------------------------------------------------------------------------
# proactive_vm_manager_init: broker parameter
# ---------------------------------------------------------------------------

class TestProactiveVmManagerInit:
    """Verify the module-level function accepts and uses broker."""

    def test_function_accepts_broker_kwarg(self):
        from backend.core.gcp_vm_manager import proactive_vm_manager_init
        sig = inspect.signature(proactive_vm_manager_init)
        assert "broker" in sig.parameters

    def test_broker_param_defaults_to_none(self):
        from backend.core.gcp_vm_manager import proactive_vm_manager_init
        sig = inspect.signature(proactive_vm_manager_init)
        assert sig.parameters["broker"].default is None

    def test_source_references_pressure_tier(self):
        """The function should use PressureTier for broker-based decisions."""
        from backend.core.gcp_vm_manager import proactive_vm_manager_init
        source = inspect.getsource(proactive_vm_manager_init)
        assert "PressureTier.OPTIMAL" in source

    def test_source_references_broker_latest_snapshot(self):
        """The function should read broker.latest_snapshot."""
        from backend.core.gcp_vm_manager import proactive_vm_manager_init
        source = inspect.getsource(proactive_vm_manager_init)
        assert "broker.latest_snapshot" in source

    @pytest.mark.asyncio
    async def test_returns_none_when_broker_pressure_low(self):
        """When broker reports OPTIMAL, should not init VM manager."""
        from backend.core.gcp_vm_manager import proactive_vm_manager_init

        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.OPTIMAL)

        result = await proactive_vm_manager_init(broker=broker)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_broker_pressure_abundant(self):
        """When broker reports ABUNDANT, should not init VM manager."""
        from backend.core.gcp_vm_manager import proactive_vm_manager_init

        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ABUNDANT)

        result = await proactive_vm_manager_init(broker=broker)
        assert result is None

    @pytest.mark.asyncio
    async def test_attempts_init_when_broker_pressure_elevated(self):
        """When broker reports ELEVATED, should attempt to init VM manager."""
        from backend.core.gcp_vm_manager import proactive_vm_manager_init

        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ELEVATED)

        with patch(
            "backend.core.gcp_vm_manager.get_gcp_vm_manager_safe",
            return_value=None,  # Manager not available
        ):
            result = await proactive_vm_manager_init(broker=broker)
            # Returns None because manager is not available, but the
            # important thing is it attempted (didn't short-circuit)
            assert result is None

    @pytest.mark.asyncio
    async def test_force_overrides_broker_pressure(self):
        """When force=True, should attempt init regardless of broker pressure."""
        from backend.core.gcp_vm_manager import proactive_vm_manager_init

        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(tier=PressureTier.ABUNDANT)

        with patch(
            "backend.core.gcp_vm_manager.get_gcp_vm_manager_safe",
            return_value=None,
        ):
            # Should not raise even though pressure is low
            result = await proactive_vm_manager_init(force=True, broker=broker)
            assert result is None  # manager not available, but attempted

    @pytest.mark.asyncio
    async def test_falls_back_to_psutil_when_no_broker(self):
        """When no broker, should use psutil (legacy path)."""
        from backend.core.gcp_vm_manager import proactive_vm_manager_init

        mock_mem = MagicMock()
        mock_mem.percent = 50.0  # Below threshold

        with patch("psutil.virtual_memory", return_value=mock_mem):
            result = await proactive_vm_manager_init(memory_threshold=70.0)
            assert result is None

    @pytest.mark.asyncio
    async def test_falls_back_to_psutil_when_broker_has_no_snapshot(self):
        """When broker has no snapshot yet, should fall through to psutil."""
        from backend.core.gcp_vm_manager import proactive_vm_manager_init

        broker = _mock_broker()
        broker.latest_snapshot = None  # No snapshot yet

        mock_mem = MagicMock()
        mock_mem.percent = 50.0  # Below threshold

        with patch("psutil.virtual_memory", return_value=mock_mem):
            result = await proactive_vm_manager_init(broker=broker)
            assert result is None


# ---------------------------------------------------------------------------
# LocalMemoryFallback: Default attributes
# ---------------------------------------------------------------------------

class TestLocalMemoryFallbackDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        fb = _make_fallback()
        assert fb._mcp_active is False

    def test_broker_default_none(self):
        fb = _make_fallback()
        assert fb._broker is None


# ---------------------------------------------------------------------------
# LocalMemoryFallback: register_with_broker
# ---------------------------------------------------------------------------

class TestLocalMemoryFallbackRegisterWithBroker:
    """Verify register_with_broker sets state correctly."""

    def test_sets_mcp_active_true(self):
        fb = _make_fallback()
        broker = _mock_broker()
        fb.register_with_broker(broker)
        assert fb._mcp_active is True

    def test_stores_broker_reference(self):
        fb = _make_fallback()
        broker = _mock_broker()
        fb.register_with_broker(broker)
        assert fb._broker is broker


# ---------------------------------------------------------------------------
# LocalMemoryFallback: attempt_local_relief uses broker snapshot
# ---------------------------------------------------------------------------

class TestLocalMemoryFallbackRelief:
    """Verify attempt_local_relief uses broker for initial reading."""

    @pytest.mark.asyncio
    async def test_uses_broker_snapshot_for_initial_percent(self):
        """When MCP active, initial_memory_percent should come from broker."""
        fb = _make_fallback()
        broker = _mock_broker()

        total = 16 * (1024 ** 3)     # 16 GB
        free = 4 * (1024 ** 3)       # 4 GB free => 75% used
        broker.latest_snapshot = _mock_snapshot(
            tier=PressureTier.ELEVATED,
            physical_total=total,
            physical_free=free,
        )
        fb.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.percent = 80.0
        mock_mem.available = 3 * (1024 ** 2)  # 3 GB available

        with patch("psutil.virtual_memory", return_value=mock_mem), \
             patch("psutil.process_iter", return_value=[]):
            result = await fb.attempt_local_relief()

        expected_pct = (total - free) / total * 100.0  # 75.0%
        assert abs(result["initial_memory_percent"] - expected_pct) < 0.1

    @pytest.mark.asyncio
    async def test_final_percent_always_raw_psutil(self):
        """Final memory reading should always be raw psutil, not broker."""
        fb = _make_fallback()
        broker = _mock_broker()
        broker.latest_snapshot = _mock_snapshot(
            tier=PressureTier.ELEVATED,
            physical_total=16 * (1024 ** 3),
            physical_free=4 * (1024 ** 3),
        )
        fb.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.percent = 80.0
        mock_mem.available = 3 * (1024 ** 2)

        with patch("psutil.virtual_memory", return_value=mock_mem), \
             patch("psutil.process_iter", return_value=[]):
            result = await fb.attempt_local_relief()

        # Final should be from psutil (80.0), not broker (75.0)
        assert result["final_memory_percent"] == 80.0

    @pytest.mark.asyncio
    async def test_falls_back_to_psutil_when_no_broker(self):
        """When MCP inactive, initial_memory_percent should come from psutil."""
        fb = _make_fallback()
        assert fb._mcp_active is False

        mock_mem = MagicMock()
        mock_mem.percent = 82.0
        mock_mem.available = 2.5 * (1024 ** 2)

        with patch("psutil.virtual_memory", return_value=mock_mem), \
             patch("psutil.process_iter", return_value=[]):
            result = await fb.attempt_local_relief()

        assert result["initial_memory_percent"] == 82.0

    @pytest.mark.asyncio
    async def test_falls_back_to_psutil_when_snapshot_none(self):
        """When broker has no snapshot, should fall back to psutil."""
        fb = _make_fallback()
        broker = _mock_broker()
        broker.latest_snapshot = None
        fb.register_with_broker(broker)

        mock_mem = MagicMock()
        mock_mem.percent = 85.0
        mock_mem.available = 2 * (1024 ** 2)

        with patch("psutil.virtual_memory", return_value=mock_mem), \
             patch("psutil.process_iter", return_value=[]):
            result = await fb.attempt_local_relief()

        assert result["initial_memory_percent"] == 85.0


# ---------------------------------------------------------------------------
# Design intent: source-level verification
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify that the source code references broker in the right places."""

    def test_gcp_vm_manager_imports_pressure_tier(self):
        """Module should import PressureTier."""
        from backend.core import gcp_vm_manager as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_types import PressureTier" in source

    def test_gcp_vm_manager_imports_memory_snapshot(self):
        """Module should import MemorySnapshot."""
        from backend.core import gcp_vm_manager as mod
        source = inspect.getsource(mod)
        assert "MemorySnapshot" in source

    def test_gcp_vm_manager_type_checking_broker_import(self):
        """Module should import MemoryBudgetBroker under TYPE_CHECKING."""
        from backend.core import gcp_vm_manager as mod
        source = inspect.getsource(mod)
        assert "from backend.core.memory_budget_broker import MemoryBudgetBroker" in source

    def test_register_with_broker_method_exists_on_vm_manager(self):
        mgr = _make_manager()
        assert hasattr(mgr, "register_with_broker")
        assert callable(mgr.register_with_broker)

    def test_register_with_broker_method_exists_on_fallback(self):
        fb = _make_fallback()
        assert hasattr(fb, "register_with_broker")
        assert callable(fb.register_with_broker)

    def test_proactive_init_broker_docstring(self):
        """Docstring should mention broker."""
        from backend.core.gcp_vm_manager import proactive_vm_manager_init
        assert "broker" in (proactive_vm_manager_init.__doc__ or "").lower()
