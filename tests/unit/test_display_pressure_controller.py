"""Tests for DisplayPressureController state machine and shedding ladder."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.memory_types import (
    DisplayState, PressureTier, BudgetPriority, StartupPhase,
    MemoryBudgetEventType,
)


def _mock_broker(tier=PressureTier.ABUNDANT):
    broker = MagicMock()
    broker.register_pressure_observer = MagicMock()
    broker.unregister_pressure_observer = MagicMock()
    broker.get_active_leases = MagicMock(return_value=[])
    broker.get_committed_bytes = MagicMock(return_value=0)
    broker.amend_lease_bytes = AsyncMock()
    broker._emit_event = MagicMock()
    broker._epoch = 1
    broker.current_phase = StartupPhase.RUNTIME_INTERACTIVE
    grant = MagicMock()
    grant.lease_id = "lease_display_001"
    grant.granted_bytes = 32_000_000
    grant.state = MagicMock(is_terminal=False)
    broker.request = AsyncMock(return_value=grant)
    broker.commit = AsyncMock()
    broker.release = AsyncMock()
    return broker, grant


def _mock_phantom_mgr():
    mgr = MagicMock()
    mgr.set_resolution_async = AsyncMock(return_value=True)
    mgr.disconnect_async = AsyncMock(return_value=True)
    mgr.reconnect_async = AsyncMock(return_value=True)
    mgr.get_current_mode_async = AsyncMock(return_value={
        "resolution": "1920x1080", "connected": True, "raw_output": ""
    })
    mgr.preferred_resolution = "1920x1080"
    return mgr


def _mock_snapshot(tier=PressureTier.ABUNDANT, thrash="healthy",
                   available=8_000_000_000, swap_hyst=False, trend="stable"):
    snap = MagicMock()
    snap.pressure_tier = tier
    snap.thrash_state = MagicMock(value=thrash)
    snap.available_budget_bytes = available
    snap.headroom_bytes = available
    snap.physical_free = available
    snap.swap_hysteresis_active = swap_hyst
    snap.pressure_trend = MagicMock(value=trend)
    snap.snapshot_id = f"snap_{time.monotonic()}"
    snap.timestamp = time.time()
    return snap


class TestDisplayPressureControllerInit:
    def test_initial_state_inactive(self):
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        assert ctrl.state == DisplayState.INACTIVE

    def test_registers_as_pressure_observer(self):
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        broker.register_pressure_observer.assert_called_once()


class TestSheddingLadder:
    @pytest.mark.asyncio
    async def test_constrained_triggers_degrade_1(self):
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        ctrl._state = DisplayState.ACTIVE
        ctrl._lease_id = "lease_001"
        ctrl._current_resolution = "1920x1080"
        ctrl._last_transition_time = 0

        snap = _mock_snapshot(tier=PressureTier.CONSTRAINED)
        target = ctrl._compute_target_state(snap)
        assert target == DisplayState.DEGRADED_1

    @pytest.mark.asyncio
    async def test_critical_triggers_degrade_2(self):
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        ctrl._state = DisplayState.DEGRADED_1
        ctrl._lease_id = "lease_001"
        ctrl._current_resolution = "1600x900"
        ctrl._last_transition_time = 0

        snap = _mock_snapshot(tier=PressureTier.CRITICAL)
        target = ctrl._compute_target_state(snap)
        assert target == DisplayState.DEGRADED_2

    @pytest.mark.asyncio
    async def test_emergency_triggers_disconnect(self):
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        ctrl._state = DisplayState.MINIMUM
        ctrl._lease_id = "lease_001"
        ctrl._current_resolution = "1024x576"
        ctrl._last_transition_time = 0

        snap = _mock_snapshot(tier=PressureTier.EMERGENCY)
        target = ctrl._compute_target_state(snap)
        assert target == DisplayState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_one_step_per_evaluation(self):
        """EMERGENCY from ACTIVE should NOT jump straight to DISCONNECTED."""
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        ctrl._state = DisplayState.ACTIVE
        ctrl._lease_id = "lease_001"
        ctrl._current_resolution = "1920x1080"
        ctrl._last_transition_time = 0

        snap = _mock_snapshot(tier=PressureTier.EMERGENCY)
        target = ctrl._compute_target_state(snap)
        assert target == DisplayState.DEGRADED_1


class TestFlapGuards:
    @pytest.mark.asyncio
    async def test_dwell_prevents_rapid_transition(self):
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        ctrl._state = DisplayState.ACTIVE
        ctrl._lease_id = "lease_001"
        ctrl._current_resolution = "1920x1080"
        ctrl._last_transition_time = time.monotonic()

        snap = _mock_snapshot(tier=PressureTier.CONSTRAINED)
        target = ctrl._compute_target_state(snap)
        assert target is None or target == DisplayState.ACTIVE

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_after_max(self):
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        ctrl._state = DisplayState.ACTIVE
        ctrl._lease_id = "lease_001"
        ctrl._transition_timestamps = [time.monotonic()] * 6

        snap = _mock_snapshot(tier=PressureTier.CONSTRAINED)
        target = ctrl._compute_target_state(snap)
        assert target is None


class TestDependencyAwareDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_blocked_by_requires_display(self):
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        dep_lease = MagicMock()
        dep_lease.metadata = {"requires_display": True}
        dep_lease.component_id = "vision:capture@v1"
        dep_lease.state = MagicMock(is_terminal=False)
        broker.get_active_leases = MagicMock(return_value=[dep_lease])

        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        ctrl._state = DisplayState.MINIMUM
        ctrl._lease_id = "lease_001"
        ctrl._last_transition_time = 0

        blocked, reason = ctrl._check_disconnect_dependencies()
        assert blocked is True
        assert "vision:capture@v1" in str(reason)


class TestRecovery:
    @pytest.mark.asyncio
    async def test_recovery_from_disconnected_goes_to_minimum(self):
        """Recovery from DISCONNECTED must reconnect at MINIMUM, not ACTIVE."""
        from backend.system.phantom_hardware_manager import DisplayPressureController
        broker, grant = _mock_broker()
        mgr = _mock_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)
        ctrl._state = DisplayState.DISCONNECTED
        ctrl._lease_id = None
        ctrl._last_transition_time = 0

        snap = _mock_snapshot(
            tier=PressureTier.ELEVATED,
            swap_hyst=False,
            trend="falling",
        )
        target = ctrl._compute_recovery_target(snap)
        assert target == DisplayState.MINIMUM
