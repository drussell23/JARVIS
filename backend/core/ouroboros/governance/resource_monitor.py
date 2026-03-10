# backend/core/ouroboros/governance/resource_monitor.py
"""
Resource Monitor — Pressure Signal Collection
===============================================

Collects system resource signals (RAM, CPU, event loop latency, disk IO)
and exposes a :class:`ResourceSnapshot` with an ``overall_pressure`` level.

Used by :class:`RoutingPolicy` and :class:`DegradationController` to make
deterministic decisions about task routing and autonomy mode transitions.

Pressure Levels::

    NORMAL     All signals within comfortable range
    ELEVATED   One or more signals approaching limits
    CRITICAL   System under significant stress
    EMERGENCY  Imminent resource exhaustion
"""

from __future__ import annotations

import enum
import logging
import os
import platform
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.ResourceMonitor")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PressureLevel(enum.IntEnum):
    """System resource pressure classification."""

    NORMAL = 0
    ELEVATED = 1
    CRITICAL = 2
    EMERGENCY = 3


# ---------------------------------------------------------------------------
# Thresholds (configurable via environment)
# ---------------------------------------------------------------------------

PRESSURE_THRESHOLDS: Dict[str, float] = {
    "ram_elevated": float(os.environ.get("OUROBOROS_RAM_ELEVATED_PCT", "80")),
    "ram_critical": float(os.environ.get("OUROBOROS_RAM_CRITICAL_PCT", "85")),
    "ram_emergency": float(os.environ.get("OUROBOROS_RAM_EMERGENCY_PCT", "90")),
    "cpu_elevated": float(os.environ.get("OUROBOROS_CPU_ELEVATED_PCT", "70")),
    "cpu_critical": float(os.environ.get("OUROBOROS_CPU_CRITICAL_PCT", "80")),
    "cpu_emergency": float(os.environ.get("OUROBOROS_CPU_EMERGENCY_PCT", "95")),
    "latency_elevated_ms": float(os.environ.get("OUROBOROS_LATENCY_ELEVATED_MS", "40")),
    "latency_critical_ms": float(os.environ.get("OUROBOROS_LATENCY_CRITICAL_MS", "100")),
}


def _cpu_emergency_for_load(active_ops: int) -> float:
    """Scale CPU emergency threshold by active operation count.

    Higher load → higher threshold before declaring EMERGENCY.
    Prevents false-positive shutdown when concurrent ops legitimately drive CPU high.

    active_ops 0-1 → base threshold (95%)
    active_ops 2-5 → base + 2% (97%)
    active_ops >5  → base + 4% (99%)
    """
    base = PRESSURE_THRESHOLDS["cpu_emergency"]
    if active_ops > 5:
        return min(99.0, base + 4.0)
    if active_ops >= 2:
        return min(99.0, base + 2.0)
    return base


