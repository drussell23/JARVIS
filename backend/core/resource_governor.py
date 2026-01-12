"""
Adaptive Resource Governor for JARVIS Memory-Constrained Environments
=====================================================================

v1.0: Defcon-Level Resource Management for 16GB M1 Macs

This governor provides intelligent throttling based on system memory pressure,
using a "Defcon Level" system inspired by military readiness levels:

    DEFCON GREEN  (< 70% memory): Full operations, no throttling
    DEFCON YELLOW (70-85% memory): Reduced operations, throttle OCR/vision
    DEFCON RED    (> 85% memory): Emergency mode, suspend heavy operations

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    ADAPTIVE RESOURCE GOVERNOR                            │
    │                                                                          │
    │  ┌────────────┐    ┌────────────┐    ┌────────────┐                     │
    │  │  DEFCON    │    │  DEFCON    │    │  DEFCON    │                     │
    │  │  GREEN     │───▶│  YELLOW    │───▶│   RED      │                     │
    │  │  < 70%     │    │  70-85%    │    │  > 85%     │                     │
    │  │            │    │            │    │            │                     │
    │  │ Full Ops   │    │ Throttled  │    │ Emergency  │                     │
    │  │ 5 FPS OCR  │    │ 1 FPS OCR  │    │  Suspend   │                     │
    │  └────────────┘    └────────────┘    └────────────┘                     │
    │         ▲                                   │                            │
    │         └───────────────────────────────────┘                            │
    │              (Recovery when < 65%)                                       │
    └─────────────────────────────────────────────────────────────────────────┘

Features:
- Real-time memory pressure monitoring via psutil
- Adaptive throttle rates for frame/OCR processing
- Emergency abort for vision monitoring when critical
- Hysteresis to prevent level oscillation
- macOS-specific optimizations (page outs, memory pressure)
- Integration with existing AdvancedRAMMonitor

Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

# =============================================================================
# Defcon Level System
# =============================================================================

class DefconLevel(Enum):
    """
    Resource readiness levels inspired by military DEFCON system.

    Lower values = higher alert/more restrictions.
    """
    GREEN = auto()   # Normal operations (DEFCON 5 equivalent)
    YELLOW = auto()  # Elevated alert (DEFCON 3 equivalent)
    RED = auto()     # Maximum alert (DEFCON 1 equivalent)

    @property
    def emoji(self) -> str:
        return {
            DefconLevel.GREEN: "🟢",
            DefconLevel.YELLOW: "🟡",
            DefconLevel.RED: "🔴"
        }[self]

    @property
    def description(self) -> str:
        return {
            DefconLevel.GREEN: "Normal operations",
            DefconLevel.YELLOW: "Elevated memory pressure - throttling active",
            DefconLevel.RED: "Critical memory pressure - emergency mode"
        }[self]


@dataclass
class DefconThresholds:
    """
    Configurable thresholds for Defcon level transitions.

    Supports hysteresis to prevent rapid oscillation between levels.
    """
    # Transition UP thresholds (memory usage percent)
    green_to_yellow: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_DEFCON_GREEN_TO_YELLOW", "70"))
    )
    yellow_to_red: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_DEFCON_YELLOW_TO_RED", "85"))
    )

    # Transition DOWN thresholds (with hysteresis buffer)
    red_to_yellow: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_DEFCON_RED_TO_YELLOW", "80"))
    )
    yellow_to_green: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_DEFCON_YELLOW_TO_GREEN", "65"))
    )

    # macOS-specific: Page outs threshold (heavy swapping indicator)
    page_outs_critical: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PAGE_OUTS_CRITICAL", "10000"))
    )

    # Time windows
    stabilization_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_DEFCON_STABILIZATION", "3.0"))
    )


@dataclass
class ThrottleSettings:
    """
    Throttle settings for each Defcon level.

    Controls how aggressively operations are throttled.
    """
    # OCR check interval multiplier (higher = fewer checks)
    # GREEN: 1x (default), YELLOW: 5x, RED: infinite (suspended)
    ocr_interval_multiplier: float = 1.0

    # Frame skip rate (1 = process all, 2 = skip every other, etc.)
    frame_skip_rate: int = 1

    # Max concurrent vision operations
    max_vision_concurrency: int = 3

    # Should suspend heavy operations entirely
    suspend_heavy_ops: bool = False

    # Target FPS for video capture (lower = less memory)
    target_fps: int = 5

    # Enable GPU acceleration (disable under pressure)
    gpu_enabled: bool = True


# Default throttle settings per Defcon level
DEFAULT_THROTTLE_SETTINGS: Dict[DefconLevel, ThrottleSettings] = {
    DefconLevel.GREEN: ThrottleSettings(
        ocr_interval_multiplier=1.0,
        frame_skip_rate=1,
        max_vision_concurrency=3,
        suspend_heavy_ops=False,
        target_fps=5,
        gpu_enabled=True
    ),
    DefconLevel.YELLOW: ThrottleSettings(
        ocr_interval_multiplier=5.0,  # ~1 OCR check/second instead of 5
        frame_skip_rate=2,            # Process every other frame
        max_vision_concurrency=1,     # Only one vision op at a time
        suspend_heavy_ops=False,
        target_fps=2,                 # Reduce capture rate
        gpu_enabled=True
    ),
    DefconLevel.RED: ThrottleSettings(
        ocr_interval_multiplier=float('inf'),  # Suspend OCR entirely
        frame_skip_rate=10,                     # Minimal frame processing
        max_vision_concurrency=0,               # No vision ops
        suspend_heavy_ops=True,
        target_fps=1,                           # Minimal capture
        gpu_enabled=False                       # Disable GPU to free VRAM
    )
}


@dataclass
class GovernorStatus:
    """
    Current status snapshot from the governor.
    """
    defcon_level: DefconLevel
    memory_usage_percent: float
    memory_available_gb: float
    memory_used_gb: float
    memory_total_gb: float
    throttle_settings: ThrottleSettings
    is_swapping: bool
    page_outs: int
    time_in_current_level_seconds: float
    level_transitions_count: int
    last_transition_time: Optional[datetime]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "defcon_level": self.defcon_level.name,
            "defcon_emoji": self.defcon_level.emoji,
            "memory_usage_percent": round(self.memory_usage_percent, 1),
            "memory_available_gb": round(self.memory_available_gb, 2),
            "memory_used_gb": round(self.memory_used_gb, 2),
            "memory_total_gb": round(self.memory_total_gb, 2),
            "is_swapping": self.is_swapping,
            "page_outs": self.page_outs,
            "time_in_current_level_seconds": round(self.time_in_current_level_seconds, 1),
            "level_transitions_count": self.level_transitions_count,
            "last_transition_time": self.last_transition_time.isoformat() if self.last_transition_time else None,
            "throttle": {
                "ocr_interval_multiplier": self.throttle_settings.ocr_interval_multiplier,
                "frame_skip_rate": self.throttle_settings.frame_skip_rate,
                "max_vision_concurrency": self.throttle_settings.max_vision_concurrency,
                "suspend_heavy_ops": self.throttle_settings.suspend_heavy_ops,
                "target_fps": self.throttle_settings.target_fps,
                "gpu_enabled": self.throttle_settings.gpu_enabled
            }
        }


# =============================================================================
# Adaptive Resource Governor
# =============================================================================

class AdaptiveResourceGovernor:
    """
    Intelligent resource governor for memory-constrained environments.

    Monitors system memory and provides throttling recommendations
    based on Defcon levels. Designed for 16GB M1 Macs running
    vision/OCR workloads.

    Usage:
        governor = AdaptiveResourceGovernor()
        await governor.start()

        # In your frame loop:
        status = await governor.check_status()
        if status.defcon_level == DefconLevel.RED:
            # Abort or suspend heavy operations
            return

        # Get throttle-adjusted check interval
        check_interval = governor.get_adjusted_check_interval(base_interval=1)

        # Check if should process this frame
        if governor.should_process_frame(frame_number):
            # Process frame
            pass
    """

    def __init__(
        self,
        thresholds: Optional[DefconThresholds] = None,
        throttle_settings: Optional[Dict[DefconLevel, ThrottleSettings]] = None,
        monitoring_interval: float = 1.0,
        enable_narration: bool = True
    ):
        """
        Initialize the resource governor.

        Args:
            thresholds: Custom Defcon thresholds
            throttle_settings: Custom throttle settings per level
            monitoring_interval: How often to check memory (seconds)
            enable_narration: Whether to announce level changes via TTS
        """
        self.thresholds = thresholds or DefconThresholds()
        self.throttle_settings = throttle_settings or DEFAULT_THROTTLE_SETTINGS.copy()
        self.monitoring_interval = monitoring_interval
        self.enable_narration = enable_narration

        # State
        self._current_level = DefconLevel.GREEN
        self._level_start_time = time.time()
        self._transitions_count = 0
        self._last_transition_time: Optional[datetime] = None
        self._is_running = False
        self._monitoring_task: Optional[asyncio.Task] = None

        # Memory state
        self._last_memory_check: Optional[Dict[str, Any]] = None
        self._page_outs_baseline = 0
        self._is_macos = platform.system() == "Darwin"

        # Callbacks for level changes
        self._level_change_callbacks: List[Callable[[DefconLevel, DefconLevel], None]] = []

        # Frame processing state (for should_process_frame)
        self._frame_counter = 0

        # Lazy import psutil
        self._psutil = None

        logger.info(
            f"[ResourceGovernor] Initialized "
            f"(thresholds: G→Y={self.thresholds.green_to_yellow}%, "
            f"Y→R={self.thresholds.yellow_to_red}%)"
        )

    def _ensure_psutil(self):
        """Lazy load psutil to avoid import issues."""
        if self._psutil is None:
            try:
                import psutil
                self._psutil = psutil
            except ImportError:
                logger.error("[ResourceGovernor] psutil not installed! Memory monitoring disabled.")
                raise ImportError("psutil is required for AdaptiveResourceGovernor")
        return self._psutil

    @property
    def current_level(self) -> DefconLevel:
        """Get current Defcon level."""
        return self._current_level

    @property
    def current_throttle(self) -> ThrottleSettings:
        """Get current throttle settings."""
        return self.throttle_settings[self._current_level]

    def register_level_change_callback(
        self,
        callback: Callable[[DefconLevel, DefconLevel], None]
    ) -> None:
        """Register callback for level changes. Called with (old_level, new_level)."""
        self._level_change_callbacks.append(callback)

    async def start(self) -> None:
        """Start background memory monitoring."""
        if self._is_running:
            logger.warning("[ResourceGovernor] Already running")
            return

        self._is_running = True
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())

        # Get initial baseline
        await self._update_memory_state()

        logger.info(f"[ResourceGovernor] {self._current_level.emoji} Started monitoring")

    async def stop(self) -> None:
        """Stop background monitoring."""
        if not self._is_running:
            return

        self._is_running = False
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass

        logger.info("[ResourceGovernor] Stopped monitoring")

    async def _monitoring_loop(self) -> None:
        """Background loop to monitor memory and update Defcon level."""
        while self._is_running:
            try:
                await self._update_memory_state()
                await asyncio.sleep(self.monitoring_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ResourceGovernor] Monitoring error: {e}")
                await asyncio.sleep(self.monitoring_interval)

    async def _update_memory_state(self) -> None:
        """Update memory state and potentially transition Defcon level."""
        psutil = self._ensure_psutil()

        # Get memory info
        mem = psutil.virtual_memory()
        memory_percent = mem.percent
        memory_available_gb = mem.available / (1024 ** 3)
        memory_used_gb = mem.used / (1024 ** 3)
        memory_total_gb = mem.total / (1024 ** 3)

        # macOS page outs (swapping indicator)
        page_outs = 0
        is_swapping = False

        if self._is_macos:
            page_outs = await self._get_macos_page_outs()
            # Check if page outs increased significantly since baseline
            page_outs_delta = page_outs - self._page_outs_baseline
            is_swapping = page_outs_delta > self.thresholds.page_outs_critical

            # Update baseline periodically
            if self._page_outs_baseline == 0:
                self._page_outs_baseline = page_outs

        # Store current state
        self._last_memory_check = {
            "memory_percent": memory_percent,
            "memory_available_gb": memory_available_gb,
            "memory_used_gb": memory_used_gb,
            "memory_total_gb": memory_total_gb,
            "page_outs": page_outs,
            "is_swapping": is_swapping,
            "timestamp": time.time()
        }

        # Determine new Defcon level
        new_level = self._calculate_defcon_level(memory_percent, is_swapping)

        # Transition if needed (with stabilization check)
        if new_level != self._current_level:
            await self._transition_level(new_level)

    def _calculate_defcon_level(
        self,
        memory_percent: float,
        is_swapping: bool
    ) -> DefconLevel:
        """
        Calculate Defcon level based on memory state.

        Uses hysteresis to prevent rapid oscillation.
        """
        current = self._current_level

        # Heavy swapping immediately triggers RED
        if is_swapping:
            return DefconLevel.RED

        # Calculate based on current level (hysteresis)
        if current == DefconLevel.GREEN:
            if memory_percent >= self.thresholds.green_to_yellow:
                return DefconLevel.YELLOW
            return DefconLevel.GREEN

        elif current == DefconLevel.YELLOW:
            if memory_percent >= self.thresholds.yellow_to_red:
                return DefconLevel.RED
            elif memory_percent < self.thresholds.yellow_to_green:
                return DefconLevel.GREEN
            return DefconLevel.YELLOW

        else:  # RED
            if memory_percent < self.thresholds.red_to_yellow:
                return DefconLevel.YELLOW
            return DefconLevel.RED

    async def _transition_level(self, new_level: DefconLevel) -> None:
        """
        Transition to a new Defcon level.

        Includes stabilization check to prevent rapid transitions.
        """
        old_level = self._current_level
        time_in_current = time.time() - self._level_start_time

        # Stabilization: Don't transition too quickly (except to RED)
        if new_level != DefconLevel.RED:
            if time_in_current < self.thresholds.stabilization_seconds:
                return

        # Perform transition
        self._current_level = new_level
        self._level_start_time = time.time()
        self._transitions_count += 1
        self._last_transition_time = datetime.now()

        logger.warning(
            f"[ResourceGovernor] {new_level.emoji} DEFCON LEVEL CHANGE: "
            f"{old_level.name} → {new_level.name} "
            f"({self._last_memory_check.get('memory_percent', 0):.1f}% memory used)"
        )

        # Notify callbacks
        for callback in self._level_change_callbacks:
            try:
                callback(old_level, new_level)
            except Exception as e:
                logger.warning(f"[ResourceGovernor] Callback error: {e}")

        # Narrate level change
        if self.enable_narration:
            await self._narrate_level_change(old_level, new_level)

    async def _narrate_level_change(
        self,
        old_level: DefconLevel,
        new_level: DefconLevel
    ) -> None:
        """Announce level change via TTS (if available)."""
        try:
            # Try to use JARVIS TTS
            from backend.voice.tts_router import get_tts_router
            tts = get_tts_router()

            if new_level == DefconLevel.RED:
                message = (
                    "Warning: Memory pressure critical. "
                    "Suspending heavy operations to prevent system slowdown."
                )
            elif new_level == DefconLevel.YELLOW:
                message = (
                    "Memory pressure elevated. "
                    "Reducing visual monitoring rate."
                )
            else:  # GREEN
                message = "Memory pressure normal. Resuming full operations."

            await tts.speak(message, priority="high")

        except Exception as e:
            logger.debug(f"[ResourceGovernor] Narration unavailable: {e}")

    async def _get_macos_page_outs(self) -> int:
        """Get macOS page outs (swapping indicator) from vm_stat."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "vm_stat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            output = stdout.decode()

            for line in output.split("\n"):
                if "Pages paged out:" in line:
                    value_str = line.split(":")[1].strip().rstrip(".")
                    return int(value_str)

            return 0
        except Exception as e:
            logger.debug(f"[ResourceGovernor] Failed to get page outs: {e}")
            return 0

    async def check_status(self) -> GovernorStatus:
        """
        Get current governor status.

        This is the primary method to call from frame processing loops.
        """
        # Ensure we have memory data
        if self._last_memory_check is None:
            await self._update_memory_state()

        mem = self._last_memory_check or {}

        return GovernorStatus(
            defcon_level=self._current_level,
            memory_usage_percent=mem.get("memory_percent", 0),
            memory_available_gb=mem.get("memory_available_gb", 0),
            memory_used_gb=mem.get("memory_used_gb", 0),
            memory_total_gb=mem.get("memory_total_gb", 0),
            throttle_settings=self.current_throttle,
            is_swapping=mem.get("is_swapping", False),
            page_outs=mem.get("page_outs", 0),
            time_in_current_level_seconds=time.time() - self._level_start_time,
            level_transitions_count=self._transitions_count,
            last_transition_time=self._last_transition_time
        )

    def get_adjusted_check_interval(self, base_interval: int = 1) -> int:
        """
        Get throttle-adjusted OCR check interval.

        Args:
            base_interval: Base check interval (e.g., fps / 5)

        Returns:
            Adjusted interval based on current Defcon level
        """
        multiplier = self.current_throttle.ocr_interval_multiplier

        if multiplier == float('inf'):
            # Suspended - return very large interval
            return 999999

        return max(1, int(base_interval * multiplier))

    def should_process_frame(self, frame_number: Optional[int] = None) -> bool:
        """
        Check if this frame should be processed based on throttle settings.

        Args:
            frame_number: Optional frame number (auto-increments if not provided)

        Returns:
            True if frame should be processed
        """
        if frame_number is None:
            self._frame_counter += 1
            frame_number = self._frame_counter

        throttle = self.current_throttle

        # Check suspend flag
        if throttle.suspend_heavy_ops:
            return False

        # Check frame skip rate
        return frame_number % throttle.frame_skip_rate == 0

    def should_abort_monitoring(self) -> Tuple[bool, str]:
        """
        Check if monitoring should be aborted due to critical memory.

        Returns:
            Tuple of (should_abort, reason)
        """
        if self._current_level == DefconLevel.RED:
            return True, "Critical memory pressure - aborting to prevent system slowdown"

        return False, ""

    def get_metrics(self) -> Dict[str, Any]:
        """Get metrics for monitoring/logging."""
        mem = self._last_memory_check or {}

        return {
            "defcon_level": self._current_level.name,
            "memory_usage_percent": mem.get("memory_percent", 0),
            "memory_available_gb": mem.get("memory_available_gb", 0),
            "is_swapping": mem.get("is_swapping", False),
            "transitions_count": self._transitions_count,
            "time_in_level_seconds": time.time() - self._level_start_time,
            "throttle_multiplier": self.current_throttle.ocr_interval_multiplier,
            "frame_skip_rate": self.current_throttle.frame_skip_rate,
            "suspend_heavy_ops": self.current_throttle.suspend_heavy_ops
        }


# =============================================================================
# Global Instance
# =============================================================================

_governor_instance: Optional[AdaptiveResourceGovernor] = None
_governor_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_resource_governor() -> AdaptiveResourceGovernor:
    """
    Get or create the global AdaptiveResourceGovernor instance.

    Thread-safe singleton access.
    """
    global _governor_instance

    async with _governor_lock:
        if _governor_instance is None:
            _governor_instance = AdaptiveResourceGovernor()
            await _governor_instance.start()

        return _governor_instance


def get_resource_governor_sync() -> Optional[AdaptiveResourceGovernor]:
    """
    Get the global governor instance synchronously (if already initialized).

    Returns None if not yet initialized.
    """
    return _governor_instance


async def shutdown_resource_governor() -> None:
    """Shutdown the global governor instance."""
    global _governor_instance

    if _governor_instance:
        await _governor_instance.stop()
        _governor_instance = None


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "AdaptiveResourceGovernor",
    "DefconLevel",
    "DefconThresholds",
    "ThrottleSettings",
    "GovernorStatus",
    "get_resource_governor",
    "get_resource_governor_sync",
    "shutdown_resource_governor",
    "DEFAULT_THROTTLE_SETTINGS"
]
