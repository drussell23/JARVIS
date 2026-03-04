"""Tests for intelligent_memory_optimizer MCP broker integration (Task 13).

Verifies that ``IntelligentMemoryOptimizer`` and ``MemoryOptimizationAPI``
can register with the MCP broker, read memory from the broker's cached
snapshot instead of raw psutil for decision-making sites, and keep raw
psutil for effectiveness measurements.
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
    """Create a mock broker with the attributes accessed by the optimizer."""
    broker = MagicMock()
    broker.current_epoch = epoch
    broker.current_sequence = 5
    broker.latest_snapshot = None
    return broker


def _mock_snapshot(
    physical_total: int = 16 * (1024 ** 3),
    physical_free: int = 4 * (1024 ** 3),
    snapshot_id: str = "snap-test-001",
) -> MagicMock:
    """Create a mock MemorySnapshot.

    Default: 16 GB total, 4 GB free => 75% used.
    """
    snap = MagicMock(spec=MemorySnapshot)
    snap.physical_total = physical_total
    snap.physical_free = physical_free
    snap.snapshot_id = snapshot_id
    snap.pressure_tier = PressureTier.OPTIMAL
    return snap


# ---------------------------------------------------------------------------
# Default attribute tests
# ---------------------------------------------------------------------------

class TestOptimizerDefaults:
    """Verify default attribute initialization."""

    def test_mcp_active_default_false(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        assert opt._mcp_active is False

    def test_broker_default_none(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        assert opt._broker is None


# ---------------------------------------------------------------------------
# register_with_broker tests
# ---------------------------------------------------------------------------

class TestRegisterWithBroker:
    """Verify register_with_broker sets state correctly."""

    def test_sets_mcp_active_true(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()
        opt.register_with_broker(broker)
        assert opt._mcp_active is True

    def test_stores_broker_reference(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()
        opt.register_with_broker(broker)
        assert opt._broker is broker


# ---------------------------------------------------------------------------
# _get_memory_percent_from_broker tests
# ---------------------------------------------------------------------------

class TestGetMemoryPercentFromBroker:
    """Verify the _get_memory_percent_from_broker helper."""

    def test_returns_none_when_not_active(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        assert opt._get_memory_percent_from_broker() is None

    def test_returns_none_when_no_snapshot(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()
        broker.latest_snapshot = None
        opt.register_with_broker(broker)
        assert opt._get_memory_percent_from_broker() is None

    def test_returns_percent_from_snapshot(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()
        # 16 GB total, 4 GB free => 75% used
        snap = _mock_snapshot(
            physical_total=16 * (1024 ** 3),
            physical_free=4 * (1024 ** 3),
        )
        broker.latest_snapshot = snap
        opt.register_with_broker(broker)

        pct = opt._get_memory_percent_from_broker()
        assert pct is not None
        assert abs(pct - 75.0) < 0.1


# ---------------------------------------------------------------------------
# _get_memory_snapshot_from_broker tests
# ---------------------------------------------------------------------------

class TestGetMemorySnapshotFromBroker:
    """Verify the _get_memory_snapshot_from_broker helper."""

    def test_returns_none_when_not_active(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        assert opt._get_memory_snapshot_from_broker() is None

    def test_returns_dict_with_correct_keys(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()
        snap = _mock_snapshot()
        broker.latest_snapshot = snap
        opt.register_with_broker(broker)

        result = opt._get_memory_snapshot_from_broker()
        assert result is not None
        assert "percent" in result
        assert "used" in result
        assert "total" in result

    def test_computes_correct_values(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()
        total = 16 * (1024 ** 3)
        free = 4 * (1024 ** 3)
        snap = _mock_snapshot(physical_total=total, physical_free=free)
        broker.latest_snapshot = snap
        opt.register_with_broker(broker)

        result = opt._get_memory_snapshot_from_broker()
        assert result["total"] == total
        assert result["used"] == total - free
        assert abs(result["percent"] - 75.0) < 0.1


# ---------------------------------------------------------------------------
# Site 1: optimize_for_langchain initial snapshot
# ---------------------------------------------------------------------------

class TestOptimizeForLangchainInitialSnapshot:
    """Verify optimize_for_langchain uses broker for initial memory check."""

    @pytest.mark.asyncio
    async def test_uses_broker_when_active_and_below_target(self):
        """When broker reports memory below target, should succeed without psutil."""
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()
        # Report 30% used (below 45% target)
        total = 16 * (1024 ** 3)
        free = int(total * 0.70)  # 70% free = 30% used
        snap = _mock_snapshot(physical_total=total, physical_free=free)
        broker.latest_snapshot = snap
        opt.register_with_broker(broker)

        with patch("backend.memory.intelligent_memory_optimizer.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 99.0  # psutil would say 99%
            mock_psutil.virtual_memory.return_value = mock_mem

            success, report = await opt.optimize_for_langchain()

            assert success is True
            assert abs(report["initial_percent"] - 30.0) < 0.5
            # psutil.virtual_memory should NOT have been called
            mock_psutil.virtual_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_psutil_when_no_broker(self):
        """When broker is not active, should use psutil."""
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()

        with patch("backend.memory.intelligent_memory_optimizer.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 30.0  # below target
            mock_psutil.virtual_memory.return_value = mock_mem

            success, report = await opt.optimize_for_langchain()

            assert success is True
            assert abs(report["initial_percent"] - 30.0) < 0.1
            mock_psutil.virtual_memory.assert_called()


# ---------------------------------------------------------------------------
# Site 2: Loop check during optimization
# ---------------------------------------------------------------------------

class TestOptimizeLoopCheck:
    """Verify loop check uses broker when active."""

    @pytest.mark.asyncio
    async def test_loop_check_uses_broker(self):
        """When broker reports memory below target, loop should break early."""
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()

        # Initial snapshot: 80% used (above 45% target)
        total = 16 * (1024 ** 3)
        free_initial = int(total * 0.20)  # 80% used
        snap = _mock_snapshot(physical_total=total, physical_free=free_initial)
        broker.latest_snapshot = snap
        opt.register_with_broker(broker)

        strategy_called = False
        async def mock_strategy():
            nonlocal strategy_called
            strategy_called = True
            # After this strategy runs, simulate broker showing memory
            # is now below target
            new_snap = _mock_snapshot(
                physical_total=total,
                physical_free=int(total * 0.70),  # 30% used
            )
            broker.latest_snapshot = new_snap
            return 100.0

        # Patch strategies
        opt._optimize_python_memory = mock_strategy
        opt._kill_helper_processes = AsyncMock(return_value=0)
        opt._clear_system_caches = AsyncMock(return_value=0)
        opt._close_high_memory_applications = AsyncMock(return_value=0)
        opt._optimize_browser_memory = AsyncMock(return_value=0)
        opt._suspend_background_apps = AsyncMock(return_value=0)
        opt._purge_inactive_memory = AsyncMock(return_value=0)
        opt._save_optimization_report = MagicMock()

        with patch("backend.memory.intelligent_memory_optimizer.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 99.0
            mock_psutil.virtual_memory.return_value = mock_mem

            success, report = await opt.optimize_for_langchain()

        assert strategy_called is True
        # After first strategy, broker shows 30% -- loop should break
        # so later strategies should not be called
        opt._close_high_memory_applications.assert_not_called()


# ---------------------------------------------------------------------------
# Site 3: Final memory check
# ---------------------------------------------------------------------------

class TestOptimizeFinalCheck:
    """Verify final check uses broker when active."""

    @pytest.mark.asyncio
    async def test_final_percent_uses_broker(self):
        """Final percent in report should come from broker."""
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()
        total = 16 * (1024 ** 3)

        # Start above target, end below
        snap = _mock_snapshot(physical_total=total, physical_free=int(total * 0.60))
        broker.latest_snapshot = snap
        opt.register_with_broker(broker)

        # Stub out all strategies
        opt._optimize_python_memory = AsyncMock(return_value=0)
        opt._kill_helper_processes = AsyncMock(return_value=0)
        opt._clear_system_caches = AsyncMock(return_value=0)
        opt._close_high_memory_applications = AsyncMock(return_value=0)
        opt._optimize_browser_memory = AsyncMock(return_value=0)
        opt._suspend_background_apps = AsyncMock(return_value=0)
        opt._purge_inactive_memory = AsyncMock(return_value=0)
        opt._save_optimization_report = MagicMock()

        with patch("backend.memory.intelligent_memory_optimizer.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 99.0
            mock_psutil.virtual_memory.return_value = mock_mem

            success, report = await opt.optimize_for_langchain()

        # Final percent should be ~40% (from broker), not 99% (from psutil)
        assert abs(report["final_percent"] - 40.0) < 0.5
        assert success is True


# ---------------------------------------------------------------------------
# Site 4: _calculate_memory_to_free
# ---------------------------------------------------------------------------

class TestCalculateMemoryToFree:
    """Verify _calculate_memory_to_free uses broker when active."""

    def test_uses_broker_when_active(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()
        broker = _mock_broker()
        total = 16 * (1024 ** 3)
        free = int(total * 0.20)  # 80% used
        snap = _mock_snapshot(physical_total=total, physical_free=free)
        broker.latest_snapshot = snap
        opt.register_with_broker(broker)

        with patch("backend.memory.intelligent_memory_optimizer.psutil") as mock_psutil:
            result = opt._calculate_memory_to_free()
            # Should NOT call psutil
            mock_psutil.virtual_memory.assert_not_called()

        # 80% used, target 45% => need to free (80-45)% of 16GB
        used_mb = (total - free) / (1024 * 1024)
        target_used_mb = (total * 45 / 100) / (1024 * 1024)
        expected = used_mb - target_used_mb
        assert abs(result - expected) < 1.0

    def test_falls_back_to_psutil_when_no_broker(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        opt = IntelligentMemoryOptimizer()

        with patch("backend.memory.intelligent_memory_optimizer.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.used = 12 * (1024 ** 3)
            mock_mem.total = 16 * (1024 ** 3)
            mock_psutil.virtual_memory.return_value = mock_mem

            result = opt._calculate_memory_to_free()
            mock_psutil.virtual_memory.assert_called()
            assert result > 0


# ---------------------------------------------------------------------------
# Sites 5 & 7: Effectiveness tracking stays on psutil
# ---------------------------------------------------------------------------

class TestEffectivenessTrackingStaysPsutil:
    """Verify before/after measurements in _clear_system_caches and
    _purge_inactive_memory stay on raw psutil even when broker is active."""

    def test_clear_system_caches_uses_raw_psutil(self):
        """Source code for _clear_system_caches should use psutil.virtual_memory
        directly (not broker) for before/after available memory measurement."""
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        source = inspect.getsource(IntelligentMemoryOptimizer._clear_system_caches)
        # Should reference psutil.virtual_memory().available directly
        assert "psutil.virtual_memory().available" in source
        # Should NOT reference broker for this method
        assert "_get_memory_percent_from_broker" not in source
        assert "_get_memory_snapshot_from_broker" not in source

    def test_purge_inactive_memory_uses_raw_psutil(self):
        """Source code for _purge_inactive_memory should use psutil.virtual_memory
        directly for before/after available memory measurement."""
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        source = inspect.getsource(IntelligentMemoryOptimizer._purge_inactive_memory)
        assert "psutil.virtual_memory().available" in source
        assert "_get_memory_percent_from_broker" not in source
        assert "_get_memory_snapshot_from_broker" not in source


# ---------------------------------------------------------------------------
# Site 6: _close_high_memory_applications loop check
# ---------------------------------------------------------------------------

class TestCloseHighMemoryAppsLoopCheck:
    """Verify _close_high_memory_applications uses broker for loop check."""

    def test_source_uses_broker_for_loop_check(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        source = inspect.getsource(
            IntelligentMemoryOptimizer._close_high_memory_applications
        )
        assert "_get_memory_percent_from_broker" in source


# ---------------------------------------------------------------------------
# Site 8: get_suggestions status reporting
# ---------------------------------------------------------------------------

class TestGetSuggestionsStatusReporting:
    """Verify MemoryOptimizationAPI.get_suggestions uses broker for reporting."""

    @pytest.mark.asyncio
    async def test_uses_broker_when_active(self):
        from backend.memory.intelligent_memory_optimizer import MemoryOptimizationAPI
        api = MemoryOptimizationAPI()
        broker = _mock_broker()
        total = 16 * (1024 ** 3)
        free = int(total * 0.25)  # 75% used
        snap = _mock_snapshot(physical_total=total, physical_free=free)
        broker.latest_snapshot = snap
        api.optimizer.register_with_broker(broker)

        with patch("backend.memory.intelligent_memory_optimizer.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 99.0
            mock_psutil.virtual_memory.return_value = mock_mem
            # Stub process_iter for get_optimization_suggestions
            mock_psutil.process_iter.return_value = []

            result = await api.get_suggestions()

        assert abs(result["current_memory_percent"] - 75.0) < 0.5

    @pytest.mark.asyncio
    async def test_falls_back_to_psutil_when_no_broker(self):
        from backend.memory.intelligent_memory_optimizer import MemoryOptimizationAPI
        api = MemoryOptimizationAPI()

        with patch("backend.memory.intelligent_memory_optimizer.psutil") as mock_psutil:
            mock_mem = MagicMock()
            mock_mem.percent = 55.0
            mock_psutil.virtual_memory.return_value = mock_mem
            mock_psutil.process_iter.return_value = []

            result = await api.get_suggestions()

        assert abs(result["current_memory_percent"] - 55.0) < 0.1


# ---------------------------------------------------------------------------
# Design intent tests (source code inspection)
# ---------------------------------------------------------------------------

class TestDesignIntent:
    """Verify the source code contains the expected integration points."""

    def test_source_imports_memory_budget_broker(self):
        import backend.memory.intelligent_memory_optimizer as mod
        source = inspect.getsource(mod)
        assert "memory_budget_broker" in source, (
            "intelligent_memory_optimizer must import from memory_budget_broker"
        )

    def test_source_imports_pressure_tier(self):
        import backend.memory.intelligent_memory_optimizer as mod
        source = inspect.getsource(mod)
        assert "PressureTier" in source, (
            "intelligent_memory_optimizer must import PressureTier"
        )

    def test_source_contains_latest_snapshot(self):
        import backend.memory.intelligent_memory_optimizer as mod
        source = inspect.getsource(mod)
        assert "latest_snapshot" in source, (
            "intelligent_memory_optimizer must read broker.latest_snapshot"
        )

    def test_source_contains_register_with_broker(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        assert hasattr(IntelligentMemoryOptimizer, "register_with_broker"), (
            "IntelligentMemoryOptimizer must have register_with_broker method"
        )

    def test_source_contains_mcp_active_guard(self):
        from backend.memory.intelligent_memory_optimizer import IntelligentMemoryOptimizer
        source = inspect.getsource(IntelligentMemoryOptimizer._get_memory_percent_from_broker)
        assert "_mcp_active" in source, (
            "_get_memory_percent_from_broker must check _mcp_active flag"
        )

    def test_type_checking_import_pattern(self):
        """MemoryBudgetBroker should be imported under TYPE_CHECKING."""
        import backend.memory.intelligent_memory_optimizer as mod
        source = inspect.getsource(mod)
        assert "TYPE_CHECKING" in source
        assert "from backend.core.memory_budget_broker import MemoryBudgetBroker" in source

    def test_pressure_tier_imported_at_module_level(self):
        """PressureTier should be imported at module level (not under TYPE_CHECKING)."""
        import backend.memory.intelligent_memory_optimizer as mod
        source = inspect.getsource(mod)
        # PressureTier import should be BEFORE the TYPE_CHECKING block
        idx_pressure = source.index("from backend.core.memory_types import PressureTier")
        idx_type_checking = source.index("if TYPE_CHECKING:")
        assert idx_pressure < idx_type_checking, (
            "PressureTier must be imported at module level, before TYPE_CHECKING"
        )