# ---------------------------------------------------------------------------
# ResourceSnapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceSnapshot:
    """Immutable snapshot of system resource state."""

    ram_percent: float
    cpu_percent: float
    event_loop_latency_ms: float
    disk_io_busy: bool
    sampled_monotonic_ns: int = 0          # set by snapshot(); enables age computation
    ram_available_gb: float = 0.0          # psutil.virtual_memory().available / 1e9, quantized
    platform_arch: str = ""                # platform.machine()
    collector_status: str = "ok"           # "ok" if psutil fully available, "partial" otherwise

    @property
    def overall_pressure(self) -> PressureLevel:
        """Compute overall pressure level from all signals.

        Returns the highest pressure level triggered by any signal.
        """
        level = PressureLevel.NORMAL

        # RAM pressure
        if self.ram_percent >= PRESSURE_THRESHOLDS["ram_emergency"]:
            level = max(level, PressureLevel.EMERGENCY)
        elif self.ram_percent >= PRESSURE_THRESHOLDS["ram_critical"]:
            level = max(level, PressureLevel.CRITICAL)
        elif self.ram_percent >= PRESSURE_THRESHOLDS["ram_elevated"]:
            level = max(level, PressureLevel.ELEVATED)

        # CPU pressure
        if self.cpu_percent >= PRESSURE_THRESHOLDS["cpu_emergency"]:
            level = max(level, PressureLevel.EMERGENCY)
        elif self.cpu_percent >= PRESSURE_THRESHOLDS["cpu_critical"]:
            level = max(level, PressureLevel.CRITICAL)
        elif self.cpu_percent >= PRESSURE_THRESHOLDS["cpu_elevated"]:
            level = max(level, PressureLevel.ELEVATED)

        # Event loop latency
        if self.event_loop_latency_ms >= PRESSURE_THRESHOLDS["latency_critical_ms"]:
            level = max(level, PressureLevel.CRITICAL)
        elif self.event_loop_latency_ms >= PRESSURE_THRESHOLDS["latency_elevated_ms"]:
            level = max(level, PressureLevel.ELEVATED)

        # Disk IO
        if self.disk_io_busy:
            level = max(level, PressureLevel.ELEVATED)

        return level

    def pressure_for_load(self, active_ops: int) -> "PressureLevel":
        """Compute pressure level with CPU emergency threshold scaled by active op count.

        Use instead of ``overall_pressure`` when making routing/shutdown decisions
        during high concurrent load to prevent false-positive EMERGENCY classification.

        Parameters
        ----------
        active_ops:
            Number of operations currently in-flight (e.g. ``len(svc._active_ops)``).
        """
        level = PressureLevel.NORMAL

        # RAM pressure (same as overall_pressure — no scaling for RAM)
        if self.ram_percent >= PRESSURE_THRESHOLDS["ram_emergency"]:
            level = max(level, PressureLevel.EMERGENCY)
        elif self.ram_percent >= PRESSURE_THRESHOLDS["ram_critical"]:
            level = max(level, PressureLevel.CRITICAL)
        elif self.ram_percent >= PRESSURE_THRESHOLDS["ram_elevated"]:
            level = max(level, PressureLevel.ELEVATED)

        # CPU pressure — threshold scaled by load
        cpu_emergency = _cpu_emergency_for_load(active_ops)
        if self.cpu_percent >= cpu_emergency:
            level = max(level, PressureLevel.EMERGENCY)
        elif self.cpu_percent >= PRESSURE_THRESHOLDS["cpu_critical"]:
            level = max(level, PressureLevel.CRITICAL)
        elif self.cpu_percent >= PRESSURE_THRESHOLDS["cpu_elevated"]:
            level = max(level, PressureLevel.ELEVATED)

        # Event loop latency (no scaling)
        if self.event_loop_latency_ms >= PRESSURE_THRESHOLDS["latency_critical_ms"]:
            level = max(level, PressureLevel.CRITICAL)
        elif self.event_loop_latency_ms >= PRESSURE_THRESHOLDS["latency_elevated_ms"]:
            level = max(level, PressureLevel.ELEVATED)

        # Disk IO
        if self.disk_io_busy:
            level = max(level, PressureLevel.ELEVATED)

        return level


# ---------------------------------------------------------------------------
# ResourceMonitor
# ---------------------------------------------------------------------------


class ResourceMonitor:
    """Collects system resource signals for governance decisions."""

    def __init__(self) -> None:
        self._last_snapshot: Optional[ResourceSnapshot] = None
        self._last_snapshot_time: float = 0.0

    async def snapshot(
        self,
        ram_override: Optional[float] = None,
        cpu_override: Optional[float] = None,
        latency_override: Optional[float] = None,
        io_override: Optional[bool] = None,
    ) -> ResourceSnapshot:
        """Collect a resource snapshot with all floats quantized to 2dp."""
        ram = ram_override if ram_override is not None else self._get_ram_percent()
        cpu = cpu_override if cpu_override is not None else self._get_cpu_percent()
        latency = latency_override if latency_override is not None else await self._get_event_loop_latency()
        io_busy = io_override if io_override is not None else False

        snap = ResourceSnapshot(
            ram_percent=round(ram, 2),
            cpu_percent=round(cpu, 2),
            event_loop_latency_ms=round(latency, 2),
            disk_io_busy=io_busy,
            sampled_monotonic_ns=time.monotonic_ns(),
            ram_available_gb=round(self._get_ram_available_gb(), 2),
            platform_arch=self._get_platform_arch(),
            collector_status=self._get_collector_status(),
        )
        self._last_snapshot = snap
        self._last_snapshot_time = time.monotonic()
        return snap

    def _get_ram_percent(self) -> float:
        """Get current RAM usage percentage."""
        try:
            import psutil
            return psutil.virtual_memory().percent
        except ImportError:
            return 0.0

    def _get_cpu_percent(self) -> float:
        """Get current CPU usage percentage."""
        try:
            import psutil
            return psutil.cpu_percent(interval=None)
        except ImportError:
            return 0.0

    async def _get_event_loop_latency(self) -> float:
        """Measure async event loop latency in milliseconds."""
        import asyncio
        start = time.monotonic()
        await asyncio.sleep(0)
        return (time.monotonic() - start) * 1000

    def _get_ram_available_gb(self) -> float:
        """Get available RAM in gigabytes."""
        try:
            import psutil
            return psutil.virtual_memory().available / 1e9
        except ImportError:
            return 0.0

    def _get_platform_arch(self) -> str:
        """Get CPU architecture string."""
        return platform.machine()

    def _get_collector_status(self) -> str:
        """Return 'ok' if psutil is importable, 'partial' otherwise."""
        try:
            import psutil  # noqa: F401
            return "ok"
        except ImportError:
            return "partial"
