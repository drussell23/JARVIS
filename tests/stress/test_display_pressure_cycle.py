"""Integration test: full pressure shedding and recovery cycle.

Exercises the real MemoryBudgetBroker with a mock PhantomHardwareManager to
validate the complete wiring:

    broker -> pressure observer -> DisplayPressureController -> phantom hardware -> lease amendment

All five tests cover:
    1. Shed from ACTIVE to DEGRADED_1 under CONSTRAINED pressure
    2. One-step invariant (EMERGENCY only steps one level)
    3. Recovery steps up one level when pressure drops
    4. Display events emitted on transition
    5. Lease bytes amended (committed bytes decrease) on degrade
"""
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.memory_types import (
    DisplayState,
    PressureTier,
    BudgetPriority,
    StartupPhase,
)
from backend.core.memory_budget_broker import MemoryBudgetBroker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_quantizer():
    """Return (quantizer_mock, snapshot_mock) with ABUNDANT defaults."""
    q = MagicMock()
    snap = MagicMock()
    snap.pressure_tier = PressureTier.ABUNDANT
    snap.headroom_bytes = 8_000_000_000
    snap.available_budget_bytes = 10_000_000_000
    snap.safety_floor_bytes = 2_000_000_000
    snap.physical_total = 16_000_000_000
    snap.physical_free = 8_000_000_000
    snap.swap_hysteresis_active = False
    snap.thrash_state = MagicMock(value="healthy")
    snap.signal_quality = MagicMock(value="good")
    snap.pressure_trend = MagicMock(value="stable")
    snap.snapshot_id = "snap_test"
    snap.max_age_ms = 5000
    snap.timestamp = 0
    snap.committed_bytes = 0
    q.snapshot = AsyncMock(return_value=snap)
    q.get_committed_bytes = MagicMock(return_value=0)
    return q, snap


def _make_phantom_mgr():
    """Return a mock PhantomHardwareManager with all async methods stubbed."""
    mgr = MagicMock()
    mgr.set_resolution_async = AsyncMock(return_value=True)
    mgr.disconnect_async = AsyncMock(return_value=True)
    mgr.reconnect_async = AsyncMock(return_value=True)
    mgr.get_current_mode_async = AsyncMock(return_value={
        "resolution": "1920x1080", "connected": True,
    })
    mgr.preferred_resolution = "1920x1080"
    return mgr


def _snap(tier, thrash="healthy", swap_hyst=False, trend="stable",
          free=8_000_000_000):
    """Build a lightweight mock snapshot for pressure observer calls."""
    s = MagicMock()
    s.pressure_tier = tier
    s.thrash_state = MagicMock(value=thrash)
    s.swap_hysteresis_active = swap_hyst
    s.pressure_trend = MagicMock(value=trend)
    s.physical_free = free
    s.snapshot_id = f"snap_{time.monotonic()}"
    s.available_budget_bytes = free
    s.headroom_bytes = free
    return s


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


