#!/usr/bin/env python3
"""
macOS Memory Pressure Manager for JARVIS Vision System

Implements macOS-native memory pressure monitoring and adaptive memory management.
Follows macOS philosophy: respond to memory pressure, not arbitrary thresholds.

Key Concepts:
- macOS uses memory pressure (not percentage) to manage resources
- Green pressure: plenty of memory available
- Yellow pressure: system is managing memory actively
- Red pressure: critical - system is struggling
- Respond dynamically to pressure changes, not fixed limits
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import psutil

logger = logging.getLogger(__name__)


class MemoryPressure(Enum):
    """macOS memory pressure levels"""

    GREEN = "normal"  # No pressure
    YELLOW = "warn"  # Moderate pressure
    RED = "critical"  # Critical pressure
    UNKNOWN = "unknown"


@dataclass
class MemoryConfig:
    """Dynamic memory configuration - NO HARDCODING"""

    # Load from environment or use adaptive defaults
    enable_adaptive_management: bool = field(
        default_factory=lambda: os.getenv("JARVIS_ADAPTIVE_MEMORY", "true").lower() == "true"
    )

    # Cache size limits (MB) - dynamically adjusted based on pressure
    cache_size_green: int = field(
        default_factory=lambda: int(os.getenv("CACHE_SIZE_GREEN", "500"))  # Normal conditions
    )
    cache_size_yellow: int = field(
        default_factory=lambda: int(os.getenv("CACHE_SIZE_YELLOW", "200"))  # Moderate pressure
    )
    cache_size_red: int = field(
        default_factory=lambda: int(os.getenv("CACHE_SIZE_RED", "50"))  # Critical pressure
    )

    # Capture quality per pressure level
    quality_green: str = field(
        default_factory=lambda: os.getenv("CAPTURE_QUALITY_GREEN", "optimized")
    )
    quality_yellow: str = field(default_factory=lambda: os.getenv("CAPTURE_QUALITY_YELLOW", "fast"))
    quality_red: str = field(default_factory=lambda: os.getenv("CAPTURE_QUALITY_RED", "thumbnail"))

    # Monitoring intervals (seconds)
    pressure_check_interval: int = field(
        default_factory=lambda: int(os.getenv("PRESSURE_CHECK_INTERVAL", "5"))
    )

    # Active/inactive space strategy
    monitor_active_only_on_pressure: bool = field(
        default_factory=lambda: os.getenv("MONITOR_ACTIVE_ONLY_PRESSURE", "true").lower() == "true"
    )

    # Monitored spaces (comma-separated list, empty = all)
    monitored_spaces: List[int] = field(default_factory=list)

    def __post_init__(self):
        """Parse monitored spaces from environment"""
        spaces_env = os.getenv("MONITORED_SPACES", "")
        if spaces_env:
            try:
                self.monitored_spaces = [int(s.strip()) for s in spaces_env.split(",")]
            except ValueError:
                logger.warning(f"Invalid MONITORED_SPACES format: {spaces_env}")


@dataclass
class MemoryStats:
    """Current memory statistics"""

    pressure: MemoryPressure
    total_gb: float
    available_gb: float
    used_percent: float
    app_memory_gb: float
    wired_memory_gb: float
    compressed_gb: float
    swap_used_gb: float
    timestamp: datetime = field(default_factory=datetime.now)


class MacOSMemoryManager:
    """
    macOS-native memory pressure manager.
    Adapts JARVIS behavior to system memory conditions.
    """

    def __init__(self, config: Optional[MemoryConfig] = None):
        self.config = config or MemoryConfig()
        self.current_pressure = MemoryPressure.UNKNOWN
        self.last_stats: Optional[MemoryStats] = None
        self._monitoring_task: Optional[asyncio.Task] = None
        self._pressure_callbacks: List[Callable] = []
        self._lock = asyncio.Lock()

        logger.info("🧠 macOS Memory Manager initialized")
        logger.info(f"   Adaptive management: {self.config.enable_adaptive_management}")
        logger.info(
            f"   Cache sizes: Green={self.config.cache_size_green}MB, "
            f"Yellow={self.config.cache_size_yellow}MB, "
            f"Red={self.config.cache_size_red}MB"
        )

    async def start_monitoring(self):
        """Start continuous memory pressure monitoring"""
        if self._monitoring_task is None or self._monitoring_task.done():
            self._monitoring_task = asyncio.create_task(self._monitor_loop())
            logger.info("✅ Memory pressure monitoring started")

    async def stop_monitoring(self):
        """Stop memory pressure monitoring"""
        if self._monitoring_task and not self._monitoring_task.done():
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            logger.info("🛑 Memory pressure monitoring stopped")

    async def _monitor_loop(self):
        """Continuous monitoring loop"""
        max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()
        while time.monotonic() - session_start < max_runtime:
            try:
                await self.check_pressure()
                await asyncio.sleep(self.config.pressure_check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Memory monitoring error: {e}")
                await asyncio.sleep(self.config.pressure_check_interval)
        else:
            logger.info("Memory pressure monitoring session timeout, stopping")

    async def check_pressure(self) -> MemoryPressure:
        """
        Check current memory pressure using macOS native tools.
        Returns pressure level and updates internal state.
        """
        try:
            # Get memory stats from macOS
            stats = await self._get_memory_stats()

            # Determine pressure level
            old_pressure = self.current_pressure
            self.current_pressure = self._calculate_pressure(stats)
            self.last_stats = stats

            # Notify if pressure changed
            if old_pressure != self.current_pressure:
                await self._notify_pressure_change(old_pressure, self.current_pressure, stats)

            return self.current_pressure

        except Exception as e:
            logger.error(f"Failed to check memory pressure: {e}")
            return MemoryPressure.UNKNOWN

    async def _get_memory_stats(self) -> MemoryStats:
        """Get memory statistics from macOS"""
        try:
            # Use psutil for cross-platform compatibility
            vm = psutil.virtual_memory()
            swap = psutil.swap_memory()

            # Try to get macOS-specific memory pressure
            pressure_str = await self._get_macos_pressure_level()

            # Parse into MemoryPressure enum
            if "critical" in pressure_str.lower() or "red" in pressure_str.lower():
                pressure = MemoryPressure.RED
            elif "warn" in pressure_str.lower() or "yellow" in pressure_str.lower():
                pressure = MemoryPressure.YELLOW
            elif "normal" in pressure_str.lower() or "green" in pressure_str.lower():
                pressure = MemoryPressure.GREEN
            else:
                # Fallback: calculate from percentages
                if vm.percent >= 90:
                    pressure = MemoryPressure.RED
                elif vm.percent >= 75:
                    pressure = MemoryPressure.YELLOW
                else:
                    pressure = MemoryPressure.GREEN

            return MemoryStats(
                pressure=pressure,
                total_gb=vm.total / (1024**3),
                available_gb=vm.available / (1024**3),
                used_percent=vm.percent,
                app_memory_gb=(vm.used - getattr(vm, "cached", 0)) / (1024**3),
                wired_memory_gb=getattr(vm, "wired", 0) / (1024**3),
                compressed_gb=0,  # Not available via psutil
                swap_used_gb=swap.used / (1024**3),
            )

        except Exception as e:
            logger.error(f"Failed to get memory stats: {e}")
            # Return safe defaults
            return MemoryStats(
                pressure=MemoryPressure.UNKNOWN,
                total_gb=16.0,
                available_gb=8.0,
                used_percent=50.0,
                app_memory_gb=4.0,
                wired_memory_gb=2.0,
                compressed_gb=0.0,
                swap_used_gb=0.0,
            )

    async def _get_macos_pressure_level(self) -> str:
        """Get macOS memory pressure level using native tools"""
        try:
            # Use memory_pressure command (macOS 10.9+)
            proc = await asyncio.create_subprocess_exec(
                "memory_pressure",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            output = stdout.decode("utf-8").lower()

            # Parse output for pressure level
            if "critical" in output or "level: 4" in output:
                return "critical"
            elif "warn" in output or "level: 2" in output:
                return "warn"
            else:
                return "normal"

        except (FileNotFoundError, asyncio.TimeoutError):
            # memory_pressure not available, fallback to vm_stat
            return await self._fallback_pressure_check()
        except Exception as e:
            logger.debug(f"memory_pressure check failed: {e}")
            return "unknown"

    async def _fallback_pressure_check(self) -> str:
        """Fallback pressure check using vm_stat"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "vm_stat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            output = stdout.decode("utf-8")

            # Parse vm_stat output (very rough approximation)
            # Real macOS memory pressure is more sophisticated
            if "Pages active:" in output:
                return "normal"  # Simplified logic
            return "unknown"

        except Exception:
            return "unknown"

    def _calculate_pressure(self, stats: MemoryStats) -> MemoryPressure:
        """
        Calculate memory pressure based on stats.
        Uses macOS philosophy: pressure, not percentage.
        """
        # Prioritize native macOS pressure if available
        if stats.pressure != MemoryPressure.UNKNOWN:
            return stats.pressure

        # Fallback calculation (not as good as native)
        # Consider: used%, swap, app memory
        if stats.swap_used_gb > 2.0 or stats.used_percent >= 90:
            return MemoryPressure.RED
        elif stats.swap_used_gb > 0.5 or stats.used_percent >= 75:
            return MemoryPressure.YELLOW
        else:
            return MemoryPressure.GREEN

    async def _notify_pressure_change(
        self, old: MemoryPressure, new: MemoryPressure, stats: MemoryStats
    ):
        """Notify listeners of pressure change"""
        logger.info(f"🔄 Memory pressure changed: {old.value} → {new.value}")
        logger.info(f"   Used: {stats.used_percent:.1f}%, Available: {stats.available_gb:.2f}GB")

        # Trigger callbacks
        for callback in self._pressure_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(new, stats)
                else:
                    callback(new, stats)
            except Exception as e:
                logger.error(f"Pressure callback error: {e}")

    def register_pressure_callback(self, callback: Callable):
        """Register callback for pressure changes"""
        self._pressure_callbacks.append(callback)

    def get_recommended_cache_size(self) -> int:
        """Get recommended cache size based on current pressure"""
        if self.current_pressure == MemoryPressure.GREEN:
            return self.config.cache_size_green
        elif self.current_pressure == MemoryPressure.YELLOW:
            return self.config.cache_size_yellow
        elif self.current_pressure == MemoryPressure.RED:
            return self.config.cache_size_red
        else:
            return self.config.cache_size_yellow  # Safe default

    def get_recommended_quality(self) -> str:
        """Get recommended capture quality based on current pressure"""
        if self.current_pressure == MemoryPressure.GREEN:
            return self.config.quality_green
        elif self.current_pressure == MemoryPressure.YELLOW:
            return self.config.quality_yellow
        elif self.current_pressure == MemoryPressure.RED:
            return self.config.quality_red
        else:
            return self.config.quality_yellow  # Safe default

    def should_monitor_all_spaces(self) -> bool:
        """Determine if all spaces should be monitored based on pressure"""
        if not self.config.enable_adaptive_management:
            return True

        # If configured spaces list exists, use it
        if self.config.monitored_spaces:
            return False

        # Otherwise, adapt based on pressure
        if self.config.monitor_active_only_on_pressure:
            return self.current_pressure == MemoryPressure.GREEN

        return True

    def get_stats_summary(self) -> Dict[str, Any]:
        """Get current memory stats summary"""
        if not self.last_stats:
            return {"status": "not_initialized"}

        return {
            "pressure": self.current_pressure.value,
            "total_gb": round(self.last_stats.total_gb, 2),
            "available_gb": round(self.last_stats.available_gb, 2),
            "used_percent": round(self.last_stats.used_percent, 1),
            "app_memory_gb": round(self.last_stats.app_memory_gb, 2),
            "swap_used_gb": round(self.last_stats.swap_used_gb, 2),
            "recommended_cache_mb": self.get_recommended_cache_size(),
            "recommended_quality": self.get_recommended_quality(),
            "monitor_all_spaces": self.should_monitor_all_spaces(),
            "timestamp": self.last_stats.timestamp.isoformat(),
        }


# Singleton instance
_memory_manager: Optional[MacOSMemoryManager] = None


def get_memory_manager(config: Optional[MemoryConfig] = None) -> MacOSMemoryManager:
    """Get or create singleton memory manager"""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MacOSMemoryManager(config)
    return _memory_manager


async def initialize_memory_manager(config: Optional[MemoryConfig] = None) -> MacOSMemoryManager:
    """Initialize and start memory manager"""
    manager = get_memory_manager(config)
    await manager.start_monitoring()
    return manager
