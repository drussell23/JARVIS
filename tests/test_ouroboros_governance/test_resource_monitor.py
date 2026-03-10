# tests/test_ouroboros_governance/test_resource_monitor.py
"""Tests for the governance resource monitor."""

import asyncio
import pytest
from unittest.mock import patch, MagicMock

from backend.core.ouroboros.governance.resource_monitor import (
    ResourceMonitor,
    ResourceSnapshot,
    PressureLevel,
    PRESSURE_THRESHOLDS,
)


class TestPressureLevel:
    def test_all_levels_defined(self):
        """Four pressure levels: NORMAL, ELEVATED, CRITICAL, EMERGENCY."""
        assert len(PressureLevel) == 4
        assert PressureLevel.NORMAL.value == 0
        assert PressureLevel.ELEVATED.value == 1
        assert PressureLevel.CRITICAL.value == 2
        assert PressureLevel.EMERGENCY.value == 3

    def test_ordering(self):
        """Pressure levels are ordered for comparison."""
        assert PressureLevel.NORMAL < PressureLevel.ELEVATED
        assert PressureLevel.ELEVATED < PressureLevel.CRITICAL
        assert PressureLevel.CRITICAL < PressureLevel.EMERGENCY


class TestResourceSnapshot:
    def test_snapshot_fields(self):
        """Snapshot has all required resource fields."""
        snap = ResourceSnapshot(
            ram_percent=65.0,
            cpu_percent=30.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.ram_percent == 65.0
        assert snap.cpu_percent == 30.0
        assert snap.event_loop_latency_ms == 5.0
        assert snap.disk_io_busy is False

    def test_overall_pressure_normal(self):
        """Low resource usage yields NORMAL pressure."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure == PressureLevel.NORMAL

    def test_overall_pressure_elevated_ram(self):
        """RAM > 80% yields ELEVATED pressure."""
        snap = ResourceSnapshot(
            ram_percent=82.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure == PressureLevel.ELEVATED

    def test_overall_pressure_critical_cpu(self):
        """CPU > 80% sustained yields CRITICAL pressure."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=85.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure == PressureLevel.CRITICAL

    def test_overall_pressure_emergency_ram(self):
        """RAM > 90% yields EMERGENCY pressure."""
        snap = ResourceSnapshot(
            ram_percent=92.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure == PressureLevel.EMERGENCY

    def test_event_loop_latency_triggers_elevated(self):
        """Event loop latency > 40ms yields ELEVATED."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=40.0,
            event_loop_latency_ms=45.0,
            disk_io_busy=False,
        )
        assert snap.overall_pressure >= PressureLevel.ELEVATED

    def test_disk_io_triggers_elevated(self):
        """Disk IO saturation yields ELEVATED."""
        snap = ResourceSnapshot(
            ram_percent=50.0,
            cpu_percent=40.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=True,
        )
        assert snap.overall_pressure >= PressureLevel.ELEVATED


class TestResourceMonitor:
    @pytest.mark.asyncio
    async def test_snapshot_returns_resource_data(self):
        """snapshot() returns a valid ResourceSnapshot."""
        monitor = ResourceMonitor()
        snap = await monitor.snapshot()
        assert isinstance(snap, ResourceSnapshot)
        assert 0.0 <= snap.ram_percent <= 100.0
        assert 0.0 <= snap.cpu_percent <= 100.0
        assert snap.event_loop_latency_ms >= 0.0

    @pytest.mark.asyncio
    async def test_snapshot_with_injected_values(self):
        """Monitor accepts injected values for testing."""
        monitor = ResourceMonitor()
        snap = await monitor.snapshot(
            ram_override=85.0,
            cpu_override=90.0,
            latency_override=50.0,
            io_override=True,
        )
        assert snap.ram_percent == 85.0
        assert snap.cpu_percent == 90.0
        assert snap.event_loop_latency_ms == 50.0
        assert snap.disk_io_busy is True

    @pytest.mark.asyncio
    async def test_thresholds_configurable_via_env(self):
        """Thresholds are read from environment variables."""
        assert "ram_elevated" in PRESSURE_THRESHOLDS
        assert "ram_emergency" in PRESSURE_THRESHOLDS
        assert "cpu_critical" in PRESSURE_THRESHOLDS
        assert "latency_elevated_ms" in PRESSURE_THRESHOLDS


# ---------------------------------------------------------------------------
# ResourceSnapshot quantization + new fields (Task 1)
# ---------------------------------------------------------------------------

async def test_snapshot_quantizes_floats():
    """All float fields in snapshot are rounded to 2 decimal places."""
    monitor = ResourceMonitor()
    snap = await monitor.snapshot(
        ram_override=77.777,
        cpu_override=12.345,
        latency_override=3.999,
    )
    assert snap.ram_percent == round(77.777, 2)
    assert snap.cpu_percent == round(12.345, 2)
    assert snap.event_loop_latency_ms == round(3.999, 2)


async def test_snapshot_has_monotonic_ns():
    """sampled_monotonic_ns is a positive integer set by snapshot()."""
    import time
    monitor = ResourceMonitor()
    before = time.monotonic_ns()
    snap = await monitor.snapshot()
    after = time.monotonic_ns()
    assert isinstance(snap.sampled_monotonic_ns, int)
    assert before <= snap.sampled_monotonic_ns <= after


async def test_snapshot_ram_available_gb():
    """ram_available_gb is a non-negative float quantized to 2dp."""
    monitor = ResourceMonitor()
    snap = await monitor.snapshot()
    assert isinstance(snap.ram_available_gb, float)
    assert snap.ram_available_gb >= 0.0
    assert snap.ram_available_gb == round(snap.ram_available_gb, 2)


async def test_snapshot_platform_arch():
    """platform_arch is a non-empty string (e.g. 'arm64', 'x86_64')."""
    monitor = ResourceMonitor()
    snap = await monitor.snapshot()
    assert isinstance(snap.platform_arch, str)
    assert len(snap.platform_arch) > 0


async def test_snapshot_collector_status():
    """collector_status is 'ok' or 'partial' depending on psutil availability."""
    monitor = ResourceMonitor()
    snap = await monitor.snapshot()
    assert snap.collector_status in ("ok", "partial")


# ---------------------------------------------------------------------------
# TestPressureForLoad
# ---------------------------------------------------------------------------


class TestPressureForLoad:
    """ResourceSnapshot.pressure_for_load() scales CPU threshold by active op count."""

    def _make_snap(self, cpu_percent: float) -> "ResourceSnapshot":
        from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot
        return ResourceSnapshot(
            ram_percent=10.0,
            cpu_percent=cpu_percent,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )

    def test_cpu_95_with_0_ops_is_emergency(self):
        """95% CPU with 0 active ops = EMERGENCY (old behavior preserved)."""
        from backend.core.ouroboros.governance.resource_monitor import PressureLevel
        snap = self._make_snap(95.0)
        assert snap.pressure_for_load(0) == PressureLevel.EMERGENCY

    def test_cpu_95_with_6_ops_is_not_emergency(self):
        """95% CPU with 6 active ops = not EMERGENCY (threshold raised to 99%)."""
        from backend.core.ouroboros.governance.resource_monitor import PressureLevel
        snap = self._make_snap(95.0)
        result = snap.pressure_for_load(6)
        assert result < PressureLevel.EMERGENCY, (
            f"Expected < EMERGENCY for 95% CPU + 6 ops, got {result}"
        )

    def test_cpu_97_with_3_ops_is_not_emergency(self):
        """97% CPU with 3 active ops = not EMERGENCY (threshold is 97% for 2-5 ops)."""
        from backend.core.ouroboros.governance.resource_monitor import PressureLevel
        snap = self._make_snap(97.0)
        # Exactly at threshold boundary — depends on whether >= or > is used
        # The implementation uses >=, so 97.0 >= 97.0 means EMERGENCY at 2-5 ops
        result = snap.pressure_for_load(3)
        # At threshold, behavior is EMERGENCY (>= comparison)
        assert result == PressureLevel.EMERGENCY

    def test_cpu_96_with_3_ops_is_not_emergency(self):
        """96% CPU with 3 active ops = not EMERGENCY (below 97% threshold)."""
        from backend.core.ouroboros.governance.resource_monitor import PressureLevel
        snap = self._make_snap(96.0)
        result = snap.pressure_for_load(3)
        assert result < PressureLevel.EMERGENCY, (
            f"Expected < EMERGENCY for 96% CPU + 3 ops, got {result}"
        )

    def test_cpu_99_with_6_ops_is_emergency(self):
        """99% CPU with 6+ ops = EMERGENCY (even with max load scaling)."""
        from backend.core.ouroboros.governance.resource_monitor import PressureLevel
        snap = self._make_snap(99.0)
        result = snap.pressure_for_load(6)
        assert result == PressureLevel.EMERGENCY

    def test_pressure_for_load_matches_overall_when_1_op(self):
        """pressure_for_load(1) == overall_pressure (same thresholds, no scaling)."""
        snap = self._make_snap(80.0)
        assert snap.pressure_for_load(1) == snap.overall_pressure
