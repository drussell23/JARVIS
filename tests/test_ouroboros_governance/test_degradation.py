# tests/test_ouroboros_governance/test_degradation.py
"""Tests for the degradation controller."""

import pytest
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.degradation import (
    DegradationController,
    DegradationMode,
    DegradationReason,
    ModeTransition,
)
from backend.core.ouroboros.governance.resource_monitor import (
    ResourceSnapshot,
    PressureLevel,
)


@pytest.fixture
def controller():
    return DegradationController()


class TestDegradationModes:
    def test_all_four_modes_defined(self):
        """Four degradation modes exist."""
        assert len(DegradationMode) == 4
        expected = ["FULL_AUTONOMY", "REDUCED_AUTONOMY", "READ_ONLY_PLANNING", "EMERGENCY_STOP"]
        assert [m.name for m in DegradationMode] == expected

    def test_starts_in_full_autonomy(self, controller):
        """Controller starts in FULL_AUTONOMY."""
        assert controller.mode == DegradationMode.FULL_AUTONOMY


class TestModeTransitions:
    @pytest.mark.asyncio
    async def test_elevated_pressure_reduces_autonomy(self, controller):
        """ELEVATED pressure transitions to REDUCED_AUTONOMY."""
        snap = ResourceSnapshot(
            ram_percent=82.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        transition = await controller.evaluate(snap)
        assert controller.mode == DegradationMode.REDUCED_AUTONOMY
        assert transition is not None
        assert transition.to_mode == DegradationMode.REDUCED_AUTONOMY

    @pytest.mark.asyncio
    async def test_critical_pressure_goes_read_only(self, controller):
        """CRITICAL pressure transitions to READ_ONLY_PLANNING."""
        snap = ResourceSnapshot(
            ram_percent=87.0,
            cpu_percent=85.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        transition = await controller.evaluate(snap)
        assert controller.mode == DegradationMode.READ_ONLY_PLANNING

    @pytest.mark.asyncio
    async def test_emergency_pressure_stops(self, controller):
        """EMERGENCY pressure transitions to EMERGENCY_STOP."""
        snap = ResourceSnapshot(
            ram_percent=95.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        transition = await controller.evaluate(snap)
        assert controller.mode == DegradationMode.EMERGENCY_STOP

    @pytest.mark.asyncio
    async def test_normal_pressure_stays_full(self, controller):
        """NORMAL pressure stays in FULL_AUTONOMY."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        transition = await controller.evaluate(snap)
        assert controller.mode == DegradationMode.FULL_AUTONOMY
        assert transition is None

    @pytest.mark.asyncio
    async def test_recovery_from_reduced_to_full(self, controller):
        """Pressure drop from ELEVATED to NORMAL recovers to FULL_AUTONOMY."""
        high = ResourceSnapshot(ram_percent=82.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(high)
        assert controller.mode == DegradationMode.REDUCED_AUTONOMY

        low = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(low)
        assert controller.mode == DegradationMode.FULL_AUTONOMY

    @pytest.mark.asyncio
    async def test_emergency_stop_requires_explicit_reset(self, controller):
        """EMERGENCY_STOP does not auto-recover; requires explicit reset."""
        high = ResourceSnapshot(ram_percent=95.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(high)
        assert controller.mode == DegradationMode.EMERGENCY_STOP

        low = ResourceSnapshot(ram_percent=30.0, cpu_percent=20.0, event_loop_latency_ms=1.0, disk_io_busy=False)
        await controller.evaluate(low)
        assert controller.mode == DegradationMode.EMERGENCY_STOP  # Still stopped

    @pytest.mark.asyncio
    async def test_explicit_reset_from_emergency(self, controller):
        """explicit_reset() recovers from EMERGENCY_STOP to FULL_AUTONOMY."""
        high = ResourceSnapshot(ram_percent=95.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(high)
        await controller.explicit_reset()
        assert controller.mode == DegradationMode.FULL_AUTONOMY


class TestGCPAvailability:
    @pytest.mark.asyncio
    async def test_gcp_down_reduces_autonomy(self, controller):
        """GCP unavailable triggers REDUCED_AUTONOMY."""
        controller.set_gcp_available(False)
        snap = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(snap)
        assert controller.mode == DegradationMode.REDUCED_AUTONOMY

    @pytest.mark.asyncio
    async def test_gcp_recovery_restores_mode(self, controller):
        """GCP coming back restores FULL_AUTONOMY."""
        controller.set_gcp_available(False)
        snap = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(snap)
        assert controller.mode == DegradationMode.REDUCED_AUTONOMY

        controller.set_gcp_available(True)
        await controller.evaluate(snap)
        assert controller.mode == DegradationMode.FULL_AUTONOMY


class TestRollbackHistory:
    @pytest.mark.asyncio
    async def test_three_rollbacks_triggers_emergency(self, controller):
        """3+ rollbacks in 1 hour triggers EMERGENCY_STOP."""
        controller.record_rollback()
        controller.record_rollback()
        controller.record_rollback()
        snap = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(snap)
        assert controller.mode == DegradationMode.EMERGENCY_STOP


class TestTransitionHistory:
    @pytest.mark.asyncio
    async def test_transitions_are_recorded(self, controller):
        """All mode transitions are stored in history."""
        high = ResourceSnapshot(ram_percent=82.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(high)
        low = ResourceSnapshot(ram_percent=50.0, cpu_percent=40.0, event_loop_latency_ms=5.0, disk_io_busy=False)
        await controller.evaluate(low)

        history = controller.get_transition_history()
        assert len(history) == 2
        assert history[0].from_mode == DegradationMode.FULL_AUTONOMY
        assert history[0].to_mode == DegradationMode.REDUCED_AUTONOMY
        assert history[1].to_mode == DegradationMode.FULL_AUTONOMY


class TestPermissions:
    @pytest.mark.asyncio
    async def test_safe_auto_allowed_in_full_and_reduced(self, controller):
        """SAFE_AUTO tasks allowed in FULL and REDUCED."""
        assert controller.safe_auto_allowed is True
        controller._mode = DegradationMode.REDUCED_AUTONOMY
        assert controller.safe_auto_allowed is True

    @pytest.mark.asyncio
    async def test_heavy_tasks_only_in_full(self, controller):
        """Heavy tasks (multi-file, cross-repo) only in FULL_AUTONOMY."""
        assert controller.heavy_tasks_allowed is True
        controller._mode = DegradationMode.REDUCED_AUTONOMY
        assert controller.heavy_tasks_allowed is False

    @pytest.mark.asyncio
    async def test_no_writes_in_read_only(self, controller):
        """No writes allowed in READ_ONLY_PLANNING."""
        controller._mode = DegradationMode.READ_ONLY_PLANNING
        assert controller.safe_auto_allowed is False
        assert controller.heavy_tasks_allowed is False