class TestFullSheddingCycle:
    """End-to-end integration tests for DisplayPressureController."""

    @pytest.mark.asyncio
    async def test_shed_active_to_degraded_1(self):
        """ACTIVE should degrade to DEGRADED_1 under CONSTRAINED pressure."""
        from backend.system.phantom_hardware_manager import (
            DisplayPressureController,
        )

        q, _ = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        mgr = _make_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)

        # Eliminate timing-based guards so transition fires immediately.
        ctrl._verify_window_s = 0
        ctrl._degrade_dwell_s = 0
        ctrl._cooldown_s = 0

        # Acquire and commit a display lease.
        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=32_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        await grant.commit(32_000_000)
        await ctrl.activate(grant.lease_id, "1920x1080")
        assert ctrl.state == DisplayState.ACTIVE

        # Bypass dwell timer.
        ctrl._last_transition_time = 0

        # Trigger CONSTRAINED pressure -- verify reports the new resolution.
        snap = _snap(PressureTier.CONSTRAINED)
        mgr.get_current_mode_async = AsyncMock(return_value={
            "resolution": "1600x900", "connected": True,
        })
        await ctrl._on_pressure_change(PressureTier.CONSTRAINED, snap)
        assert ctrl.state == DisplayState.DEGRADED_1

    @pytest.mark.asyncio
    async def test_one_step_invariant_enforced(self):
        """Even under EMERGENCY pressure, only one shed step should occur."""
        from backend.system.phantom_hardware_manager import (
            DisplayPressureController,
        )

        q, _ = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        mgr = _make_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)

        ctrl._verify_window_s = 0
        ctrl._degrade_dwell_s = 0
        ctrl._cooldown_s = 0

        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=32_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        await grant.commit(32_000_000)
        await ctrl.activate(grant.lease_id, "1920x1080")
        ctrl._last_transition_time = 0

        snap = _snap(PressureTier.EMERGENCY)
        mgr.get_current_mode_async = AsyncMock(return_value={
            "resolution": "1600x900", "connected": True,
        })
        await ctrl._on_pressure_change(PressureTier.EMERGENCY, snap)
        # Must be DEGRADED_1, NOT DISCONNECTED — one step only.
        assert ctrl.state == DisplayState.DEGRADED_1

    @pytest.mark.asyncio
    async def test_recovery_steps_up_one_level(self):
        """Recovery from DEGRADED_2 under OPTIMAL pressure should reach
        DEGRADED_1 (one step up), not ACTIVE."""
        from backend.system.phantom_hardware_manager import (
            DisplayPressureController,
        )

        q, _ = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        mgr = _make_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)

        ctrl._verify_window_s = 0
        ctrl._recovery_dwell_s = 0
        ctrl._cooldown_s = 0

        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=14_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        await grant.commit(14_000_000)
        await ctrl.activate(grant.lease_id, "1280x720")

        # Force into DEGRADED_2 state for recovery test.
        ctrl._state = DisplayState.DEGRADED_2
        ctrl._last_transition_time = 0

        snap = _snap(PressureTier.OPTIMAL, trend="falling")
        mgr.get_current_mode_async = AsyncMock(return_value={
            "resolution": "1600x900", "connected": True,
        })
        await ctrl._on_pressure_change(PressureTier.OPTIMAL, snap)
        # One step up only.
        assert ctrl.state == DisplayState.DEGRADED_1

    @pytest.mark.asyncio
    async def test_events_emitted_on_transition(self):
        """At least two display events (requested + success) should be
        logged in the broker event log on a successful degradation."""
        from backend.system.phantom_hardware_manager import (
            DisplayPressureController,
        )

        q, _ = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        mgr = _make_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)

        ctrl._verify_window_s = 0
        ctrl._degrade_dwell_s = 0
        ctrl._cooldown_s = 0

        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=32_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        await grant.commit(32_000_000)
        await ctrl.activate(grant.lease_id, "1920x1080")
        ctrl._last_transition_time = 0

        # Clear the event log so we only capture transition events.
        broker._event_log.clear()

        snap = _snap(PressureTier.CONSTRAINED)
        mgr.get_current_mode_async = AsyncMock(return_value={
            "resolution": "1600x900", "connected": True,
        })
        await ctrl._on_pressure_change(PressureTier.CONSTRAINED, snap)

        display_events = [
            e for e in broker._event_log
            if e["type"].startswith("display_")
        ]
        # Expect at least: DISPLAY_DEGRADE_REQUESTED + DISPLAY_DEGRADED
        assert len(display_events) >= 2

    @pytest.mark.asyncio
    async def test_lease_bytes_amended_on_degrade(self):
        """Committed bytes in the broker should decrease after degradation
        because the lease is amended to a lower resolution estimate."""
        from backend.system.phantom_hardware_manager import (
            DisplayPressureController,
        )

        q, _ = _make_quantizer()
        broker = MemoryBudgetBroker(q, epoch=1)
        broker._phase = StartupPhase.RUNTIME_INTERACTIVE
        mgr = _make_phantom_mgr()
        ctrl = DisplayPressureController(mgr, broker)

        ctrl._verify_window_s = 0
        ctrl._degrade_dwell_s = 0
        ctrl._cooldown_s = 0

        grant = await broker.request(
            component="display:ghost@v1",
            bytes_requested=32_000_000,
            priority=BudgetPriority.BOOT_OPTIONAL,
            phase=StartupPhase.BOOT_OPTIONAL,
        )
        await grant.commit(32_000_000)
        initial_committed = broker.get_committed_bytes()
        await ctrl.activate(grant.lease_id, "1920x1080")
        ctrl._last_transition_time = 0

        snap = _snap(PressureTier.CONSTRAINED)
        mgr.get_current_mode_async = AsyncMock(return_value={
            "resolution": "1600x900", "connected": True,
        })
        await ctrl._on_pressure_change(PressureTier.CONSTRAINED, snap)

        # Committed bytes must be strictly lower after degradation.
        new_committed = broker.get_committed_bytes()
        assert new_committed < initial_committed
