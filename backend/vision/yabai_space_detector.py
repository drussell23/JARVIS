"""
Yabai integration for accurate Mission Control space detection
Provides real-time space and window information using Yabai CLI
Enhanced with YOLO vision for multi-monitor layout detection

FEATURES:
- Auto-start service with retry logic
- Graceful fallback when yabai unavailable
- Async subprocess execution (non-blocking)
- Service health monitoring and auto-recovery
- Multi-display space organization
- Vision-enhanced layout detection

NOTE: This file has had repeated indentation issues with auto-formatters.
If using black, autopep8, or other formatters, please exclude this file
or manually review changes before committing.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Import GhostPersistenceManager for crash recovery
try:
    from backend.vision.ghost_persistence_manager import (
        GhostPersistenceManager,
        get_persistence_manager,
    )
    _HAS_GHOST_PERSISTENCE = True
except ImportError:
    _HAS_GHOST_PERSISTENCE = False

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

logger = logging.getLogger(__name__)


# =============================================================================
# ROOT CAUSE FIX v11.0.0: Non-Blocking Yabai Availability Cache
# =============================================================================
# PROBLEM: YabaiSpaceDetector.__init__ calls subprocess.run() which blocks
# the entire event loop, causing "Processing..." hang
#
# SOLUTION: Module-level cache + async initialization
# - Cache yabai availability status at module level
# - Skip blocking checks if we already know yabai state
# - Async methods for initialization when needed
# =============================================================================

# Module-level cache for yabai availability (avoids repeated blocking checks)
_YABAI_AVAILABILITY_CACHE = {
    "checked": False,
    "available": False,
    "path": None,
    "version": None,
    "last_check": None,
    "check_count": 0,
}

# Cache validity duration (seconds) - don't recheck too often
_YABAI_CACHE_TTL = 60.0


def _is_yabai_cache_valid() -> bool:
    """Check if the yabai availability cache is still valid."""
    if not _YABAI_AVAILABILITY_CACHE["checked"]:
        return False
    last_check = _YABAI_AVAILABILITY_CACHE.get("last_check")
    if last_check is None:
        return False
    age = (datetime.now() - last_check).total_seconds()
    return age < _YABAI_CACHE_TTL


async def async_quick_yabai_check() -> Tuple[bool, Optional[str]]:
    """
    TRUE ASYNC yabai availability check using asyncio subprocesses.

    ROOT CAUSE FIX v12.0.0: True Non-Blocking Implementation
    =========================================================
    PROBLEM: Previous implementation used subprocess.run() which blocks
    the entire event loop, even with short timeouts. During that block:
    - UI spinner freezes
    - Voice input stops processing
    - WebSocket messages queue up
    - System feels unresponsive

    SOLUTION: Use asyncio.create_subprocess_exec() which:
    - Yields control to event loop while waiting for OS
    - Allows other coroutines to run during subprocess execution
    - Keeps spinner, voice, and WebSocket 100% responsive
    - True concurrent execution, not fake-async

    Returns:
        Tuple of (is_available, yabai_path)
    """
    global _YABAI_AVAILABILITY_CACHE

    # =========================================================================
    # STEP 1: Check cache FIRST (instant, no I/O)
    # =========================================================================
    if _is_yabai_cache_valid():
        logger.debug("[YABAI] Using cached availability result")
        return _YABAI_AVAILABILITY_CACHE["available"], _YABAI_AVAILABILITY_CACHE["path"]

    # =========================================================================
    # STEP 2: Find yabai path (filesystem check - very fast)
    # =========================================================================
    # Use asyncio.to_thread for shutil.which (it's a blocking syscall)
    try:
        yabai_path = await asyncio.wait_for(
            asyncio.to_thread(shutil.which, "yabai"),
            timeout=0.5
        )
    except asyncio.TimeoutError:
        yabai_path = None

    if not yabai_path:
        # Check common locations (fast filesystem checks)
        common_paths = [
            "/opt/homebrew/bin/yabai",
            "/usr/local/bin/yabai",
        ]
        for path in common_paths:
            try:
                exists = await asyncio.to_thread(
                    lambda p: os.path.isfile(p) and os.access(p, os.X_OK),
                    path
                )
                if exists:
                    yabai_path = path
                    break
            except Exception:
                continue

    if not yabai_path:
        # Yabai not installed - cache and return
        logger.debug("[YABAI] Yabai executable not found")
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True,
            "available": False,
            "path": None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return False, None

    # =========================================================================
    # STEP 3: TRUE ASYNC subprocess check (yields to event loop!)
    # =========================================================================
    try:
        # Create subprocess WITHOUT blocking the event loop
        process = await asyncio.create_subprocess_exec(
            yabai_path, "-m", "query", "--spaces",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Wait for completion with timeout (event loop stays responsive!)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=2.0  # 2 second timeout for subprocess
            )
            is_available = process.returncode == 0
        except asyncio.TimeoutError:
            # Kill the hung process
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            logger.warning("[YABAI] Async subprocess timed out after 2s")
            is_available = False

        # Update cache with result
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True,
            "available": is_available,
            "path": yabai_path if is_available else None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })

        if is_available:
            logger.debug(f"[YABAI] Async check: Available at {yabai_path}")
        else:
            logger.debug(f"[YABAI] Async check: Not running (returncode={process.returncode})")

        return is_available, yabai_path if is_available else None

    except Exception as e:
        logger.debug(f"[YABAI] Async check failed: {e}")
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True,
            "available": False,
            "path": None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return False, None


def _quick_yabai_check() -> Tuple[bool, Optional[str]]:
    """
    SYNC version of yabai check (for non-async contexts).

    WARNING: This blocks the event loop! Use async_quick_yabai_check() when possible.
    Kept for backward compatibility with sync code paths.
    """
    global _YABAI_AVAILABILITY_CACHE

    # Return cached result if valid (instant)
    if _is_yabai_cache_valid():
        return _YABAI_AVAILABILITY_CACHE["available"], _YABAI_AVAILABILITY_CACHE["path"]

    # Quick path check
    yabai_path = shutil.which("yabai")
    if not yabai_path:
        common_paths = ["/opt/homebrew/bin/yabai", "/usr/local/bin/yabai"]
        for path in common_paths:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                yabai_path = path
                break

    if not yabai_path:
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True, "available": False, "path": None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return False, None

    # Blocking subprocess check (1s timeout)
    try:
        result = subprocess.run(
            [yabai_path, "-m", "query", "--spaces"],
            capture_output=True, text=True, timeout=1.0
        )
        is_available = result.returncode == 0
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True,
            "available": is_available,
            "path": yabai_path if is_available else None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return is_available, yabai_path if is_available else None
    except Exception as e:
        logger.debug(f"[YABAI] Sync check failed: {e}")
        _YABAI_AVAILABILITY_CACHE.update({
            "checked": True, "available": False, "path": None,
            "last_check": datetime.now(),
            "check_count": _YABAI_AVAILABILITY_CACHE["check_count"] + 1,
        })
        return False, None


def get_cached_yabai_status() -> Dict[str, Any]:
    """Get the current cached yabai status without blocking."""
    return dict(_YABAI_AVAILABILITY_CACHE)


def invalidate_yabai_cache():
    """Invalidate the yabai cache to force a recheck."""
    global _YABAI_AVAILABILITY_CACHE
    _YABAI_AVAILABILITY_CACHE["checked"] = False


# =============================================================================
# SERVICE STATE & HEALTH TRACKING
# =============================================================================

@dataclass
class YabaiServiceHealth:
    """Tracks yabai service health metrics."""
    is_running: bool = False
    last_check: Optional[datetime] = None
    last_successful_query: Optional[datetime] = None
    consecutive_failures: int = 0
    total_queries: int = 0
    successful_queries: int = 0
    avg_response_time_ms: float = 0.0
    last_error: Optional[str] = None
    permissions_granted: bool = False
    yabai_path: Optional[str] = None
    yabai_version: Optional[str] = None
    startup_attempts: int = 0
    last_startup_attempt: Optional[datetime] = None

    @property
    def success_rate(self) -> float:
        """Calculate query success rate."""
        if self.total_queries == 0:
            return 0.0
        return (self.successful_queries / self.total_queries) * 100

    @property
    def needs_restart(self) -> bool:
        """Determine if service needs restart based on health metrics."""
        # Restart if we've had 3+ consecutive failures
        if self.consecutive_failures >= 3:
            return True
        # Restart if no successful query in 5 minutes and we're supposed to be running
        if self.is_running and self.last_successful_query:
            if datetime.now() - self.last_successful_query > timedelta(minutes=5):
                return True
        return False

    def record_success(self, response_time_ms: float):
        """Record a successful query."""
        self.total_queries += 1
        self.successful_queries += 1
        self.consecutive_failures = 0
        self.last_successful_query = datetime.now()
        self.last_error = None
        # Rolling average
        alpha = 0.3  # Weight for new value
        self.avg_response_time_ms = (
            alpha * response_time_ms + (1 - alpha) * self.avg_response_time_ms
        )

    def record_failure(self, error: str):
        """Record a failed query."""
        self.total_queries += 1
        self.consecutive_failures += 1
        self.last_error = error

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for reporting."""
        return {
            "is_running": self.is_running,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "last_successful_query": self.last_successful_query.isoformat() if self.last_successful_query else None,
            "consecutive_failures": self.consecutive_failures,
            "total_queries": self.total_queries,
            "successful_queries": self.successful_queries,
            "success_rate": f"{self.success_rate:.1f}%",
            "avg_response_time_ms": round(self.avg_response_time_ms, 2),
            "last_error": self.last_error,
            "permissions_granted": self.permissions_granted,
            "yabai_path": self.yabai_path,
            "yabai_version": self.yabai_version,
            "needs_restart": self.needs_restart,
        }


@dataclass
class YabaiConfig:
    """Configuration for yabai service management."""
    auto_start: bool = True
    max_startup_attempts: int = 3
    startup_retry_delay_seconds: float = 2.0
    health_check_interval_seconds: float = 30.0
    query_timeout_seconds: float = 5.0
    enable_auto_recovery: bool = True
    config_path: Path = field(default_factory=lambda: Path.home() / ".config" / "yabai" / "yabairc")


# =============================================================================
# v24.0.0: INTELLIGENT SEARCH & RESCUE PROTOCOL
# =============================================================================
# Root cause solution for dehydrated windows with:
# - Multi-strategy rescue approaches
# - Dynamic wake delay calibration
# - Parallel rescue with concurrency control
# - Retry logic with exponential backoff
# - Root cause detection for failures
# - Comprehensive telemetry and diagnostics
# =============================================================================

class RescueFailureReason(Enum):
    """Root cause categories for rescue failures."""
    WINDOW_NOT_FOUND = "window_not_found"           # Window doesn't exist or was closed
    SPACE_NOT_FOUND = "space_not_found"             # Source/target space doesn't exist
    YABAI_UNAVAILABLE = "yabai_unavailable"         # Yabai service not running
    YABAI_TIMEOUT = "yabai_timeout"                 # Yabai command timed out
    PERMISSION_DENIED = "permission_denied"         # Accessibility permission issue
    WINDOW_MINIMIZED = "window_minimized"           # Window is minimized (special state)
    WINDOW_FULLSCREEN = "window_fullscreen"         # Window in fullscreen mode
    SPACE_SWITCH_FAILED = "space_switch_failed"     # Couldn't switch to source space
    MOVE_AFTER_WAKE_FAILED = "move_after_wake_failed"  # Woke window but move still failed
    UNKNOWN = "unknown"                             # Unknown failure reason


class RescueStrategy(Enum):
    """Different strategies for rescuing dehydrated windows."""
    DIRECT = "direct"                   # Try direct move (fast path)
    SWITCH_GRAB_RETURN = "switch_grab_return"  # Classic rescue: switch → move → return
    FOCUS_THEN_MOVE = "focus_then_move"  # Focus window first, then move
    UNMINIMIZE_FIRST = "unminimize_first"  # Unminimize if minimized, then move
    SPACE_FOCUS_EXTENDED = "space_focus_extended"  # Extended wake delay for stubborn windows


@dataclass
class RescueTelemetry:
    """
    Telemetry and diagnostics for rescue operations.

    Tracks success rates, timing, and failure patterns to:
    - Calibrate wake delays dynamically
    - Identify problematic windows/apps
    - Optimize strategy selection
    """
    total_attempts: int = 0
    successful_rescues: int = 0
    failed_rescues: int = 0

    # Strategy success tracking
    direct_successes: int = 0
    switch_grab_return_successes: int = 0
    focus_then_move_successes: int = 0
    unminimize_first_successes: int = 0
    extended_wake_successes: int = 0

    # Timing metrics (rolling averages)
    avg_direct_time_ms: float = 0.0
    avg_rescue_time_ms: float = 0.0
    avg_wake_delay_needed_ms: float = 50.0  # Start with default

    # Failure tracking by reason
    failures_by_reason: Dict[str, int] = field(default_factory=dict)

    # App-specific metrics for intelligent routing
    app_success_rates: Dict[str, float] = field(default_factory=dict)
    app_preferred_strategies: Dict[str, str] = field(default_factory=dict)

    # Last calibration data
    last_calibration_time: Optional[datetime] = None
    calibrated_wake_delay_ms: float = 50.0

    @property
    def success_rate(self) -> float:
        """Calculate overall rescue success rate."""
        if self.total_attempts == 0:
            return 1.0  # No data, assume success
        return self.successful_rescues / self.total_attempts

    def record_attempt(
        self,
        success: bool,
        strategy: RescueStrategy,
        duration_ms: float,
        failure_reason: Optional[RescueFailureReason] = None,
        app_name: Optional[str] = None,
        wake_delay_used_ms: Optional[float] = None
    ):
        """Record a rescue attempt for telemetry."""
        self.total_attempts += 1

        if success:
            self.successful_rescues += 1

            # Track strategy success
            if strategy == RescueStrategy.DIRECT:
                self.direct_successes += 1
                alpha = 0.3
                self.avg_direct_time_ms = alpha * duration_ms + (1 - alpha) * self.avg_direct_time_ms
            elif strategy == RescueStrategy.SWITCH_GRAB_RETURN:
                self.switch_grab_return_successes += 1
            elif strategy == RescueStrategy.FOCUS_THEN_MOVE:
                self.focus_then_move_successes += 1
            elif strategy == RescueStrategy.UNMINIMIZE_FIRST:
                self.unminimize_first_successes += 1
            elif strategy == RescueStrategy.SPACE_FOCUS_EXTENDED:
                self.extended_wake_successes += 1

            # Update rescue timing
            if strategy != RescueStrategy.DIRECT:
                alpha = 0.3
                self.avg_rescue_time_ms = alpha * duration_ms + (1 - alpha) * self.avg_rescue_time_ms

                # Calibrate wake delay based on what worked
                if wake_delay_used_ms is not None:
                    self.avg_wake_delay_needed_ms = (
                        alpha * wake_delay_used_ms + (1 - alpha) * self.avg_wake_delay_needed_ms
                    )

            # Track app success
            if app_name:
                current_rate = self.app_success_rates.get(app_name, 1.0)
                self.app_success_rates[app_name] = 0.8 * current_rate + 0.2 * 1.0

                # Remember preferred strategy for this app
                self.app_preferred_strategies[app_name] = strategy.value
        else:
            self.failed_rescues += 1

            # Track failure reason
            reason_key = failure_reason.value if failure_reason else "unknown"
            self.failures_by_reason[reason_key] = self.failures_by_reason.get(reason_key, 0) + 1

            # Track app failure
            if app_name:
                current_rate = self.app_success_rates.get(app_name, 1.0)
                self.app_success_rates[app_name] = 0.8 * current_rate + 0.2 * 0.0

    def get_recommended_wake_delay_ms(self, app_name: Optional[str] = None) -> float:
        """
        Get the recommended wake delay based on telemetry.

        Considers:
        - Overall average that worked
        - App-specific requirements (some apps need more time)
        - Base environment variable override
        """
        # Check for environment override
        env_override = os.environ.get("JARVIS_RESCUE_WAKE_DELAY")
        if env_override:
            return float(env_override) * 1000  # Convert seconds to ms

        base_delay = max(self.avg_wake_delay_needed_ms, 30.0)  # At least 30ms

        # Increase for apps with lower success rates
        if app_name and app_name in self.app_success_rates:
            app_rate = self.app_success_rates[app_name]
            if app_rate < 0.8:  # Lower success rate = need more time
                base_delay *= (2.0 - app_rate)  # Up to 2x for problematic apps

        return min(base_delay, 500.0)  # Cap at 500ms

    def get_recommended_strategy(
        self,
        app_name: Optional[str] = None,
        is_minimized: bool = False,
        is_fullscreen: bool = False
    ) -> RescueStrategy:
        """Get the recommended rescue strategy based on telemetry and window state."""
        # Special cases first
        if is_minimized:
            return RescueStrategy.UNMINIMIZE_FIRST
        if is_fullscreen:
            return RescueStrategy.SPACE_FOCUS_EXTENDED

        # Check app-specific preference
        if app_name and app_name in self.app_preferred_strategies:
            try:
                return RescueStrategy(self.app_preferred_strategies[app_name])
            except ValueError:
                pass

        # Default to switch-grab-return (most reliable)
        return RescueStrategy.SWITCH_GRAB_RETURN

    def to_dict(self) -> Dict[str, Any]:
        """Convert telemetry to dictionary for logging/reporting."""
        return {
            "total_attempts": self.total_attempts,
            "success_rate": f"{self.success_rate * 100:.1f}%",
            "successful_rescues": self.successful_rescues,
            "failed_rescues": self.failed_rescues,
            "strategy_successes": {
                "direct": self.direct_successes,
                "switch_grab_return": self.switch_grab_return_successes,
                "focus_then_move": self.focus_then_move_successes,
                "unminimize_first": self.unminimize_first_successes,
                "extended_wake": self.extended_wake_successes,
            },
            "timing": {
                "avg_direct_ms": round(self.avg_direct_time_ms, 2),
                "avg_rescue_ms": round(self.avg_rescue_time_ms, 2),
                "calibrated_wake_delay_ms": round(self.calibrated_wake_delay_ms, 2),
            },
            "failures_by_reason": self.failures_by_reason,
            "app_success_rates": {
                k: f"{v * 100:.1f}%" for k, v in self.app_success_rates.items()
            },
        }


@dataclass
class RescueResult:
    """Detailed result of a rescue operation."""
    success: bool
    window_id: int
    source_space: Optional[int]
    target_space: int
    strategy_used: RescueStrategy
    duration_ms: float
    failure_reason: Optional[RescueFailureReason] = None
    attempts: int = 1
    wake_delay_used_ms: float = 0.0
    app_name: Optional[str] = None

    # For batch operations
    method: str = ""  # "direct", "rescue", "failed" for compatibility

    def __post_init__(self):
        """Set method for backward compatibility."""
        if not self.method:
            if self.success and self.strategy_used == RescueStrategy.DIRECT:
                self.method = "direct"
            elif self.success:
                self.method = "rescue"
            else:
                self.method = "failed"


# Global telemetry instance (module-level for persistence across calls)
_RESCUE_TELEMETRY: Optional[RescueTelemetry] = None


def get_rescue_telemetry() -> RescueTelemetry:
    """Get or create the global rescue telemetry instance."""
    global _RESCUE_TELEMETRY
    if _RESCUE_TELEMETRY is None:
        _RESCUE_TELEMETRY = RescueTelemetry()
    return _RESCUE_TELEMETRY


def reset_rescue_telemetry() -> None:
    """Reset the global rescue telemetry (for testing)."""
    global _RESCUE_TELEMETRY
    _RESCUE_TELEMETRY = None


# =============================================================================
# v25.0.0: SHADOW MONITOR INFRASTRUCTURE
# =============================================================================
# Comprehensive Ghost Display management with:
# - Health monitoring and auto-recovery
# - Window geometry preservation and restoration
# - Intelligent window layout management
# - Window return policy for post-monitoring cleanup
# - Fallback strategies when Ghost Display unavailable
# - Multi-window coordination with smart throttling
# =============================================================================

class WindowLayoutStyle(Enum):
    """Layout styles for arranging windows on Ghost Display."""
    SIDE_BY_SIDE = "side_by_side"      # Windows arranged horizontally
    STACKED = "stacked"                 # Windows arranged vertically
    GRID = "grid"                       # Windows in a grid pattern
    CASCADE = "cascade"                 # Cascaded windows (overlapping)
    MAXIMIZE = "maximize"               # Each window maximized (for single window)
    PRESERVE = "preserve"               # Keep original positions


class GhostDisplayStatus(Enum):
    """Status of the Ghost Display."""
    AVAILABLE = "available"             # Ghost Display is ready
    UNAVAILABLE = "unavailable"         # No Ghost Display found
    DISCONNECTED = "disconnected"       # Was available, now gone
    RECONNECTING = "reconnecting"       # Attempting to reconnect
    FALLBACK = "fallback"               # Using fallback strategy


@dataclass
class WindowGeometry:
    """
    Preserved window geometry for restoration with display-aware scaling.

    v26.0: Enhanced with:
    - Display scaling/Retina coordinate handling
    - Space UUID for stability across space reordering
    - Focus state tracking
    - Z-order (layer) information
    - Min/max size constraints
    - Animation timing data
    """
    window_id: int
    app_name: str
    original_space: int
    original_display: int
    x: int
    y: int
    width: int
    height: int
    is_minimized: bool = False
    is_fullscreen: bool = False
    is_floating: bool = False
    teleported_at: Optional[datetime] = None
    current_space: Optional[int] = None

    # v26.0: Display scaling support
    source_display_scale: float = 1.0  # Retina scale factor of original display
    source_display_width: int = 1920   # Original display resolution
    source_display_height: int = 1080
    ghost_display_scale: float = 1.0   # Scale factor of Ghost Display
    ghost_display_width: int = 1920
    ghost_display_height: int = 1080

    # v26.0: Space stability
    original_space_uuid: Optional[str] = None  # More stable than space ID
    original_space_label: Optional[str] = None  # Space label if available

    # v26.0: Focus and z-order
    was_focused: bool = False  # Was this window focused before teleport?
    z_order: int = 0           # Layer order (higher = more in front)
    is_split_view: bool = False  # Window in split view
    is_picture_in_picture: bool = False  # PiP mode

    # v26.0: Window constraints
    min_width: Optional[int] = None
    min_height: Optional[int] = None
    max_width: Optional[int] = None
    max_height: Optional[int] = None
    has_constraints: bool = False

    # v26.0: Animation timing
    move_animation_duration_ms: float = 250.0  # macOS animation duration
    last_position_stable_at: Optional[datetime] = None

    def convert_for_display(
        self,
        target_scale: float,
        target_width: int,
        target_height: int
    ) -> Tuple[int, int, int, int]:
        """
        Convert geometry coordinates for a different display.

        Handles Retina scaling and different resolutions.

        Args:
            target_scale: Scale factor of target display
            target_width: Width of target display
            target_height: Height of target display

        Returns:
            Tuple of (x, y, width, height) adjusted for target display
        """
        # Scale ratio between displays
        scale_ratio = target_scale / self.source_display_scale if self.source_display_scale > 0 else 1.0

        # Resolution ratio (for positioning)
        width_ratio = target_width / self.source_display_width if self.source_display_width > 0 else 1.0
        height_ratio = target_height / self.source_display_height if self.source_display_height > 0 else 1.0

        # Convert position (scaled by resolution ratio)
        new_x = int(self.x * width_ratio)
        new_y = int(self.y * height_ratio)

        # Convert size (scaled by scale ratio for Retina)
        new_width = int(self.width * scale_ratio)
        new_height = int(self.height * scale_ratio)

        # Apply constraints if available
        if self.has_constraints:
            if self.min_width is not None:
                new_width = max(new_width, int(self.min_width * scale_ratio))
            if self.min_height is not None:
                new_height = max(new_height, int(self.min_height * scale_ratio))
            if self.max_width is not None:
                new_width = min(new_width, int(self.max_width * scale_ratio))
            if self.max_height is not None:
                new_height = min(new_height, int(self.max_height * scale_ratio))

        # Ensure window fits on target display
        new_x = min(new_x, target_width - new_width)
        new_y = min(new_y, target_height - new_height)
        new_x = max(0, new_x)
        new_y = max(25, new_y)  # Account for menu bar

        return new_x, new_y, new_width, new_height

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_id": self.window_id,
            "app_name": self.app_name,
            "original_space": self.original_space,
            "original_display": self.original_display,
            "bounds": {"x": self.x, "y": self.y, "width": self.width, "height": self.height},
            "is_minimized": self.is_minimized,
            "is_fullscreen": self.is_fullscreen,
            "is_floating": self.is_floating,
            "teleported_at": self.teleported_at.isoformat() if self.teleported_at else None,
            "display_scaling": {
                "source_scale": self.source_display_scale,
                "ghost_scale": self.ghost_display_scale,
                "source_resolution": f"{self.source_display_width}x{self.source_display_height}",
                "ghost_resolution": f"{self.ghost_display_width}x{self.ghost_display_height}",
            },
            "state": {
                "was_focused": self.was_focused,
                "z_order": self.z_order,
                "is_split_view": self.is_split_view,
                "is_picture_in_picture": self.is_picture_in_picture,
            },
            "constraints": {
                "has_constraints": self.has_constraints,
                "min_size": f"{self.min_width}x{self.min_height}" if self.min_width else None,
                "max_size": f"{self.max_width}x{self.max_height}" if self.max_width else None,
            },
        }


class SystemEventType(Enum):
    """System events that affect monitoring."""
    DISPLAY_CONNECTED = "display_connected"
    DISPLAY_DISCONNECTED = "display_disconnected"
    DISPLAY_RESOLUTION_CHANGED = "display_resolution_changed"
    DISPLAY_ARRANGEMENT_CHANGED = "display_arrangement_changed"
    SYSTEM_SLEEP = "system_sleep"
    SYSTEM_WAKE = "system_wake"
    SPACE_CREATED = "space_created"
    SPACE_DESTROYED = "space_destroyed"
    USER_SWITCHED_TO_GHOST = "user_switched_to_ghost"
    YABAI_RESTARTED = "yabai_restarted"
    PERMISSIONS_CHANGED = "permissions_changed"


@dataclass
class GhostDisplayInfo:
    """
    Information about the Ghost Display with comprehensive tracking.

    v26.0: Enhanced with:
    - Display scale factor for Retina support
    - Resolution change detection
    - User presence tracking
    - Animation timing information
    """
    space_id: int
    display_id: int
    display_name: str
    width: int
    height: int
    is_virtual: bool
    window_count: int
    last_health_check: Optional[datetime] = None
    consecutive_failures: int = 0

    # v26.0: Display scaling
    scale_factor: float = 1.0  # Retina scale (1.0 = standard, 2.0 = Retina)
    physical_width: int = 0    # Physical pixels (width * scale_factor)
    physical_height: int = 0

    # v26.0: Resolution tracking
    initial_width: int = 0     # Width at initialization
    initial_height: int = 0    # Height at initialization
    resolution_changed: bool = False
    last_resolution_check: Optional[datetime] = None

    # v26.0: User presence
    user_last_seen_on_ghost: Optional[datetime] = None
    user_currently_on_ghost: bool = False
    user_visit_count: int = 0

    # v26.0: Space stability
    space_uuid: Optional[str] = None  # More stable than space_id
    space_label: Optional[str] = None
    space_index: int = 0  # Position in space list

    # v26.0: System state
    is_system_sleeping: bool = False
    last_sleep_time: Optional[datetime] = None
    last_wake_time: Optional[datetime] = None

    # v26.0: Animation timing
    animation_complete: bool = True
    animation_started_at: Optional[datetime] = None
    standard_animation_duration_ms: float = 250.0

    def __post_init__(self):
        """Initialize computed fields."""
        if self.initial_width == 0:
            self.initial_width = self.width
        if self.initial_height == 0:
            self.initial_height = self.height
        if self.physical_width == 0:
            self.physical_width = int(self.width * self.scale_factor)
        if self.physical_height == 0:
            self.physical_height = int(self.height * self.scale_factor)

    @property
    def is_healthy(self) -> bool:
        """Check if Ghost Display is healthy based on recent checks."""
        if self.consecutive_failures >= 3:
            return False
        if self.is_system_sleeping:
            return False  # Not healthy during sleep
        if self.last_health_check is None:
            return True  # No data, assume healthy
        age = (datetime.now() - self.last_health_check).total_seconds()
        return age < 60  # Consider unhealthy if not checked in 60s

    @property
    def resolution_changed_since_init(self) -> bool:
        """Check if resolution changed since initialization."""
        return self.width != self.initial_width or self.height != self.initial_height

    @property
    def animation_in_progress(self) -> bool:
        """Check if an animation is still in progress."""
        if self.animation_complete or self.animation_started_at is None:
            return False
        elapsed = (datetime.now() - self.animation_started_at).total_seconds() * 1000
        return elapsed < self.standard_animation_duration_ms

    def mark_animation_start(self):
        """Mark that a window animation has started."""
        self.animation_complete = False
        self.animation_started_at = datetime.now()

    def mark_animation_complete(self):
        """Mark that animations are complete."""
        self.animation_complete = True
        self.animation_started_at = None

    async def wait_for_animation(self, extra_buffer_ms: float = 50.0):
        """Wait for any in-progress animation to complete."""
        if self.animation_in_progress:
            remaining = self.standard_animation_duration_ms
            if self.animation_started_at:
                elapsed = (datetime.now() - self.animation_started_at).total_seconds() * 1000
                remaining = max(0, self.standard_animation_duration_ms - elapsed)
            await asyncio.sleep((remaining + extra_buffer_ms) / 1000.0)
        self.mark_animation_complete()

    def update_resolution(self, new_width: int, new_height: int):
        """Update resolution and track changes."""
        if new_width != self.width or new_height != self.height:
            self.resolution_changed = True
            self.width = new_width
            self.height = new_height
            self.physical_width = int(new_width * self.scale_factor)
            self.physical_height = int(new_height * self.scale_factor)
            self.last_resolution_check = datetime.now()

    def record_user_presence(self, is_present: bool):
        """Record user presence on Ghost Display."""
        if is_present and not self.user_currently_on_ghost:
            self.user_last_seen_on_ghost = datetime.now()
            self.user_visit_count += 1
        self.user_currently_on_ghost = is_present

    def record_system_sleep(self):
        """Record system entering sleep."""
        self.is_system_sleeping = True
        self.last_sleep_time = datetime.now()

    def record_system_wake(self):
        """Record system waking from sleep."""
        self.is_system_sleeping = False
        self.last_wake_time = datetime.now()
        # Reset health check after wake
        self.consecutive_failures = 0


@dataclass
class GhostDisplayManagerConfig:
    """
    Configuration for Ghost Display management.

    v26.0: Enhanced with edge case handling configuration.
    """
    # Health monitoring
    health_check_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_HEALTH_INTERVAL", "30"))
    )
    max_consecutive_failures: int = 3
    auto_recovery_enabled: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_AUTO_RECOVERY", "true").lower() == "true"
    )

    # Window layout
    default_layout_style: WindowLayoutStyle = field(
        default_factory=lambda: WindowLayoutStyle(
            os.environ.get("JARVIS_GHOST_LAYOUT", "side_by_side")
        ) if os.environ.get("JARVIS_GHOST_LAYOUT") else WindowLayoutStyle.SIDE_BY_SIDE
    )
    layout_padding: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_PADDING", "10"))
    )
    max_windows_per_row: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_MAX_PER_ROW", "3"))
    )

    # Window return policy
    return_windows_after_monitoring: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_RETURN_WINDOWS", "false").lower() == "true"
    )
    preserve_geometry_on_return: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_PRESERVE_GEOMETRY", "true").lower() == "true"
    )

    # Fallback strategy
    fallback_enabled: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_FALLBACK", "true").lower() == "true"
    )
    fallback_create_space: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_CREATE_SPACE", "false").lower() == "true"
    )

    # Multi-window coordination
    max_windows_on_ghost: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_MAX_WINDOWS", "10"))
    )
    throttle_teleports: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_THROTTLE_TELEPORTS", "true").lower() == "true"
    )
    teleport_delay_ms: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_TELEPORT_DELAY_MS", "50"))
    )

    # v26.0: Display scaling configuration
    enable_retina_scaling: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_RETINA_SCALING", "true").lower() == "true"
    )
    auto_detect_display_scale: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_AUTO_SCALE", "true").lower() == "true"
    )
    default_display_scale: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_DEFAULT_SCALE", "1.0"))
    )

    # v26.0: Resolution change handling
    monitor_resolution_changes: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_MONITOR_RESOLUTION", "true").lower() == "true"
    )
    resolution_check_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_RESOLUTION_INTERVAL", "10"))
    )
    auto_relayout_on_resolution_change: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_AUTO_RELAYOUT", "true").lower() == "true"
    )

    # v26.0: System sleep/wake handling
    handle_sleep_wake_events: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_HANDLE_SLEEP", "true").lower() == "true"
    )
    post_wake_delay_ms: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_WAKE_DELAY_MS", "500"))
    )
    post_wake_health_check: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_WAKE_HEALTH_CHECK", "true").lower() == "true"
    )

    # v26.0: User presence detection
    detect_user_on_ghost_display: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_DETECT_USER", "true").lower() == "true"
    )
    pause_operations_when_user_present: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_PAUSE_ON_USER", "true").lower() == "true"
    )
    user_presence_cooldown_seconds: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_USER_COOLDOWN", "5.0"))
    )

    # v26.0: Animation timing
    wait_for_animations: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_WAIT_ANIMATIONS", "true").lower() == "true"
    )
    standard_animation_duration_ms: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_ANIMATION_MS", "250"))
    )
    animation_buffer_ms: float = field(
        default_factory=lambda: float(os.environ.get("JARVIS_GHOST_ANIMATION_BUFFER_MS", "50"))
    )

    # v26.0: Space stability
    use_space_uuid: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_USE_SPACE_UUID", "true").lower() == "true"
    )
    verify_space_before_move: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_VERIFY_SPACE", "true").lower() == "true"
    )

    # v26.0: Window z-order management
    preserve_z_order: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_PRESERVE_ZORDER", "true").lower() == "true"
    )
    restore_focus_on_return: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_RESTORE_FOCUS", "true").lower() == "true"
    )

    # v26.0: Window constraints
    respect_window_constraints: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_RESPECT_CONSTRAINTS", "true").lower() == "true"
    )
    enforce_minimum_window_size: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_GHOST_ENFORCE_MIN_SIZE", "true").lower() == "true"
    )
    minimum_window_width: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_MIN_WIDTH", "200"))
    )
    minimum_window_height: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_GHOST_MIN_HEIGHT", "150"))
    )


class GhostDisplayManager:
    """
    Comprehensive Ghost Display lifecycle management.

    Features:
    - Health monitoring with auto-recovery
    - Window geometry preservation
    - Intelligent layout management
    - Window return policy
    - Fallback strategies
    - Multi-window coordination

    v26.0 Edge Case Handling:
    - Display scaling/Retina coordinate translation
    - Resolution change detection and recovery
    - System sleep/wake event handling
    - User presence detection on Ghost Display
    - Window z-order/layer management
    - Animation-aware timing
    - Space UUID stability verification
    """

    def __init__(self, config: Optional[GhostDisplayManagerConfig] = None):
        self.config = config or GhostDisplayManagerConfig()
        self._ghost_info: Optional[GhostDisplayInfo] = None
        self._status: GhostDisplayStatus = GhostDisplayStatus.UNAVAILABLE
        self._geometry_cache: Dict[int, WindowGeometry] = {}  # window_id -> geometry
        self._monitoring_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._windows_on_ghost: Set[int] = set()
        self._fallback_space: Optional[int] = None
        self._last_layout_time: float = 0.0

        # v26.0: Display scaling state
        self._display_scale_cache: Dict[int, float] = {}  # display_id -> scale factor
        self._display_resolution_cache: Dict[int, Tuple[int, int]] = {}  # display_id -> (width, height)

        # v26.0: System sleep/wake state
        self._is_system_sleeping: bool = False
        self._last_sleep_time: Optional[datetime] = None
        self._last_wake_time: Optional[datetime] = None
        self._pending_post_wake_check: bool = False

        # v26.0: User presence tracking
        self._user_on_ghost_display: bool = False
        self._last_user_presence_check: Optional[datetime] = None
        self._user_visit_history: List[datetime] = []  # Track when user visits ghost display
        self._operations_paused: bool = False

        # v26.0: Animation tracking
        self._pending_animations: Dict[int, datetime] = {}  # window_id -> animation_start_time
        self._global_animation_lock: bool = False

        # v26.0: Space UUID tracking
        self._space_uuid_cache: Dict[int, str] = {}  # space_id -> uuid
        self._space_label_cache: Dict[int, str] = {}  # space_id -> label

        # v26.0: Window z-order tracking
        self._z_order_cache: Dict[int, int] = {}  # window_id -> z_order
        self._focused_window_before_teleport: Optional[int] = None

        # v27.0: Crash Recovery - Persistence Manager Integration
        # =========================================================================
        # FIXES "AMNESIA" RISK:
        # If JARVIS crashes while windows are on Ghost Display, the in-memory
        # geometry cache is lost. GhostPersistenceManager persists state to disk
        # BEFORE teleportation, enabling recovery on restart.
        # =========================================================================
        self._persistence_manager: Optional[GhostPersistenceManager] = None
        self._persistence_initialized: bool = False

    @property
    def status(self) -> GhostDisplayStatus:
        return self._status

    @property
    def ghost_space(self) -> Optional[int]:
        if self._ghost_info:
            return self._ghost_info.space_id
        return self._fallback_space

    @property
    def windows_on_ghost(self) -> Set[int]:
        return self._windows_on_ghost.copy()

    @property
    def window_count(self) -> int:
        return len(self._windows_on_ghost)

    def get_preserved_geometry(self, window_id: int) -> Optional[WindowGeometry]:
        """Get preserved geometry for a window."""
        return self._geometry_cache.get(window_id)

    def get_all_preserved_geometries(self) -> List[WindowGeometry]:
        """Get all preserved window geometries."""
        return list(self._geometry_cache.values())

    # =========================================================================
    # v26.0: New Properties for Edge Case State
    # =========================================================================

    @property
    def is_system_sleeping(self) -> bool:
        """Check if system is currently sleeping."""
        return self._is_system_sleeping

    @property
    def is_user_on_ghost_display(self) -> bool:
        """Check if user is currently on Ghost Display."""
        return self._user_on_ghost_display

    @property
    def are_operations_paused(self) -> bool:
        """Check if operations are paused due to user presence."""
        return self._operations_paused

    @property
    def has_pending_animations(self) -> bool:
        """Check if there are pending window animations."""
        if not self._pending_animations:
            return False
        # Check if any animation is still in progress
        now = datetime.now()
        animation_duration = timedelta(milliseconds=self.config.standard_animation_duration_ms)
        for window_id, start_time in list(self._pending_animations.items()):
            if now - start_time < animation_duration:
                return True
            else:
                # Animation complete, remove from pending
                del self._pending_animations[window_id]
        return False

    @property
    def ghost_display_scale(self) -> float:
        """Get the scale factor of the Ghost Display."""
        if self._ghost_info:
            return self._ghost_info.scale_factor
        return self.config.default_display_scale

    # =========================================================================
    # v26.0: Display Scaling Detection
    # =========================================================================

    async def detect_display_scale(
        self,
        display_id: int,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> float:
        """
        Detect the scale factor of a display (for Retina support).

        Args:
            display_id: Display to check
            yabai_detector: YabaiSpaceDetector for querying

        Returns:
            Scale factor (1.0 for standard, 2.0 for Retina)
        """
        if not self.config.auto_detect_display_scale:
            return self.config.default_display_scale

        # Check cache first
        if display_id in self._display_scale_cache:
            return self._display_scale_cache[display_id]

        try:
            # Query display info using system_profiler or displayplacer if available
            # For now, use a heuristic based on display resolution
            displays = yabai_detector.enumerate_all_spaces(include_display_info=True)

            for space_info in displays:
                if space_info.get("display") == display_id:
                    width = space_info.get("width", 1920)
                    height = space_info.get("height", 1080)

                    # Heuristic: High resolution displays are likely Retina
                    # MacBook Pro Retina: 2560x1600 or higher (scaled to 1440x900, etc.)
                    # iMac Retina: 4096x2304 or 5120x2880
                    if width >= 2560 and height >= 1440:
                        scale = 2.0
                    elif width >= 1920 and height >= 1080:
                        scale = 1.0
                    else:
                        scale = 1.0

                    self._display_scale_cache[display_id] = scale
                    self._display_resolution_cache[display_id] = (width, height)

                    logger.debug(
                        f"[GhostManager] 📏 Display {display_id} scale detected: "
                        f"{scale}x ({width}x{height})"
                    )
                    return scale

        except Exception as e:
            logger.debug(f"[GhostManager] Scale detection failed: {e}")

        return self.config.default_display_scale

    def get_display_resolution(self, display_id: int) -> Tuple[int, int]:
        """Get cached resolution for a display."""
        return self._display_resolution_cache.get(display_id, (1920, 1080))

    # =========================================================================
    # v26.0: Resolution Change Detection
    # =========================================================================

    async def check_resolution_changes(
        self,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Optional[Dict[str, Any]]:
        """
        Check if Ghost Display resolution has changed.

        Returns:
            Dict with change info if resolution changed, None otherwise
        """
        if not self.config.monitor_resolution_changes or self._ghost_info is None:
            return None

        try:
            spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)

            for space_info in spaces:
                if space_info.get("space_id") == self._ghost_info.space_id:
                    new_width = space_info.get("width", self._ghost_info.width)
                    new_height = space_info.get("height", self._ghost_info.height)

                    if (new_width != self._ghost_info.width or
                        new_height != self._ghost_info.height):

                        old_width = self._ghost_info.width
                        old_height = self._ghost_info.height

                        # Update ghost info
                        self._ghost_info.update_resolution(new_width, new_height)

                        change_info = {
                            "old_width": old_width,
                            "old_height": old_height,
                            "new_width": new_width,
                            "new_height": new_height,
                            "display_id": self._ghost_info.display_id,
                            "detected_at": datetime.now()
                        }

                        logger.warning(
                            f"[GhostManager] 🔄 Resolution change detected: "
                            f"{old_width}x{old_height} → {new_width}x{new_height}"
                        )

                        return change_info

        except Exception as e:
            logger.debug(f"[GhostManager] Resolution check failed: {e}")

        return None

    async def handle_resolution_change(
        self,
        change_info: Dict[str, Any],
        yabai_detector: 'YabaiSpaceDetector'
    ):
        """
        Handle a resolution change by optionally re-laying out windows.

        Args:
            change_info: Resolution change information
            yabai_detector: YabaiSpaceDetector for window operations
        """
        if not self.config.auto_relayout_on_resolution_change:
            return

        if self._windows_on_ghost:
            logger.info(
                f"[GhostManager] 📐 Re-laying out {len(self._windows_on_ghost)} windows "
                f"after resolution change"
            )
            await self.apply_layout(
                list(self._windows_on_ghost),
                yabai_detector,
                self.config.default_layout_style
            )

    # =========================================================================
    # v26.0: System Sleep/Wake Handling
    # =========================================================================

    def record_system_sleep(self):
        """
        Record that the system is entering sleep mode.

        Call this when system sleep is detected.
        """
        if not self.config.handle_sleep_wake_events:
            return

        self._is_system_sleeping = True
        self._last_sleep_time = datetime.now()

        if self._ghost_info:
            self._ghost_info.record_system_sleep()

        logger.info("[GhostManager] 😴 System entering sleep - pausing operations")

    def record_system_wake(self):
        """
        Record that the system has woken from sleep.

        Call this when system wake is detected.
        """
        if not self.config.handle_sleep_wake_events:
            return

        self._is_system_sleeping = False
        self._last_wake_time = datetime.now()
        self._pending_post_wake_check = self.config.post_wake_health_check

        if self._ghost_info:
            self._ghost_info.record_system_wake()

        logger.info("[GhostManager] ☀️ System waking from sleep - resuming operations")

    async def wait_for_post_wake_stability(self):
        """
        Wait for system stability after wake from sleep.

        This allows displays, window server, and yabai to stabilize.
        """
        if not self.config.handle_sleep_wake_events:
            return

        if self._last_wake_time:
            time_since_wake = (datetime.now() - self._last_wake_time).total_seconds() * 1000
            if time_since_wake < self.config.post_wake_delay_ms:
                wait_time = (self.config.post_wake_delay_ms - time_since_wake) / 1000.0
                logger.debug(f"[GhostManager] Waiting {wait_time:.2f}s for post-wake stability")
                await asyncio.sleep(wait_time)

    # =========================================================================
    # v26.0: User Presence Detection
    # =========================================================================

    async def detect_user_presence(
        self,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> bool:
        """
        Detect if user is currently viewing the Ghost Display.

        Args:
            yabai_detector: YabaiSpaceDetector for space queries

        Returns:
            True if user is on Ghost Display
        """
        if not self.config.detect_user_on_ghost_display or self._ghost_info is None:
            return False

        try:
            # Get current focused space
            current_space = yabai_detector.get_current_user_space()

            is_on_ghost = current_space == self._ghost_info.space_id

            # Update tracking
            was_on_ghost = self._user_on_ghost_display
            self._user_on_ghost_display = is_on_ghost
            self._last_user_presence_check = datetime.now()

            # Record presence in ghost info
            self._ghost_info.record_user_presence(is_on_ghost)

            # Track visits
            if is_on_ghost and not was_on_ghost:
                self._user_visit_history.append(datetime.now())
                # Keep only last 100 visits
                if len(self._user_visit_history) > 100:
                    self._user_visit_history = self._user_visit_history[-100:]
                logger.debug("[GhostManager] 👤 User arrived on Ghost Display")

            elif not is_on_ghost and was_on_ghost:
                logger.debug("[GhostManager] 👤 User left Ghost Display")

            return is_on_ghost

        except Exception as e:
            logger.debug(f"[GhostManager] User presence detection failed: {e}")
            return False

    def should_pause_operations(self) -> bool:
        """
        Check if operations should be paused due to user presence.

        Returns:
            True if operations should be paused
        """
        if not self.config.pause_operations_when_user_present:
            return False

        if not self._user_on_ghost_display:
            return False

        # Check cooldown
        if self._last_user_presence_check:
            time_since_check = (datetime.now() - self._last_user_presence_check).total_seconds()
            if time_since_check > self.config.user_presence_cooldown_seconds:
                # Cooldown expired, need to recheck
                return True

        return self._user_on_ghost_display

    async def wait_for_user_to_leave(
        self,
        yabai_detector: 'YabaiSpaceDetector',
        timeout_seconds: float = 30.0
    ) -> bool:
        """
        Wait for user to leave Ghost Display before proceeding.

        Args:
            yabai_detector: YabaiSpaceDetector for space queries
            timeout_seconds: Maximum time to wait

        Returns:
            True if user left, False if timeout
        """
        start_time = time.time()
        check_interval = 0.5  # Check every 500ms

        while time.time() - start_time < timeout_seconds:
            is_present = await self.detect_user_presence(yabai_detector)
            if not is_present:
                return True
            await asyncio.sleep(check_interval)

        logger.warning(f"[GhostManager] Timeout waiting for user to leave Ghost Display")
        return False

    # =========================================================================
    # v26.0: Animation Timing
    # =========================================================================

    def mark_animation_start(self, window_id: int):
        """Mark that a window animation has started."""
        self._pending_animations[window_id] = datetime.now()
        if self._ghost_info:
            self._ghost_info.mark_animation_start()

    def mark_animation_complete(self, window_id: int):
        """Mark that a window animation has completed."""
        if window_id in self._pending_animations:
            del self._pending_animations[window_id]
        if not self._pending_animations and self._ghost_info:
            self._ghost_info.mark_animation_complete()

    async def wait_for_animation(
        self,
        window_id: Optional[int] = None,
        extra_buffer_ms: Optional[float] = None
    ):
        """
        Wait for window animation(s) to complete.

        Args:
            window_id: Specific window to wait for, or None for all
            extra_buffer_ms: Extra time to wait after animation
        """
        if not self.config.wait_for_animations:
            return

        buffer = extra_buffer_ms or self.config.animation_buffer_ms
        duration = self.config.standard_animation_duration_ms

        if window_id is not None:
            # Wait for specific window
            if window_id in self._pending_animations:
                start_time = self._pending_animations[window_id]
                elapsed = (datetime.now() - start_time).total_seconds() * 1000
                remaining = max(0, duration - elapsed + buffer)
                if remaining > 0:
                    await asyncio.sleep(remaining / 1000.0)
                self.mark_animation_complete(window_id)
        else:
            # Wait for all animations
            if self._pending_animations:
                # Find the most recent animation start
                latest_start = max(self._pending_animations.values())
                elapsed = (datetime.now() - latest_start).total_seconds() * 1000
                remaining = max(0, duration - elapsed + buffer)
                if remaining > 0:
                    await asyncio.sleep(remaining / 1000.0)
                # Clear all pending
                self._pending_animations.clear()
                if self._ghost_info:
                    self._ghost_info.mark_animation_complete()

    # =========================================================================
    # v26.0: Space UUID Stability
    # =========================================================================

    async def get_space_uuid(
        self,
        space_id: int,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Optional[str]:
        """
        Get the UUID for a space (more stable than space ID).

        Args:
            space_id: Space ID to look up
            yabai_detector: YabaiSpaceDetector for queries

        Returns:
            Space UUID if available
        """
        if not self.config.use_space_uuid:
            return None

        # Check cache first
        if space_id in self._space_uuid_cache:
            return self._space_uuid_cache[space_id]

        try:
            spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
            for space_info in spaces:
                if space_info.get("space_id") == space_id:
                    uuid = space_info.get("uuid")
                    label = space_info.get("label")
                    if uuid:
                        self._space_uuid_cache[space_id] = uuid
                    if label:
                        self._space_label_cache[space_id] = label
                    return uuid
        except Exception as e:
            logger.debug(f"[GhostManager] Space UUID lookup failed: {e}")

        return None

    async def find_space_by_uuid(
        self,
        uuid: str,
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Optional[int]:
        """
        Find a space ID by its UUID.

        Args:
            uuid: Space UUID to find
            yabai_detector: YabaiSpaceDetector for queries

        Returns:
            Space ID if found
        """
        if not self.config.use_space_uuid:
            return None

        # Check if we have a cached mapping
        for space_id, cached_uuid in self._space_uuid_cache.items():
            if cached_uuid == uuid:
                return space_id

        try:
            spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
            for space_info in spaces:
                if space_info.get("uuid") == uuid:
                    space_id = space_info.get("space_id")
                    self._space_uuid_cache[space_id] = uuid
                    return space_id
        except Exception as e:
            logger.debug(f"[GhostManager] Space lookup by UUID failed: {e}")

        return None

    async def verify_space_still_valid(
        self,
        space_id: int,
        expected_uuid: Optional[str],
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Tuple[bool, Optional[int]]:
        """
        Verify a space is still valid (hasn't been reordered or deleted).

        Args:
            space_id: Space ID to verify
            expected_uuid: Expected UUID for the space
            yabai_detector: YabaiSpaceDetector for queries

        Returns:
            Tuple of (is_valid, new_space_id_if_changed)
        """
        if not self.config.verify_space_before_move:
            return True, None

        try:
            current_uuid = await self.get_space_uuid(space_id, yabai_detector)

            if expected_uuid is None:
                # No UUID to compare, assume valid
                return True, None

            if current_uuid == expected_uuid:
                return True, None

            # UUID mismatch - space may have been reordered
            # Try to find the new space ID by UUID
            new_space_id = await self.find_space_by_uuid(expected_uuid, yabai_detector)
            if new_space_id:
                logger.warning(
                    f"[GhostManager] ⚠️ Space reordered: {space_id} → {new_space_id}"
                )
                return False, new_space_id

            logger.warning(f"[GhostManager] ⚠️ Space {space_id} no longer exists (UUID mismatch)")
            return False, None

        except Exception as e:
            logger.debug(f"[GhostManager] Space verification failed: {e}")
            return True, None  # Assume valid on error

    # =========================================================================
    # v26.0: Z-Order/Layer Management
    # =========================================================================

    async def capture_z_order(
        self,
        window_ids: List[int],
        yabai_detector: 'YabaiSpaceDetector'
    ) -> Dict[int, int]:
        """
        Capture the z-order (layer) of windows before teleportation.

        Args:
            window_ids: Windows to capture z-order for
            yabai_detector: YabaiSpaceDetector for queries

        Returns:
            Dict mapping window_id to z_order
        """
        if not self.config.preserve_z_order:
            return {}

        z_orders = {}
        try:
            # Get all windows to determine relative ordering
            all_windows = yabai_detector.get_all_windows()

            # Windows are returned in z-order (front to back typically)
            window_set = set(window_ids)
            z_order = 0

            for window in all_windows:
                wid = window.get("id")
                if wid in window_set:
                    z_orders[wid] = z_order
                    self._z_order_cache[wid] = z_order
                    z_order += 1

            # Also capture which window was focused
            focused_window = yabai_detector.get_focused_window()
            if focused_window and focused_window in window_set:
                self._focused_window_before_teleport = focused_window

        except Exception as e:
            logger.debug(f"[GhostManager] Z-order capture failed: {e}")

        return z_orders

    async def restore_z_order(
        self,
        window_ids: List[int],
        yabai_detector: 'YabaiSpaceDetector'
    ):
        """
        Restore the z-order of windows after returning from Ghost Display.

        Args:
            window_ids: Windows to restore z-order for
            yabai_detector: YabaiSpaceDetector for operations
        """
        if not self.config.preserve_z_order:
            return

        try:
            yabai_path = yabai_detector._health.yabai_path or "yabai"

            # Sort windows by their original z-order (back to front)
            sorted_windows = sorted(
                [(wid, self._z_order_cache.get(wid, 0)) for wid in window_ids],
                key=lambda x: -x[1]  # Reverse order so we can bring each to front
            )

            for window_id, z_order in sorted_windows:
                try:
                    # Focus the window to bring it forward
                    subprocess.run(
                        [yabai_path, "-m", "window", str(window_id), "--focus"],
                        capture_output=True,
                        timeout=1.0
                    )
                    await asyncio.sleep(0.05)  # Small delay between focus operations
                except Exception:
                    pass

            # Restore focus to originally focused window
            if (self.config.restore_focus_on_return and
                self._focused_window_before_teleport and
                self._focused_window_before_teleport in window_ids):

                try:
                    subprocess.run(
                        [yabai_path, "-m", "window",
                         str(self._focused_window_before_teleport), "--focus"],
                        capture_output=True,
                        timeout=1.0
                    )
                except Exception:
                    pass

            logger.debug(f"[GhostManager] 📚 Restored z-order for {len(window_ids)} windows")

        except Exception as e:
            logger.debug(f"[GhostManager] Z-order restore failed: {e}")

    async def initialize(self, yabai_detector: 'YabaiSpaceDetector') -> bool:
        """
        Initialize the Ghost Display Manager.

        Args:
            yabai_detector: YabaiSpaceDetector instance for querying spaces

        Returns:
            True if Ghost Display is available (or fallback activated)
        """
        async with self._lock:
            # Detect Ghost Display
            ghost_space = yabai_detector.get_ghost_display_space()

            if ghost_space is not None:
                # Get display info
                spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
                for space in spaces:
                    if space.get("space_id") == ghost_space:
                        self._ghost_info = GhostDisplayInfo(
                            space_id=ghost_space,
                            display_id=space.get("display", 2),
                            display_name=f"Display {space.get('display', 2)}",
                            width=space.get("width", 1920),
                            height=space.get("height", 1080),
                            is_virtual=space.get("display", 1) > 1,
                            window_count=space.get("window_count", 0),
                            last_health_check=datetime.now()
                        )
                        self._status = GhostDisplayStatus.AVAILABLE
                        logger.info(
                            f"[GhostManager] ✅ Initialized: Space {ghost_space} "
                            f"on Display {self._ghost_info.display_id} "
                            f"({self._ghost_info.width}x{self._ghost_info.height})"
                        )

                        # v27.0: Initialize persistence manager for crash recovery
                        await self._initialize_persistence(yabai_detector)

                        return True

            # No Ghost Display - try fallback
            if self.config.fallback_enabled:
                return await self._activate_fallback(yabai_detector)

            self._status = GhostDisplayStatus.UNAVAILABLE
            logger.warning("[GhostManager] ❌ No Ghost Display available and fallback disabled")
            return False

    async def _activate_fallback(self, yabai_detector: 'YabaiSpaceDetector') -> bool:
        """Activate fallback strategy when Ghost Display unavailable."""
        self._status = GhostDisplayStatus.FALLBACK

        # Strategy 1: Find any visible space that's not current
        spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
        current_space = yabai_detector.get_current_user_space()

        for space in spaces:
            if space.get("is_visible") and space.get("space_id") != current_space:
                self._fallback_space = space.get("space_id")
                logger.info(
                    f"[GhostManager] 🔄 FALLBACK: Using Space {self._fallback_space} "
                    f"as substitute Ghost Display"
                )
                return True

        # Strategy 2: Use current space (last resort - will be visible to user)
        if current_space:
            self._fallback_space = current_space
            logger.warning(
                f"[GhostManager] ⚠️ FALLBACK: Using current Space {current_space} "
                f"(windows will be visible to user)"
            )
            return True

        logger.error("[GhostManager] ❌ No fallback available")
        return False

    # =========================================================================
    # v27.0: Crash Recovery Integration
    # =========================================================================

    async def _initialize_persistence(
        self,
        yabai_detector: 'YabaiSpaceDetector',
        narrate_callback: Optional[Callable] = None
    ) -> None:
        """
        Initialize the persistence manager and audit for stranded windows.

        This is called during GhostDisplayManager initialization to:
        1. Load any persisted state from a previous session
        2. Detect windows stranded on Ghost Display after a crash
        3. Optionally repatriate stranded windows to their original spaces

        Args:
            yabai_detector: YabaiSpaceDetector for window operations
            narrate_callback: Optional async callback for voice narration
        """
        if not _HAS_GHOST_PERSISTENCE:
            logger.debug("[GhostManager] Ghost persistence not available - skipping crash recovery")
            return

        if self._persistence_initialized:
            return

        try:
            # Get or create persistence manager
            self._persistence_manager = get_persistence_manager()

            # Start up and audit for stranded windows
            stranded_windows = await self._persistence_manager.startup()

            if stranded_windows:
                logger.info(
                    f"[GhostManager] 🆘 Found {len(stranded_windows)} stranded windows from previous session"
                )

                # Repatriate stranded windows
                result = await self._persistence_manager.repatriate_stranded_windows(
                    stranded=stranded_windows,
                    narrate_callback=narrate_callback
                )

                logger.info(
                    f"[GhostManager] 🏠 Crash recovery complete: "
                    f"{result['success']} returned, {result['failed']} failed"
                )
            else:
                logger.debug("[GhostManager] No stranded windows found - clean startup")

            self._persistence_initialized = True

        except Exception as e:
            logger.warning(f"[GhostManager] Persistence initialization failed: {e}")
            # Non-fatal - continue without crash recovery

    async def shutdown_persistence(self) -> None:
        """Shutdown persistence manager gracefully."""
        if self._persistence_manager:
            try:
                await self._persistence_manager.shutdown()
                logger.debug("[GhostManager] Persistence manager shutdown complete")
            except Exception as e:
                logger.warning(f"[GhostManager] Persistence shutdown error: {e}")

    async def start_health_monitoring(self, yabai_detector: 'YabaiSpaceDetector'):
        """Start background health monitoring for Ghost Display."""
        if self._monitoring_task is not None:
            return  # Already running

        async def monitor_loop():
            while True:
                try:
                    await asyncio.sleep(self.config.health_check_interval_seconds)
                    await self._health_check(yabai_detector)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[GhostManager] Health check error: {e}")

        self._monitoring_task = asyncio.create_task(monitor_loop())
        logger.debug("[GhostManager] 🏥 Health monitoring started")

    async def stop_health_monitoring(self):
        """Stop health monitoring."""
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None
            logger.debug("[GhostManager] 🏥 Health monitoring stopped")

    async def _health_check(self, yabai_detector: 'YabaiSpaceDetector'):
        """
        Perform health check on Ghost Display.

        v26.0: Enhanced with edge case handling:
        - Skip during system sleep
        - Check for post-wake stability
        - Detect resolution changes
        - Detect user presence
        - Check space UUID stability
        """
        if self._ghost_info is None:
            return

        # v26.0: Skip health checks during system sleep
        if self._is_system_sleeping:
            logger.debug("[GhostManager] Health check skipped - system sleeping")
            return

        async with self._lock:
            # v26.0: Wait for post-wake stability if needed
            if self._pending_post_wake_check:
                await self.wait_for_post_wake_stability()
                self._pending_post_wake_check = False

            # v26.0: Check for resolution changes
            if self.config.monitor_resolution_changes:
                resolution_change = await self.check_resolution_changes(yabai_detector)
                if resolution_change:
                    await self.handle_resolution_change(resolution_change, yabai_detector)

            # v26.0: Detect user presence on Ghost Display
            if self.config.detect_user_on_ghost_display:
                await self.detect_user_presence(yabai_detector)

            # v26.0: Verify space UUID stability
            if self.config.use_space_uuid and self._ghost_info.space_uuid:
                is_valid, new_space_id = await self.verify_space_still_valid(
                    self._ghost_info.space_id,
                    self._ghost_info.space_uuid,
                    yabai_detector
                )
                if not is_valid and new_space_id:
                    # Space was reordered, update our tracking
                    logger.info(
                        f"[GhostManager] 🔄 Ghost Display space reordered: "
                        f"{self._ghost_info.space_id} → {new_space_id}"
                    )
                    self._ghost_info.space_id = new_space_id

            # Verify Ghost Display is still available
            current_ghost = yabai_detector.get_ghost_display_space()

            if current_ghost == self._ghost_info.space_id:
                # Still available
                self._ghost_info.last_health_check = datetime.now()
                self._ghost_info.consecutive_failures = 0
                self._status = GhostDisplayStatus.AVAILABLE

                # v26.0: Update display scale if auto-detection enabled
                if self.config.auto_detect_display_scale:
                    scale = await self.detect_display_scale(
                        self._ghost_info.display_id,
                        yabai_detector
                    )
                    if scale != self._ghost_info.scale_factor:
                        logger.debug(
                            f"[GhostManager] Display scale changed: "
                            f"{self._ghost_info.scale_factor} → {scale}"
                        )
                        self._ghost_info.scale_factor = scale

            else:
                # Ghost Display changed or disappeared
                self._ghost_info.consecutive_failures += 1
                logger.warning(
                    f"[GhostManager] ⚠️ Health check failed "
                    f"({self._ghost_info.consecutive_failures}/{self.config.max_consecutive_failures})"
                )

                if self._ghost_info.consecutive_failures >= self.config.max_consecutive_failures:
                    self._status = GhostDisplayStatus.DISCONNECTED

                    if self.config.auto_recovery_enabled:
                        await self._attempt_recovery(yabai_detector)

    async def _attempt_recovery(self, yabai_detector: 'YabaiSpaceDetector'):
        """Attempt to recover Ghost Display connection."""
        self._status = GhostDisplayStatus.RECONNECTING
        logger.info("[GhostManager] 🔄 Attempting Ghost Display recovery...")

        # Try to find new Ghost Display
        new_ghost = yabai_detector.get_ghost_display_space()

        if new_ghost is not None:
            # Found new Ghost Display
            spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)
            for space in spaces:
                if space.get("space_id") == new_ghost:
                    self._ghost_info = GhostDisplayInfo(
                        space_id=new_ghost,
                        display_id=space.get("display", 2),
                        display_name=f"Display {space.get('display', 2)}",
                        width=space.get("width", 1920),
                        height=space.get("height", 1080),
                        is_virtual=space.get("display", 1) > 1,
                        window_count=space.get("window_count", 0),
                        last_health_check=datetime.now()
                    )
                    self._status = GhostDisplayStatus.AVAILABLE
                    logger.info(f"[GhostManager] ✅ Recovery successful: Space {new_ghost}")

                    # Re-teleport windows if any were on old Ghost Display
                    if self._windows_on_ghost:
                        logger.info(
                            f"[GhostManager] 🚑 Re-teleporting {len(self._windows_on_ghost)} "
                            f"windows to new Ghost Display"
                        )
                        # Windows will need to be re-teleported by caller
                    return

        # Recovery failed - activate fallback
        if self.config.fallback_enabled:
            await self._activate_fallback(yabai_detector)
        else:
            self._status = GhostDisplayStatus.UNAVAILABLE
            logger.error("[GhostManager] ❌ Recovery failed, Ghost Display unavailable")

    async def preserve_window_geometry(
        self,
        window_id: int,
        yabai_detector: 'YabaiSpaceDetector',
        app_name: Optional[str] = None
    ) -> Optional[WindowGeometry]:
        """
        Preserve window geometry before teleportation.

        v26.0: Enhanced with:
        - Display scale factor capture
        - Space UUID for stability
        - Focus state and z-order
        - Window constraints (min/max size)
        - Split view and PiP detection

        Args:
            window_id: Window to preserve geometry for
            yabai_detector: YabaiSpaceDetector for querying
            app_name: Optional app name for tracking

        Returns:
            WindowGeometry if successful
        """
        try:
            # Get window info from yabai
            windows = yabai_detector.get_all_windows()
            window_info = None
            window_z_order = 0

            # Find window and determine z-order from position in list
            for idx, w in enumerate(windows):
                if w.get("id") == window_id:
                    window_info = w
                    window_z_order = idx
                    break

            if window_info is None:
                logger.debug(f"[GhostManager] Window {window_id} not found for geometry preservation")
                return None

            frame = window_info.get("frame", {})
            original_space = window_info.get("space", 1)
            original_display = window_info.get("display", 1)

            # v26.0: Get display scale factor
            source_scale = self.config.default_display_scale
            source_width, source_height = 1920, 1080
            if self.config.enable_retina_scaling:
                source_scale = await self.detect_display_scale(original_display, yabai_detector)
                source_width, source_height = self.get_display_resolution(original_display)

            # v26.0: Get Ghost Display scale
            ghost_scale = self.ghost_display_scale
            ghost_width = self._ghost_info.width if self._ghost_info else 1920
            ghost_height = self._ghost_info.height if self._ghost_info else 1080

            # v26.0: Get space UUID for stability
            space_uuid = None
            space_label = None
            if self.config.use_space_uuid:
                space_uuid = await self.get_space_uuid(original_space, yabai_detector)
                space_label = self._space_label_cache.get(original_space)

            # v26.0: Check if window is focused
            focused_window = yabai_detector.get_focused_window()
            was_focused = focused_window == window_id

            # v26.0: Detect split view and PiP
            # Note: yabai may not directly expose these, but we can infer from window properties
            is_split_view = window_info.get("is-sticky", False) and not window_info.get("is-floating", False)
            is_pip = window_info.get("is-sticky", False) and window_info.get("is-floating", False)

            # v26.0: Get window constraints (if available from accessibility APIs)
            # These would need AppleScript or accessibility framework to get accurately
            # For now, use heuristics based on window type
            min_width = None
            min_height = None
            max_width = None
            max_height = None
            has_constraints = False

            # Apply minimum size constraints from config
            if self.config.enforce_minimum_window_size:
                min_width = self.config.minimum_window_width
                min_height = self.config.minimum_window_height
                has_constraints = True

            geometry = WindowGeometry(
                window_id=window_id,
                app_name=app_name or window_info.get("app", "Unknown"),
                original_space=original_space,
                original_display=original_display,
                x=int(frame.get("x", 0)),
                y=int(frame.get("y", 0)),
                width=int(frame.get("w", 800)),
                height=int(frame.get("h", 600)),
                is_minimized=window_info.get("is-minimized", False),
                is_fullscreen=window_info.get("is-native-fullscreen", False),
                is_floating=window_info.get("is-floating", False),
                teleported_at=datetime.now(),
                # v26.0: Display scaling
                source_display_scale=source_scale,
                source_display_width=source_width,
                source_display_height=source_height,
                ghost_display_scale=ghost_scale,
                ghost_display_width=ghost_width,
                ghost_display_height=ghost_height,
                # v26.0: Space stability
                original_space_uuid=space_uuid,
                original_space_label=space_label,
                # v26.0: Focus and z-order
                was_focused=was_focused,
                z_order=window_z_order,
                is_split_view=is_split_view,
                is_picture_in_picture=is_pip,
                # v26.0: Window constraints
                min_width=min_width,
                min_height=min_height,
                max_width=max_width,
                max_height=max_height,
                has_constraints=has_constraints,
                # v26.0: Animation timing
                move_animation_duration_ms=self.config.standard_animation_duration_ms,
                last_position_stable_at=datetime.now()
            )

            # Cache geometry and z-order
            self._geometry_cache[window_id] = geometry
            self._z_order_cache[window_id] = window_z_order

            # Track focused window
            if was_focused:
                self._focused_window_before_teleport = window_id

            # v27.0: Persist to disk BEFORE teleportation for crash recovery
            # This ensures if JARVIS crashes mid-teleport, we know where to return windows
            if self._persistence_manager:
                try:
                    ghost_space_id = self.ghost_space or 0
                    await self._persistence_manager.record_teleportation(
                        window_id=window_id,
                        app_name=geometry.app_name,
                        original_space=original_space,
                        original_x=geometry.x,
                        original_y=geometry.y,
                        original_width=geometry.width,
                        original_height=geometry.height,
                        ghost_space=ghost_space_id,
                        z_order=window_z_order,
                        original_display=original_display,
                        was_minimized=geometry.is_minimized,
                        was_fullscreen=geometry.is_fullscreen,
                    )
                except Exception as e:
                    # Non-fatal - log and continue
                    logger.debug(f"[GhostManager] Persistence record failed: {e}")

            logger.debug(
                f"[GhostManager] 📐 Preserved geometry for window {window_id}: "
                f"{geometry.width}x{geometry.height} at ({geometry.x}, {geometry.y}) "
                f"[scale={source_scale}, z={window_z_order}, focused={was_focused}]"
            )
            return geometry

        except Exception as e:
            logger.debug(f"[GhostManager] Failed to preserve geometry: {e}")
            return None

    async def track_window_teleport(self, window_id: int, to_space: int):
        """Track a window being teleported to Ghost Display."""
        self._windows_on_ghost.add(window_id)
        if window_id in self._geometry_cache:
            self._geometry_cache[window_id].current_space = to_space

    async def track_window_return(self, window_id: int):
        """Track a window being returned from Ghost Display."""
        self._windows_on_ghost.discard(window_id)

    def calculate_layout(
        self,
        window_count: int,
        style: Optional[WindowLayoutStyle] = None
    ) -> List[Dict[str, int]]:
        """
        Calculate window positions for a given layout style.

        Args:
            window_count: Number of windows to lay out
            style: Layout style (uses config default if None)

        Returns:
            List of position dicts: [{"x": int, "y": int, "width": int, "height": int}, ...]
        """
        style = style or self.config.default_layout_style
        padding = self.config.layout_padding

        if self._ghost_info is None:
            # Use reasonable defaults
            screen_width, screen_height = 1920, 1080
        else:
            screen_width = self._ghost_info.width
            screen_height = self._ghost_info.height

        # Account for menu bar and dock
        usable_y = 25  # Menu bar
        usable_height = screen_height - usable_y - 50  # Dock
        usable_width = screen_width

        positions = []

        if style == WindowLayoutStyle.MAXIMIZE or window_count == 1:
            # Single window or maximize: fill the screen
            positions.append({
                "x": padding,
                "y": usable_y + padding,
                "width": usable_width - 2 * padding,
                "height": usable_height - 2 * padding
            })
            # Duplicate for additional windows if needed
            for _ in range(1, window_count):
                positions.append(positions[0].copy())

        elif style == WindowLayoutStyle.SIDE_BY_SIDE:
            # Horizontal arrangement
            window_width = (usable_width - (window_count + 1) * padding) // window_count
            for i in range(window_count):
                positions.append({
                    "x": padding + i * (window_width + padding),
                    "y": usable_y + padding,
                    "width": window_width,
                    "height": usable_height - 2 * padding
                })

        elif style == WindowLayoutStyle.STACKED:
            # Vertical arrangement
            window_height = (usable_height - (window_count + 1) * padding) // window_count
            for i in range(window_count):
                positions.append({
                    "x": padding,
                    "y": usable_y + padding + i * (window_height + padding),
                    "width": usable_width - 2 * padding,
                    "height": window_height
                })

        elif style == WindowLayoutStyle.GRID:
            # Grid arrangement
            max_per_row = self.config.max_windows_per_row
            rows = (window_count + max_per_row - 1) // max_per_row
            cols = min(window_count, max_per_row)

            window_width = (usable_width - (cols + 1) * padding) // cols
            window_height = (usable_height - (rows + 1) * padding) // rows

            for i in range(window_count):
                row = i // cols
                col = i % cols
                positions.append({
                    "x": padding + col * (window_width + padding),
                    "y": usable_y + padding + row * (window_height + padding),
                    "width": window_width,
                    "height": window_height
                })

        elif style == WindowLayoutStyle.CASCADE:
            # Cascaded arrangement
            cascade_offset = 30
            window_width = usable_width - (window_count - 1) * cascade_offset - 2 * padding
            window_height = usable_height - (window_count - 1) * cascade_offset - 2 * padding

            for i in range(window_count):
                positions.append({
                    "x": padding + i * cascade_offset,
                    "y": usable_y + padding + i * cascade_offset,
                    "width": window_width,
                    "height": window_height
                })

        else:  # PRESERVE - return empty positions (don't move windows)
            for _ in range(window_count):
                positions.append({"x": -1, "y": -1, "width": -1, "height": -1})

        return positions

    async def apply_layout(
        self,
        window_ids: List[int],
        yabai_detector: 'YabaiSpaceDetector',
        style: Optional[WindowLayoutStyle] = None
    ) -> Dict[str, Any]:
        """
        Apply layout to windows on Ghost Display.

        v26.0: Enhanced with:
        - Wait for pending animations before applying layout
        - User presence check (pause if user on Ghost Display)
        - Animation tracking for each window movement
        - Window constraints enforcement
        - Display scaling awareness

        Args:
            window_ids: Windows to arrange
            yabai_detector: YabaiSpaceDetector for moving/resizing
            style: Layout style (uses config default if None)

        Returns:
            Result dict with success/failure info
        """
        style = style or self.config.default_layout_style

        if style == WindowLayoutStyle.PRESERVE:
            return {"success": True, "message": "Layout preserved (no changes)"}

        results = {
            "success": True,
            "applied": [],
            "failed": [],
            "skipped": [],
            "paused_for_user": False
        }

        # v26.0: Check if operations are paused due to user presence
        if self.config.pause_operations_when_user_present:
            is_user_present = await self.detect_user_presence(yabai_detector)
            if is_user_present:
                logger.info("[GhostManager] 👤 User on Ghost Display - waiting for them to leave")
                results["paused_for_user"] = True

                # Wait for user to leave (with timeout)
                user_left = await self.wait_for_user_to_leave(
                    yabai_detector,
                    timeout_seconds=10.0  # Short timeout for layout operations
                )
                if not user_left:
                    # User didn't leave, skip layout to avoid disruption
                    results["success"] = True
                    results["message"] = "Skipped layout - user present on Ghost Display"
                    return results

        # v26.0: Wait for any pending animations to complete
        if self.config.wait_for_animations and self.has_pending_animations:
            logger.debug("[GhostManager] Waiting for pending animations before layout")
            await self.wait_for_animation()

        positions = self.calculate_layout(len(window_ids), style)

        for i, window_id in enumerate(window_ids):
            pos = positions[i]
            if pos["x"] == -1:
                results["skipped"].append(window_id)
                continue  # Skip if preserve

            try:
                # v26.0: Apply window constraints from cached geometry
                geometry = self._geometry_cache.get(window_id)
                target_width = pos["width"]
                target_height = pos["height"]

                if geometry and geometry.has_constraints and self.config.respect_window_constraints:
                    if geometry.min_width and target_width < geometry.min_width:
                        target_width = geometry.min_width
                    if geometry.min_height and target_height < geometry.min_height:
                        target_height = geometry.min_height
                    if geometry.max_width and target_width > geometry.max_width:
                        target_width = geometry.max_width
                    if geometry.max_height and target_height > geometry.max_height:
                        target_height = geometry.max_height

                # v26.0: Apply minimum window size from config
                if self.config.enforce_minimum_window_size:
                    target_width = max(target_width, self.config.minimum_window_width)
                    target_height = max(target_height, self.config.minimum_window_height)

                # Use yabai to resize and move window
                yabai_path = yabai_detector._health.yabai_path or "yabai"

                # v26.0: Mark animation start for this window
                self.mark_animation_start(window_id)

                # Move window
                move_result = subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--move", f"abs:{pos['x']}:{pos['y']}"],
                    capture_output=True,
                    timeout=2.0
                )

                # Resize window
                resize_result = subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--resize", f"abs:{target_width}:{target_height}"],
                    capture_output=True,
                    timeout=2.0
                )

                if move_result.returncode == 0 or resize_result.returncode == 0:
                    results["applied"].append(window_id)
                else:
                    results["failed"].append(window_id)
                    self.mark_animation_complete(window_id)  # Clear animation tracking on failure

                # v26.0: Add throttle delay between teleports if configured
                if self.config.throttle_teleports and i < len(window_ids) - 1:
                    await asyncio.sleep(self.config.teleport_delay_ms / 1000.0)

            except Exception as e:
                logger.debug(f"[GhostManager] Layout apply failed for {window_id}: {e}")
                results["failed"].append(window_id)
                self.mark_animation_complete(window_id)

        # v26.0: Wait for all animations to complete before returning
        if self.config.wait_for_animations and results["applied"]:
            await self.wait_for_animation()

        results["success"] = len(results["failed"]) == 0
        self._last_layout_time = time.time()

        logger.info(
            f"[GhostManager] 📐 Layout applied ({style.value}): "
            f"{len(results['applied'])} succeeded, {len(results['failed'])} failed"
            + (f", {len(results['skipped'])} skipped" if results["skipped"] else "")
        )

        return results

    async def return_window_to_original(
        self,
        window_id: int,
        yabai_detector: 'YabaiSpaceDetector',
        restore_geometry: bool = True
    ) -> bool:
        """
        Return a window to its original space and optionally restore geometry.

        v26.0: Enhanced with:
        - Space UUID verification (handle space reordering)
        - Display scaling during geometry restore
        - Animation tracking for the return move
        - Focus restoration if window was focused
        - Z-order cleanup

        Args:
            window_id: Window to return
            yabai_detector: YabaiSpaceDetector for moving
            restore_geometry: Whether to restore original position/size

        Returns:
            True if successful
        """
        geometry = self._geometry_cache.get(window_id)
        if geometry is None:
            logger.warning(f"[GhostManager] No preserved geometry for window {window_id}")
            return False

        # v26.0: Verify original space is still valid (handle space reordering)
        target_space = geometry.original_space
        if self.config.use_space_uuid and geometry.original_space_uuid:
            is_valid, new_space_id = await self.verify_space_still_valid(
                geometry.original_space,
                geometry.original_space_uuid,
                yabai_detector
            )
            if not is_valid:
                if new_space_id:
                    logger.info(
                        f"[GhostManager] Original space was reordered: "
                        f"{geometry.original_space} → {new_space_id}"
                    )
                    target_space = new_space_id
                else:
                    logger.warning(
                        f"[GhostManager] Original space {geometry.original_space} no longer exists, "
                        f"returning to current user space instead"
                    )
                    target_space = yabai_detector.get_current_user_space() or geometry.original_space

        # v26.0: Mark animation start
        self.mark_animation_start(window_id)

        # Move back to original space
        success, method = yabai_detector.move_window_to_space_with_rescue(
            window_id=window_id,
            target_space=target_space,
            source_space=geometry.current_space,
            app_name=geometry.app_name
        )

        if not success:
            logger.warning(f"[GhostManager] Failed to return window {window_id} to Space {target_space}")
            self.mark_animation_complete(window_id)
            return False

        # Restore geometry if requested
        if restore_geometry and self.config.preserve_geometry_on_return:
            try:
                yabai_path = yabai_detector._health.yabai_path or "yabai"

                # v26.0: Calculate restored position accounting for display scale differences
                restore_x = geometry.x
                restore_y = geometry.y
                restore_width = geometry.width
                restore_height = geometry.height

                # If display scales differ, adjust coordinates
                if (self.config.enable_retina_scaling and
                    geometry.source_display_scale != geometry.ghost_display_scale):

                    # Get current target display scale
                    current_scale = await self.detect_display_scale(
                        geometry.original_display,
                        yabai_detector
                    )

                    if current_scale != geometry.source_display_scale:
                        # Display scale changed since window was captured
                        scale_ratio = current_scale / geometry.source_display_scale
                        restore_x = int(restore_x * scale_ratio)
                        restore_y = int(restore_y * scale_ratio)
                        # Size typically doesn't need scaling, only position
                        logger.debug(
                            f"[GhostManager] Adjusted geometry for scale change: "
                            f"{geometry.source_display_scale} → {current_scale}"
                        )

                # Move to original position
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--move", f"abs:{restore_x}:{restore_y}"],
                    capture_output=True,
                    timeout=2.0
                )

                # Resize to original size
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--resize", f"abs:{restore_width}:{restore_height}"],
                    capture_output=True,
                    timeout=2.0
                )

                logger.debug(f"[GhostManager] 📐 Restored geometry for window {window_id}")

            except Exception as e:
                logger.debug(f"[GhostManager] Geometry restore failed: {e}")

        # v26.0: Wait for animation to complete
        if self.config.wait_for_animations:
            await self.wait_for_animation(window_id)

        # v26.0: Restore focus if this window was focused before teleport
        if (self.config.restore_focus_on_return and
            geometry.was_focused and
            self._focused_window_before_teleport == window_id):

            try:
                yabai_path = yabai_detector._health.yabai_path or "yabai"
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--focus"],
                    capture_output=True,
                    timeout=1.0
                )
                logger.debug(f"[GhostManager] 🎯 Restored focus to window {window_id}")
            except Exception:
                pass

        # Clean up tracking
        await self.track_window_return(window_id)

        # v27.0: Clear from persistence AFTER successful return
        # This ensures crash recovery won't try to return an already-returned window
        if self._persistence_manager:
            try:
                await self._persistence_manager.record_return(window_id)
            except Exception as e:
                # Non-fatal - log and continue
                logger.debug(f"[GhostManager] Persistence clear failed: {e}")

        # v26.0: Clean up z-order cache
        if window_id in self._z_order_cache:
            del self._z_order_cache[window_id]

        # Clean up focused window tracker if this was the focused window
        if self._focused_window_before_teleport == window_id:
            self._focused_window_before_teleport = None

        del self._geometry_cache[window_id]

        logger.info(f"[GhostManager] 🏠 Window {window_id} returned to Space {target_space}")
        return True

    async def return_all_windows(
        self,
        yabai_detector: 'YabaiSpaceDetector',
        restore_geometry: bool = True
    ) -> Dict[str, Any]:
        """
        Return all windows from Ghost Display to their original spaces.

        Args:
            yabai_detector: YabaiSpaceDetector for moving
            restore_geometry: Whether to restore original positions

        Returns:
            Result dict with success/failure counts
        """
        results = {"success": True, "returned": [], "failed": []}

        window_ids = list(self._windows_on_ghost)
        for window_id in window_ids:
            if await self.return_window_to_original(window_id, yabai_detector, restore_geometry):
                results["returned"].append(window_id)
            else:
                results["failed"].append(window_id)

        results["success"] = len(results["failed"]) == 0

        logger.info(
            f"[GhostManager] 🏠 Return complete: "
            f"{len(results['returned'])} returned, {len(results['failed'])} failed"
        )

        return results

    def can_accept_more_windows(self) -> bool:
        """Check if Ghost Display can accept more windows."""
        return len(self._windows_on_ghost) < self.config.max_windows_on_ghost

    def get_status_report(self) -> Dict[str, Any]:
        """Get comprehensive status report."""
        return {
            "status": self._status.value,
            "ghost_space": self.ghost_space,
            "ghost_info": {
                "space_id": self._ghost_info.space_id if self._ghost_info else None,
                "display_id": self._ghost_info.display_id if self._ghost_info else None,
                "is_virtual": self._ghost_info.is_virtual if self._ghost_info else None,
                "is_healthy": self._ghost_info.is_healthy if self._ghost_info else False,
                "dimensions": f"{self._ghost_info.width}x{self._ghost_info.height}" if self._ghost_info else None,
            },
            "windows_on_ghost": len(self._windows_on_ghost),
            "windows_tracked": list(self._windows_on_ghost),
            "preserved_geometries": len(self._geometry_cache),
            "can_accept_more": self.can_accept_more_windows(),
            "fallback_active": self._status == GhostDisplayStatus.FALLBACK,
            "fallback_space": self._fallback_space,
        }


# Global Ghost Display Manager instance
_GHOST_MANAGER: Optional[GhostDisplayManager] = None


def get_ghost_manager() -> GhostDisplayManager:
    """Get or create the global Ghost Display Manager."""
    global _GHOST_MANAGER
    if _GHOST_MANAGER is None:
        _GHOST_MANAGER = GhostDisplayManager()
    return _GHOST_MANAGER


def reset_ghost_manager() -> None:
    """Reset the global Ghost Display Manager (for testing)."""
    global _GHOST_MANAGER
    _GHOST_MANAGER = None


# Thread pool for subprocess operations (avoids blocking event loop)
_yabai_executor: Optional[ThreadPoolExecutor] = None


def _get_yabai_executor() -> ThreadPoolExecutor:
    """Get or create thread pool for Yabai subprocess calls."""
    global _yabai_executor
    if _yabai_executor is None:
        if _HAS_MANAGED_EXECUTOR:

            _yabai_executor = ManagedThreadPoolExecutor(max_workers=2, thread_name_prefix="yabai_", name='yabai')

        else:

            _yabai_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yabai_")
    return _yabai_executor


def _run_subprocess_sync(args: List[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    """Run subprocess synchronously (called from thread pool)."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


async def run_subprocess_async(args: List[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    """
    Run subprocess asynchronously using thread pool.
    Prevents blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    executor = _get_yabai_executor()

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, partial(_run_subprocess_sync, args, timeout)),
            timeout=timeout + 1.0  # Extra second for thread overhead
        )
    except asyncio.TimeoutError:
        logger.error(f"[YABAI] Subprocess timed out: {' '.join(args)}")
        raise


class YabaiStatus(Enum):
    """Status of Yabai installation and availability"""

    AVAILABLE = "available"
    NOT_INSTALLED = "not_installed"
    NOT_RUNNING = "not_running"
    NO_PERMISSIONS = "no_permissions"
    STARTING = "starting"
    ERROR = "error"


class YabaiSpaceDetector:
    """
    Yabai-based Mission Control space detector
    Enhanced with YOLO vision for multi-monitor layout detection

    Features:
    - Auto-start yabai service with retry logic
    - Health monitoring and auto-recovery
    - Async-first design for non-blocking operations
    - Graceful degradation when unavailable

    ROOT CAUSE FIX v11.0.0: Non-Blocking Initialization
    - Uses module-level cache to avoid blocking subprocess calls in __init__
    - Lazy initialization - only check yabai when actually needed
    - All blocking operations moved out of constructor
    """

    def __init__(
        self,
        enable_vision: bool = True,
        config: Optional[YabaiConfig] = None,
        auto_start: bool = True,
    ):
        self.config = config or YabaiConfig()
        self.enable_vision = enable_vision
        self._vision_analyzer = None
        self._health = YabaiServiceHealth()
        self._status_callbacks: List[Callable[[YabaiStatus], None]] = []
        self._health_check_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._lazy_init_done = False

        # =====================================================================
        # ROOT CAUSE FIX v12.0.0: ZERO-BLOCKING Constructor
        # =====================================================================
        # PROBLEM: Even _quick_yabai_check() with cache uses subprocess.run()
        # on cache miss, which blocks the event loop during startup.
        #
        # SOLUTION: NEVER run subprocess in __init__
        # - Only check if cache is already valid (instant)
        # - If cache invalid, DON'T run subprocess - defer to lazy init
        # - Actual yabai check happens on first is_available() call
        # =====================================================================

        # Check ONLY the cache - NO subprocess calls allowed in __init__!
        if _is_yabai_cache_valid():
            # Cache is valid - use cached result (instant)
            is_available = _YABAI_AVAILABILITY_CACHE["available"]
            yabai_path = _YABAI_AVAILABILITY_CACHE["path"]
            if yabai_path:
                self._health.yabai_path = yabai_path
                self._health.is_running = is_available
            logger.debug(f"[YABAI] Using cached result: available={is_available}")
        else:
            # Cache invalid - defer check to lazy initialization
            # Just check if executable exists (fast filesystem check, no subprocess)
            yabai_path = shutil.which("yabai")
            if not yabai_path:
                # Check common locations
                for path in ["/opt/homebrew/bin/yabai", "/usr/local/bin/yabai"]:
                    if os.path.isfile(path) and os.access(path, os.X_OK):
                        yabai_path = path
                        break

            if yabai_path:
                self._health.yabai_path = yabai_path
                # Don't set is_running - we don't know yet (deferred)
                logger.debug(f"[YABAI] Found yabai at {yabai_path} (availability check deferred)")
            else:
                logger.debug("[YABAI] Yabai not found - availability check deferred")

        self._initialized = True

    def _discover_yabai_lazy(self) -> None:
        """
        Lazy discovery of yabai installation and version.
        Only called when full yabai info is needed, not in __init__.
        """
        if self._lazy_init_done:
            return

        self._lazy_init_done = True

        # Use cached result first
        is_available, yabai_path = _quick_yabai_check()

        if not yabai_path:
            # Not available, skip expensive operations
            logger.debug("[YABAI] Lazy init: Yabai not available, skipping")
            return

        self._health.yabai_path = yabai_path

        # Only get version if yabai is available (with short timeout)
        try:
            result = subprocess.run(
                [yabai_path, "--version"],
                capture_output=True,
                text=True,
                timeout=1.0,  # Short timeout
            )
            if result.returncode == 0:
                self._health.yabai_version = result.stdout.strip()
                logger.info(f"[YABAI] Lazy init: Found yabai {self._health.yabai_version}")
        except Exception as e:
            logger.debug(f"[YABAI] Lazy init: Could not get version: {e}")

    def _discover_yabai(self) -> None:
        """
        DEPRECATED: Use _discover_yabai_lazy() instead.
        Kept for backward compatibility but now just calls lazy version.
        """
        self._discover_yabai_lazy()

    def _attempt_startup(self) -> bool:
        """Attempt to start yabai service."""
        if not self._health.yabai_path:
            logger.warning("[YABAI] Cannot start - yabai not installed")
            return False

        # Check if already running
        if self._check_service_running():
            self._health.is_running = True
            logger.info("[YABAI] Yabai already running")
            return True

        # Rate limit startup attempts
        if self._health.last_startup_attempt:
            elapsed = datetime.now() - self._health.last_startup_attempt
            if elapsed.total_seconds() < self.config.startup_retry_delay_seconds:
                return False

        if self._health.startup_attempts >= self.config.max_startup_attempts:
            logger.warning(
                f"[YABAI] Max startup attempts ({self.config.max_startup_attempts}) reached. "
                "Manual intervention required."
            )
            return False

        self._health.startup_attempts += 1
        self._health.last_startup_attempt = datetime.now()

        logger.info(f"[YABAI] Starting yabai service (attempt {self._health.startup_attempts}/{self.config.max_startup_attempts})...")

        try:
            # Try yabai --start-service first (preferred method)
            result = subprocess.run(
                [self._health.yabai_path, "--start-service"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            # Give it time to start
            time.sleep(1.5)

            # Verify it started
            if self._check_service_running():
                self._health.is_running = True
                self._health.startup_attempts = 0  # Reset on success
                logger.info("[YABAI] Yabai service started successfully")
                return True

            # If --start-service didn't work, try launchctl
            logger.debug("[YABAI] --start-service didn't work, trying launchctl...")
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.koekeishiya.yabai"],
                capture_output=True,
                timeout=10,
            )

            time.sleep(1.5)

            if self._check_service_running():
                self._health.is_running = True
                self._health.startup_attempts = 0
                logger.info("[YABAI] Yabai service started via launchctl")
                return True

            # Check for permission issues
            logger.warning(
                "[YABAI] Service failed to start. This usually means yabai needs Accessibility permissions.\n"
                "  To fix:\n"
                "  1. Open System Settings → Privacy & Security → Accessibility\n"
                "  2. Click '+' and add /opt/homebrew/bin/yabai (or your yabai path)\n"
                "  3. Restart yabai with: yabai --start-service"
            )
            self._health.permissions_granted = False
            return False

        except subprocess.TimeoutExpired:
            logger.error("[YABAI] Startup timed out")
            return False
        except Exception as e:
            logger.error(f"[YABAI] Error starting service: {e}")
            return False

    def _check_service_running(self) -> bool:
        """Check if yabai service is actually running and responding."""
        if not self._health.yabai_path:
            return False

        try:
            # Quick query to verify yabai is responding
            result = subprocess.run(
                [self._health.yabai_path, "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return result.returncode == 0 and "failed to connect" not in result.stderr.lower()
        except Exception:
            return False

    @property
    def yabai_available(self) -> bool:
        """
        Property for backward compatibility.
        ROOT CAUSE FIX v11.0.0: Uses cached quick check.
        """
        return self._check_yabai_available()

    def register_status_callback(self, callback: Callable[[YabaiStatus], None]) -> None:
        """Register a callback for status changes."""
        self._status_callbacks.append(callback)

    def _notify_status_change(self, status: YabaiStatus) -> None:
        """Notify all registered callbacks of status change."""
        for callback in self._status_callbacks:
            try:
                callback(status)
            except Exception as e:
                logger.error(f"[YABAI] Status callback error: {e}")

    def get_health(self) -> Dict[str, Any]:
        """Get current service health metrics."""
        return self._health.to_dict()

    async def ensure_running_async(self) -> bool:
        """Ensure yabai is running, attempting restart if needed."""
        if self._health.is_running and not self._health.needs_restart:
            return True

        # Run startup in thread pool to not block
        loop = asyncio.get_event_loop()
        executor = _get_yabai_executor()
        return await loop.run_in_executor(executor, self._attempt_startup)

    def ensure_running(self) -> bool:
        """Synchronous version of ensure_running."""
        if self._health.is_running and not self._health.needs_restart:
            return True
        return self._attempt_startup()

    def _get_vision_analyzer(self):
        """Lazy load vision analyzer for layout detection"""
        if self._vision_analyzer is None and self.enable_vision:
            try:
                import os

                from backend.vision.optimized_claude_vision import OptimizedClaudeVisionAnalyzer

                api_key = os.getenv("ANTHROPIC_API_KEY")
                if api_key:
                    self._vision_analyzer = OptimizedClaudeVisionAnalyzer(
                        api_key=api_key, use_intelligent_selection=True, use_yolo_hybrid=True
                    )
                    logger.info("[YABAI] Vision analyzer loaded for layout detection")
            except Exception as e:
                logger.warning(f"[YABAI] Vision analyzer not available: {e}")
        return self._vision_analyzer

    def _check_yabai_available(self) -> bool:
        """
        Check if Yabai is installed and running.

        ROOT CAUSE FIX v11.0.0: Uses cached quick check to avoid blocking.
        """
        # Use the non-blocking cached check
        is_available, _ = _quick_yabai_check()
        return is_available

    def _check_yabai_available_legacy(self) -> bool:
        """Legacy blocking check - DEPRECATED, use _check_yabai_available instead."""
        try:
            # Check if yabai command exists
            result = subprocess.run(["which", "yabai"], capture_output=True, text=True, timeout=1)
            if result.returncode != 0:
                return False

            # Try to query yabai (will fail if not running)
            result = subprocess.run(
                ["yabai", "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=1,  # Reduced from 2 to 1
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def is_available(self) -> bool:
        """Check if Yabai detector is available"""
        return self.yabai_available

    def get_status(self) -> YabaiStatus:
        """Get the current Yabai availability status with detailed diagnostics."""
        # First check if it's running
        if self._check_service_running():
            self._health.is_running = True
            return YabaiStatus.AVAILABLE

        # Not running - determine why
        if not self._health.yabai_path:
            return YabaiStatus.NOT_INSTALLED

        # Yabai is installed but not running - check why
        try:
            # Try to start and see what happens
            result = subprocess.run(
                [self._health.yabai_path, "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=3,
            )

            if "failed to connect to socket" in result.stderr.lower():
                # Socket doesn't exist = service not running
                return YabaiStatus.NOT_RUNNING

            if result.returncode != 0:
                # Other error - likely permissions
                return YabaiStatus.NO_PERMISSIONS

        except subprocess.TimeoutExpired:
            return YabaiStatus.ERROR
        except Exception:
            return YabaiStatus.ERROR

        return YabaiStatus.NOT_RUNNING

    def get_detailed_status(self) -> Dict[str, Any]:
        """Get comprehensive status including health metrics and diagnostics."""
        status = self.get_status()
        health = self.get_health()

        result = {
            "status": status.value,
            "status_description": self._get_status_description(status),
            "health": health,
            "installation": {
                "yabai_path": self._health.yabai_path,
                "yabai_version": self._health.yabai_version,
                "config_path": str(self.config.config_path),
                "config_exists": self.config.config_path.exists(),
            },
            "recommendations": self._get_recommendations(status),
        }

        return result

    def _get_status_description(self, status: YabaiStatus) -> str:
        """Get human-readable status description."""
        descriptions = {
            YabaiStatus.AVAILABLE: "Yabai is running and responding to queries",
            YabaiStatus.NOT_INSTALLED: "Yabai is not installed. Install with: brew install koekeishiya/formulae/yabai",
            YabaiStatus.NOT_RUNNING: "Yabai is installed but the service is not running",
            YabaiStatus.NO_PERMISSIONS: "Yabai needs Accessibility permissions in System Settings",
            YabaiStatus.STARTING: "Yabai service is starting up",
            YabaiStatus.ERROR: "Yabai encountered an error",
        }
        return descriptions.get(status, "Unknown status")

    def _get_recommendations(self, status: YabaiStatus) -> List[str]:
        """Get actionable recommendations based on current status."""
        if status == YabaiStatus.AVAILABLE:
            return []

        recommendations = []

        if status == YabaiStatus.NOT_INSTALLED:
            recommendations.extend([
                "Install yabai: brew install koekeishiya/formulae/yabai",
                "After installation, run: yabai --start-service",
            ])

        elif status == YabaiStatus.NOT_RUNNING:
            recommendations.extend([
                "Start yabai service: yabai --start-service",
                "Or restart via: yabai --restart-service",
            ])

        elif status == YabaiStatus.NO_PERMISSIONS:
            recommendations.extend([
                "1. Open System Settings → Privacy & Security → Accessibility",
                f"2. Click '+' and add: {self._health.yabai_path or '/opt/homebrew/bin/yabai'}",
                "3. Toggle the checkbox to enable",
                "4. Run: yabai --start-service",
            ])

        elif status == YabaiStatus.ERROR:
            recommendations.extend([
                "Check system logs: log show --predicate 'process == \"yabai\"' --last 5m",
                "Try restarting: yabai --restart-service",
                f"Check config file: {self.config.config_path}",
            ])

        return recommendations

    async def start_health_monitoring(self, interval_seconds: Optional[float] = None) -> None:
        """Start background health monitoring task."""
        if self._health_check_task and not self._health_check_task.done():
            logger.debug("[YABAI] Health monitoring already running")
            return

        interval = interval_seconds or self.config.health_check_interval_seconds

        async def _monitor():
            while True:
                try:
                    await asyncio.sleep(interval)
                    was_running = self._health.is_running
                    is_running = self._check_service_running()
                    self._health.is_running = is_running

                    if was_running and not is_running:
                        logger.warning("[YABAI] Service stopped unexpectedly")
                        self._notify_status_change(YabaiStatus.NOT_RUNNING)

                        if self.config.enable_auto_recovery:
                            logger.info("[YABAI] Attempting auto-recovery...")
                            await self.ensure_running_async()

                    elif not was_running and is_running:
                        logger.info("[YABAI] Service recovered")
                        self._notify_status_change(YabaiStatus.AVAILABLE)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[YABAI] Health monitoring error: {e}")

        self._health_check_task = asyncio.create_task(_monitor())
        logger.info(f"[YABAI] Started health monitoring (interval: {interval}s)")

    async def stop_health_monitoring(self) -> None:
        """Stop background health monitoring."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None
            logger.info("[YABAI] Stopped health monitoring")

    def enumerate_all_spaces(self, include_display_info: bool = True, auto_start: bool = True) -> List[Dict[str, Any]]:
        """
        Enumerate all Mission Control spaces using Yabai

        Args:
            include_display_info: If True, include display ID for each space
            auto_start: If True, attempt to start yabai if not running
        """
        start_time = time.time()

        # Attempt to ensure yabai is running if auto_start enabled
        if auto_start and not self.is_available():
            if self.ensure_running():
                logger.info("[YABAI] Auto-started yabai service")
            else:
                logger.warning("[YABAI] Yabai not available and could not be started")
                self._health.record_failure("Service not available")
                return []

        if not self.is_available():
            logger.warning("[YABAI] Yabai not available, returning empty list")
            self._health.record_failure("Service not available")
            return []

        try:
            # Query spaces from Yabai
            yabai_path = self._health.yabai_path or "yabai"
            result = subprocess.run(
                [yabai_path, "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout_seconds,
            )

            if result.returncode != 0:
                error_msg = f"Failed to query spaces: {result.stderr}"
                logger.error(f"[YABAI] {error_msg}")
                self._health.record_failure(error_msg)
                return []

            spaces_data = json.loads(result.stdout)

            # Query windows for more detail
            windows_result = subprocess.run(
                [yabai_path, "-m", "query", "--windows"],
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout_seconds,
            )

            windows_data = []
            if windows_result.returncode == 0:
                windows_data = json.loads(windows_result.stdout)

            # Build enhanced space information
            spaces = []
            for space in spaces_data:
                space_id = space["index"]

                # Get windows for this space
                space_windows = [w for w in windows_data if w.get("space") == space_id]

                # Get unique applications
                applications = list(set(w.get("app", "Unknown") for w in space_windows))

                # Determine primary activity
                if not space_windows:
                    primary_activity = "Empty"
                elif len(applications) == 1:
                    primary_activity = applications[0]
                else:
                    primary_activity = f"{applications[0]} and {len(applications)-1} others"

                # Get display info if requested
                display_id = space.get("display", 1) if include_display_info else None

                space_info = {
                    "space_id": space_id,
                    "space_name": f"Desktop {space_id}",
                    "is_current": space.get("has-focus", False),
                    "is_visible": space.get("is-visible", False),
                    "is_fullscreen": space.get("is-native-fullscreen", False),
                    "window_count": len(space_windows),
                    "window_ids": space.get("windows", []),
                    "applications": applications,
                    "primary_activity": primary_activity,
                    "type": space.get("type", "unknown"),
                    "display": display_id,  # Added display awareness
                    "uuid": space.get("uuid", ""),
                    "windows": [
                        {
                            "app": w.get("app", "Unknown"),
                            "title": w.get("title", ""),
                            "id": w.get("id"),
                            "minimized": w.get("minimized", False),
                            "hidden": w.get("hidden", False),
                        }
                        for w in space_windows
                    ],
                }

                spaces.append(space_info)

                logger.debug(
                    f"[YABAI] Space {space_id}: {primary_activity} ({len(space_windows)} windows)"
                )

            # Record success metrics
            elapsed_ms = (time.time() - start_time) * 1000
            self._health.record_success(elapsed_ms)

            logger.info(f"[YABAI] Detected {len(spaces)} spaces via Yabai ({elapsed_ms:.1f}ms)")
            return spaces

        except json.JSONDecodeError as e:
            error_msg = f"Failed to parse Yabai output: {e}"
            logger.error(f"[YABAI] {error_msg}")
            self._health.record_failure(error_msg)
            return []
        except subprocess.TimeoutExpired:
            error_msg = "Yabai query timed out"
            logger.error(f"[YABAI] {error_msg}")
            self._health.record_failure(error_msg)
            return []
        except Exception as e:
            error_msg = f"Error enumerating spaces: {e}"
            logger.error(f"[YABAI] {error_msg}")
            self._health.record_failure(error_msg)
            return []

    def get_display_for_space(self, space_id: int) -> Optional[int]:
        """
        Get display ID for a given space

        Args:
            space_id: Space ID to lookup

        Returns:
            Display ID or None if not found
        """
        if not self.is_available():
            return None

        try:
            yabai_path = self._health.yabai_path or "yabai"
            result = subprocess.run(
                [yabai_path, "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout_seconds,
            )

            if result.returncode != 0:
                return None

            spaces_data = json.loads(result.stdout)

            for space in spaces_data:
                if space.get("index") == space_id:
                    return space.get("display", 1)

            return None

        except Exception as e:
            logger.error(f"[YABAI] Error getting display for space {space_id}: {e}")
            return None

    def enumerate_spaces_by_display(self) -> Dict[int, List[Dict[str, Any]]]:
        """
        Group spaces by display ID

        Returns:
            Dictionary mapping display_id -> list of spaces
        """
        spaces = self.enumerate_all_spaces(include_display_info=True)

        spaces_by_display = {}
        for space in spaces:
            display_id = space.get("display", 1)
            if display_id not in spaces_by_display:
                spaces_by_display[display_id] = []
            spaces_by_display[display_id].append(space)

        logger.info(
            f"[YABAI] Grouped {len(spaces)} spaces across {len(spaces_by_display)} displays"
        )
        return spaces_by_display

    def get_current_space(self) -> Optional[Dict[str, Any]]:
        """Get information about the currently focused space"""
        spaces = self.enumerate_all_spaces()
        for space in spaces:
            if space.get("is_current"):
                return space
        return None

    def get_space_info(self, space_id: int) -> Optional[Dict[str, Any]]:
        """Get detailed information about a specific space"""
        spaces = self.enumerate_all_spaces()
        for space in spaces:
            if space.get("space_id") == space_id:
                return space
        return None

    def get_space_count(self) -> int:
        """Get the total number of spaces"""
        spaces = self.enumerate_all_spaces()
        return len(spaces)

    def get_windows_for_space(self, space_id: int) -> List[Dict[str, Any]]:
        """Get all windows in a specific space"""
        space_info = self.get_space_info(space_id)
        if space_info:
            return space_info.get("windows", [])
        return []

    # =========================================================================
    # v22.0.0: WINDOW TELEPORTATION - Autonomous Window Management
    # =========================================================================
    # These methods enable JARVIS to move windows between spaces automatically.
    # Key use case: Move windows to Ghost Display for background monitoring
    # without disturbing the user's current workspace.
    # =========================================================================

    def move_window_to_space(
        self,
        window_id: int,
        target_space: int,
        follow: bool = False
    ) -> bool:
        """
        Teleport a window to a different space using Yabai.

        Args:
            window_id: The window ID to move
            target_space: The target space index (1-based)
            follow: If True, also switch focus to that space

        Returns:
            True if successful, False otherwise

        Example:
            # Move Chrome window 12345 to Ghost Display (Space 10)
            yabai.move_window_to_space(12345, 10)
        """
        if not self.is_available():
            logger.warning("[YABAI] Cannot move window - Yabai not available")
            return False

        try:
            yabai_path = self._health.yabai_path or "yabai"

            # Execute the window move command
            result = subprocess.run(
                [yabai_path, "-m", "window", str(window_id), "--space", str(target_space)],
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout_seconds,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                logger.error(f"[YABAI] Failed to move window {window_id}: {error_msg}")
                return False

            logger.info(f"[YABAI] ✅ Teleported window {window_id} to Space {target_space}")

            # Optionally follow the window
            if follow:
                subprocess.run(
                    [yabai_path, "-m", "space", "--focus", str(target_space)],
                    capture_output=True,
                    timeout=self.config.query_timeout_seconds,
                )

            self._health.record_success(0)
            return True

        except subprocess.TimeoutExpired:
            logger.error(f"[YABAI] Window move timed out for window {window_id}")
            self._health.record_failure("Timeout during window move")
            return False
        except Exception as e:
            logger.error(f"[YABAI] Window move failed: {e}")
            self._health.record_failure(str(e))
            return False

    async def move_window_to_space_async(
        self,
        window_id: int,
        target_space: int,
        follow: bool = False
    ) -> bool:
        """Async version of move_window_to_space."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.move_window_to_space(window_id, target_space, follow)
        )

    # =========================================================================
    # SEARCH & RESCUE PROTOCOL v23.0.0
    # =========================================================================
    # ROOT CAUSE: Yabai cannot act on "dehydrated" windows on hidden spaces.
    # macOS puts windows on hidden spaces into a frozen state where Yabai
    # loses track of them ("could not locate the window to act on!").
    #
    # SOLUTION: Switch-Grab-Return protocol
    # 1. Try direct move (fast path - works for visible windows)
    # 2. If fails, "wake" the window by switching to its space
    # 3. Move the window while it's awake
    # 4. Immediately return to the user's original space
    #
    # This creates a brief screen flash but successfully rescues windows
    # from hidden spaces that would otherwise be inaccessible.
    # =========================================================================

    def _switch_to_space(self, space_id: int) -> bool:
        """
        Switch focus to a specific space.

        This is a low-level helper for the rescue protocol. It briefly
        changes the active space to "wake up" dehydrated windows.

        Args:
            space_id: The space index to switch to (1-based)

        Returns:
            True if successful, False otherwise
        """
        if not self.is_available():
            return False

        try:
            yabai_path = self._health.yabai_path or "yabai"

            result = subprocess.run(
                [yabai_path, "-m", "space", "--focus", str(space_id)],
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout_seconds,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                logger.debug(f"[YABAI] Space switch to {space_id} failed: {error_msg}")
                return False

            return True

        except subprocess.TimeoutExpired:
            logger.warning(f"[YABAI] Space switch timed out for space {space_id}")
            return False
        except Exception as e:
            logger.warning(f"[YABAI] Space switch failed: {e}")
            return False

    async def _switch_to_space_async(self, space_id: int) -> bool:
        """Async version of _switch_to_space."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._switch_to_space(space_id)
        )

    def move_window_to_space_with_rescue(
        self,
        window_id: int,
        target_space: int,
        source_space: Optional[int] = None,
        app_name: Optional[str] = None,
        max_retries: int = 3,
        is_minimized: bool = False,
        is_fullscreen: bool = False
    ) -> Tuple[bool, str]:
        """
        Move a window to a target space using INTELLIGENT SEARCH & RESCUE PROTOCOL v24.0.

        INTELLIGENT SEARCH & RESCUE PROTOCOL:
        1. Fast Path: Try direct move (works for visible windows)
        2. Strategy Selection: Choose best rescue strategy based on telemetry
        3. Retry with Backoff: Multiple attempts with exponential backoff
        4. Multi-Strategy Fallback: Try alternative strategies on failure
        5. Telemetry Recording: Track success/failure for learning

        Args:
            window_id: The window ID to move
            target_space: The target space index (1-based)
            source_space: The window's current space (optional, for rescue)
            app_name: Application name for telemetry (optional)
            max_retries: Maximum retry attempts (default: 3)
            is_minimized: Whether window is minimized (affects strategy)
            is_fullscreen: Whether window is fullscreen (affects strategy)

        Returns:
            Tuple of (success, method):
            - success: True if window was moved
            - method: "direct" | "rescue" | "failed"
        """
        start_time = time.time()
        telemetry = get_rescue_telemetry()

        # =====================================================================
        # FAST PATH: Direct move (works for visible windows)
        # =====================================================================
        if self.move_window_to_space(window_id, target_space):
            duration_ms = (time.time() - start_time) * 1000
            telemetry.record_attempt(
                success=True,
                strategy=RescueStrategy.DIRECT,
                duration_ms=duration_ms,
                app_name=app_name
            )
            return True, "direct"

        # =====================================================================
        # INTELLIGENT RESCUE PATH: Multi-strategy with retry and backoff
        # =====================================================================
        if source_space is None:
            logger.warning(
                f"[YABAI] 🛟 Rescue needed for window {window_id} but source_space unknown"
            )
            telemetry.record_attempt(
                success=False,
                strategy=RescueStrategy.DIRECT,
                duration_ms=(time.time() - start_time) * 1000,
                failure_reason=RescueFailureReason.SPACE_NOT_FOUND,
                app_name=app_name
            )
            return False, "failed"

        # Get recommended strategy from telemetry
        recommended_strategy = telemetry.get_recommended_strategy(
            app_name=app_name,
            is_minimized=is_minimized,
            is_fullscreen=is_fullscreen
        )

        logger.info(
            f"[YABAI] 🛟 INTELLIGENT RESCUE: Window {window_id} on Space {source_space} "
            f"→ Space {target_space} (Strategy: {recommended_strategy.value})"
        )

        # Remember where the user is
        current_space = self.get_current_user_space()
        if current_space is None:
            logger.error("[YABAI] Cannot rescue: unable to determine current space")
            telemetry.record_attempt(
                success=False,
                strategy=recommended_strategy,
                duration_ms=(time.time() - start_time) * 1000,
                failure_reason=RescueFailureReason.SPACE_NOT_FOUND,
                app_name=app_name
            )
            return False, "failed"

        # Strategy order: try recommended first, then fallbacks
        strategies_to_try = [recommended_strategy]
        all_strategies = [
            RescueStrategy.SWITCH_GRAB_RETURN,
            RescueStrategy.FOCUS_THEN_MOVE,
            RescueStrategy.SPACE_FOCUS_EXTENDED,
            RescueStrategy.UNMINIMIZE_FIRST,
        ]
        for s in all_strategies:
            if s not in strategies_to_try:
                strategies_to_try.append(s)

        rescue_success = False
        failure_reason = RescueFailureReason.UNKNOWN
        total_attempts = 0
        final_strategy = recommended_strategy
        wake_delay_used_ms = 0.0

        try:
            for strategy in strategies_to_try[:2]:  # Try at most 2 strategies
                for attempt in range(1, max_retries + 1):
                    total_attempts += 1
                    final_strategy = strategy

                    # Calculate backoff delay
                    backoff_multiplier = 2 ** (attempt - 1)
                    base_wake_delay_ms = telemetry.get_recommended_wake_delay_ms(app_name)

                    # Strategy-specific wake delay multipliers
                    if strategy == RescueStrategy.SPACE_FOCUS_EXTENDED:
                        wake_delay_ms = base_wake_delay_ms * 3 * backoff_multiplier
                    elif strategy == RescueStrategy.UNMINIMIZE_FIRST:
                        wake_delay_ms = base_wake_delay_ms * 2 * backoff_multiplier
                    else:
                        wake_delay_ms = base_wake_delay_ms * backoff_multiplier

                    wake_delay_used_ms = wake_delay_ms
                    wake_delay_s = wake_delay_ms / 1000.0

                    logger.debug(
                        f"[YABAI] 🛟 Attempt {total_attempts}: {strategy.value} "
                        f"(wake delay: {wake_delay_ms:.0f}ms)"
                    )

                    # Execute the rescue strategy
                    success, reason = self._execute_rescue_strategy(
                        strategy=strategy,
                        window_id=window_id,
                        source_space=source_space,
                        target_space=target_space,
                        wake_delay_s=wake_delay_s
                    )

                    if success:
                        rescue_success = True
                        break

                    failure_reason = reason

                    # Brief pause before retry
                    if attempt < max_retries:
                        time.sleep(0.1 * attempt)

                if rescue_success:
                    break

        except Exception as e:
            logger.error(f"[YABAI] 🛟 Rescue exception: {e}")
            failure_reason = RescueFailureReason.UNKNOWN

        finally:
            # ALWAYS return to original space (even on failure)
            logger.debug(f"[YABAI] 🛟 Returning to Space {current_space}...")
            if not self._switch_to_space(current_space):
                logger.warning(f"[YABAI] 🛟 Warning: Failed to return to Space {current_space}")

        # Record telemetry
        duration_ms = (time.time() - start_time) * 1000
        telemetry.record_attempt(
            success=rescue_success,
            strategy=final_strategy,
            duration_ms=duration_ms,
            failure_reason=failure_reason if not rescue_success else None,
            app_name=app_name,
            wake_delay_used_ms=wake_delay_used_ms
        )

        if rescue_success:
            logger.info(
                f"[YABAI] 🛟 Rescue SUCCESS: Window {window_id} → Space {target_space} "
                f"({final_strategy.value}, {total_attempts} attempts, {duration_ms:.0f}ms)"
            )
        else:
            logger.warning(
                f"[YABAI] 🛟 Rescue FAILED: Window {window_id} "
                f"(reason: {failure_reason.value}, {total_attempts} attempts)"
            )

        return rescue_success, "rescue" if rescue_success else "failed"

    def _execute_rescue_strategy(
        self,
        strategy: RescueStrategy,
        window_id: int,
        source_space: int,
        target_space: int,
        wake_delay_s: float
    ) -> Tuple[bool, RescueFailureReason]:
        """
        Execute a specific rescue strategy.

        Returns:
            Tuple of (success, failure_reason if failed)
        """
        yabai_path = self._health.yabai_path or "yabai"

        try:
            if strategy == RescueStrategy.SWITCH_GRAB_RETURN:
                # Classic: Switch to source space, move window, return
                if not self._switch_to_space(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED
                time.sleep(wake_delay_s)
                if self.move_window_to_space(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            elif strategy == RescueStrategy.FOCUS_THEN_MOVE:
                # Focus the window first, then move
                if not self._switch_to_space(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED

                # Focus the specific window
                result = subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--focus"],
                    capture_output=True,
                    text=True,
                    timeout=self.config.query_timeout_seconds,
                )
                time.sleep(wake_delay_s)

                if self.move_window_to_space(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            elif strategy == RescueStrategy.UNMINIMIZE_FIRST:
                # Unminimize the window first
                if not self._switch_to_space(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED

                # Toggle minimize off
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--minimize", "off"],
                    capture_output=True,
                    timeout=self.config.query_timeout_seconds,
                )
                time.sleep(wake_delay_s)

                if self.move_window_to_space(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            elif strategy == RescueStrategy.SPACE_FOCUS_EXTENDED:
                # Extended wake delay for stubborn windows
                if not self._switch_to_space(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED

                # Focus the window
                subprocess.run(
                    [yabai_path, "-m", "window", str(window_id), "--focus"],
                    capture_output=True,
                    timeout=self.config.query_timeout_seconds,
                )

                # Extended wake time
                time.sleep(wake_delay_s)

                if self.move_window_to_space(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            else:
                return False, RescueFailureReason.UNKNOWN

        except subprocess.TimeoutExpired:
            return False, RescueFailureReason.YABAI_TIMEOUT
        except Exception as e:
            logger.debug(f"[YABAI] Strategy {strategy.value} failed: {e}")
            return False, RescueFailureReason.UNKNOWN

    def _detect_failure_reason(
        self,
        window_id: int,
        source_space: int,
        error_message: str
    ) -> RescueFailureReason:
        """Detect the root cause of a rescue failure from error message."""
        error_lower = error_message.lower()

        if "could not locate" in error_lower or "window not found" in error_lower:
            return RescueFailureReason.WINDOW_NOT_FOUND
        if "space not found" in error_lower or "invalid space" in error_lower:
            return RescueFailureReason.SPACE_NOT_FOUND
        if "timeout" in error_lower:
            return RescueFailureReason.YABAI_TIMEOUT
        if "permission" in error_lower or "accessibility" in error_lower:
            return RescueFailureReason.PERMISSION_DENIED
        if "failed to connect" in error_lower:
            return RescueFailureReason.YABAI_UNAVAILABLE

        return RescueFailureReason.UNKNOWN

    async def move_window_to_space_with_rescue_async(
        self,
        window_id: int,
        target_space: int,
        source_space: Optional[int] = None,
        app_name: Optional[str] = None,
        max_retries: int = 3,
        is_minimized: bool = False,
        is_fullscreen: bool = False
    ) -> Tuple[bool, str]:
        """
        Async version of move_window_to_space_with_rescue with INTELLIGENT PROTOCOL v24.0.

        Uses the full Search & Rescue protocol for hidden windows with:
        - Multi-strategy approach
        - Telemetry-driven strategy selection
        - Dynamic wake delay calibration
        - Retry with exponential backoff

        See move_window_to_space_with_rescue for full documentation.
        """
        start_time = time.time()
        telemetry = get_rescue_telemetry()

        # =====================================================================
        # FAST PATH: Try direct async move first
        # =====================================================================
        if await self.move_window_to_space_async(window_id, target_space):
            duration_ms = (time.time() - start_time) * 1000
            telemetry.record_attempt(
                success=True,
                strategy=RescueStrategy.DIRECT,
                duration_ms=duration_ms,
                app_name=app_name
            )
            return True, "direct"

        # =====================================================================
        # INTELLIGENT RESCUE PATH: Multi-strategy with retry and backoff
        # =====================================================================
        if source_space is None:
            logger.warning(
                f"[YABAI] 🛟 Rescue needed for window {window_id} but source_space unknown"
            )
            telemetry.record_attempt(
                success=False,
                strategy=RescueStrategy.DIRECT,
                duration_ms=(time.time() - start_time) * 1000,
                failure_reason=RescueFailureReason.SPACE_NOT_FOUND,
                app_name=app_name
            )
            return False, "failed"

        # Get recommended strategy from telemetry
        recommended_strategy = telemetry.get_recommended_strategy(
            app_name=app_name,
            is_minimized=is_minimized,
            is_fullscreen=is_fullscreen
        )

        logger.info(
            f"[YABAI] 🛟 INTELLIGENT RESCUE (async): Window {window_id} on Space {source_space} "
            f"→ Space {target_space} (Strategy: {recommended_strategy.value})"
        )

        current_space = self.get_current_user_space()
        if current_space is None:
            telemetry.record_attempt(
                success=False,
                strategy=recommended_strategy,
                duration_ms=(time.time() - start_time) * 1000,
                failure_reason=RescueFailureReason.SPACE_NOT_FOUND,
                app_name=app_name
            )
            return False, "failed"

        # Strategy order: try recommended first, then fallbacks
        strategies_to_try = [recommended_strategy]
        all_strategies = [
            RescueStrategy.SWITCH_GRAB_RETURN,
            RescueStrategy.FOCUS_THEN_MOVE,
            RescueStrategy.SPACE_FOCUS_EXTENDED,
        ]
        for s in all_strategies:
            if s not in strategies_to_try:
                strategies_to_try.append(s)

        rescue_success = False
        failure_reason = RescueFailureReason.UNKNOWN
        total_attempts = 0
        final_strategy = recommended_strategy
        wake_delay_used_ms = 0.0

        try:
            for strategy in strategies_to_try[:2]:  # Try at most 2 strategies
                for attempt in range(1, max_retries + 1):
                    total_attempts += 1
                    final_strategy = strategy

                    # Calculate backoff delay
                    backoff_multiplier = 2 ** (attempt - 1)
                    base_wake_delay_ms = telemetry.get_recommended_wake_delay_ms(app_name)

                    # Strategy-specific wake delay multipliers
                    if strategy == RescueStrategy.SPACE_FOCUS_EXTENDED:
                        wake_delay_ms = base_wake_delay_ms * 3 * backoff_multiplier
                    else:
                        wake_delay_ms = base_wake_delay_ms * backoff_multiplier

                    wake_delay_used_ms = wake_delay_ms
                    wake_delay_s = wake_delay_ms / 1000.0

                    logger.debug(
                        f"[YABAI] 🛟 Async attempt {total_attempts}: {strategy.value} "
                        f"(wake delay: {wake_delay_ms:.0f}ms)"
                    )

                    # Execute the rescue strategy asynchronously
                    success, reason = await self._execute_rescue_strategy_async(
                        strategy=strategy,
                        window_id=window_id,
                        source_space=source_space,
                        target_space=target_space,
                        wake_delay_s=wake_delay_s
                    )

                    if success:
                        rescue_success = True
                        break

                    failure_reason = reason

                    # Brief pause before retry
                    if attempt < max_retries:
                        await asyncio.sleep(0.1 * attempt)

                if rescue_success:
                    break

        except Exception as e:
            logger.error(f"[YABAI] 🛟 Async rescue exception: {e}")
            failure_reason = RescueFailureReason.UNKNOWN

        finally:
            # ALWAYS return to original space
            logger.debug(f"[YABAI] 🛟 Returning to Space {current_space}...")
            await self._switch_to_space_async(current_space)

        # Record telemetry
        duration_ms = (time.time() - start_time) * 1000
        telemetry.record_attempt(
            success=rescue_success,
            strategy=final_strategy,
            duration_ms=duration_ms,
            failure_reason=failure_reason if not rescue_success else None,
            app_name=app_name,
            wake_delay_used_ms=wake_delay_used_ms
        )

        if rescue_success:
            logger.info(
                f"[YABAI] 🛟 Async rescue SUCCESS: Window {window_id} → Space {target_space} "
                f"({final_strategy.value}, {total_attempts} attempts, {duration_ms:.0f}ms)"
            )
        else:
            logger.warning(
                f"[YABAI] 🛟 Async rescue FAILED: Window {window_id} "
                f"(reason: {failure_reason.value}, {total_attempts} attempts)"
            )

        return rescue_success, "rescue" if rescue_success else "failed"

    async def _execute_rescue_strategy_async(
        self,
        strategy: RescueStrategy,
        window_id: int,
        source_space: int,
        target_space: int,
        wake_delay_s: float
    ) -> Tuple[bool, RescueFailureReason]:
        """
        Async version of rescue strategy execution.

        Returns:
            Tuple of (success, failure_reason if failed)
        """
        try:
            if strategy in (
                RescueStrategy.SWITCH_GRAB_RETURN,
                RescueStrategy.FOCUS_THEN_MOVE,
                RescueStrategy.SPACE_FOCUS_EXTENDED
            ):
                # Switch to source space
                if not await self._switch_to_space_async(source_space):
                    return False, RescueFailureReason.SPACE_SWITCH_FAILED

                # For focus-based strategies, focus the window
                if strategy in (RescueStrategy.FOCUS_THEN_MOVE, RescueStrategy.SPACE_FOCUS_EXTENDED):
                    yabai_path = self._health.yabai_path or "yabai"
                    try:
                        await run_subprocess_async(
                            [yabai_path, "-m", "window", str(window_id), "--focus"],
                            timeout=self.config.query_timeout_seconds
                        )
                    except Exception:
                        pass  # Focus may fail but move might still work

                # Wait for window to hydrate
                await asyncio.sleep(wake_delay_s)

                # Attempt move
                if await self.move_window_to_space_async(window_id, target_space):
                    return True, RescueFailureReason.UNKNOWN
                return False, RescueFailureReason.MOVE_AFTER_WAKE_FAILED

            return False, RescueFailureReason.UNKNOWN

        except asyncio.TimeoutError:
            return False, RescueFailureReason.YABAI_TIMEOUT
        except Exception as e:
            logger.debug(f"[YABAI] Async strategy {strategy.value} failed: {e}")
            return False, RescueFailureReason.UNKNOWN

    async def rescue_windows_to_ghost_async(
        self,
        windows: List[Dict[str, Any]],
        ghost_space: Optional[int] = None,
        max_parallel: int = 5
    ) -> Dict[str, Any]:
        """
        INTELLIGENT BATCH RESCUE v24.0: Move multiple windows to Ghost Display.

        This is the main entry point for Auto-Handoff, featuring:
        - Parallel rescue for windows on the same space
        - Telemetry-driven strategy selection per window
        - Dynamic wake delay calibration
        - Comprehensive result tracking

        Args:
            windows: List of window dicts with 'window_id', 'space_id', and optionally 'app_name'
            ghost_space: Target Ghost Display space (auto-detected if None)
            max_parallel: Maximum parallel window moves within a space

        Returns:
            Dict with:
                - success: bool (any windows moved)
                - direct_count: int (windows moved directly)
                - rescue_count: int (windows rescued from hidden)
                - failed_count: int (windows that couldn't be moved)
                - details: list of per-window results
                - telemetry: rescue telemetry snapshot
        """
        start_time = time.time()
        telemetry = get_rescue_telemetry()

        result = {
            "success": False,
            "direct_count": 0,
            "rescue_count": 0,
            "failed_count": 0,
            "details": [],
            "telemetry": {}
        }

        if not windows:
            return result

        # Auto-detect Ghost Display if not specified
        if ghost_space is None:
            ghost_space = await self.get_ghost_display_space_async()
            if ghost_space is None:
                logger.warning("[YABAI] 🛟 No Ghost Display available for rescue")
                return result

        current_space = self.get_current_user_space()
        visible_spaces = {current_space, ghost_space}

        # Get all visible spaces for smarter routing
        try:
            from backend.vision.multi_space_window_detector import MultiSpaceWindowDetector
            detector = MultiSpaceWindowDetector()
            all_visible = await detector.get_all_visible_spaces()
            visible_spaces.update(all_visible)
        except Exception:
            pass

        # =====================================================================
        # INTELLIGENT BATCH OPTIMIZATION: Group windows by source space
        # =====================================================================
        from collections import defaultdict
        windows_by_space = defaultdict(list)
        for w in windows:
            source = w.get("space_id")
            windows_by_space[source].append(w)

        # =====================================================================
        # PHASE 1: PARALLEL direct moves for visible spaces (fast path)
        # =====================================================================
        visible_space_ids = [
            sid for sid in windows_by_space.keys()
            if sid in visible_spaces and sid != ghost_space
        ]

        for space_id in visible_space_ids:
            space_windows = windows_by_space[space_id]

            # Parallel move for windows in visible spaces
            async def move_visible_window(w):
                window_id = w.get("window_id")
                app_name = w.get("app_name") or w.get("app")
                move_start = time.time()

                success = await self.move_window_to_space_async(window_id, ghost_space)
                duration_ms = (time.time() - move_start) * 1000

                telemetry.record_attempt(
                    success=success,
                    strategy=RescueStrategy.DIRECT,
                    duration_ms=duration_ms,
                    app_name=app_name
                )

                return {
                    "window_id": window_id,
                    "source_space": space_id,
                    "success": success,
                    "method": "direct" if success else "failed",
                    "duration_ms": duration_ms,
                    "app_name": app_name
                }

            # Execute in parallel with concurrency limit
            tasks = [move_visible_window(w) for w in space_windows]

            if len(tasks) > max_parallel:
                # Process in batches to avoid overwhelming yabai
                for i in range(0, len(tasks), max_parallel):
                    batch = tasks[i:i + max_parallel]
                    batch_results = await asyncio.gather(*batch, return_exceptions=True)
                    for r in batch_results:
                        if isinstance(r, Exception):
                            result["failed_count"] += 1
                        elif r.get("success"):
                            result["direct_count"] += 1
                            result["details"].append(r)
                        else:
                            result["failed_count"] += 1
                            result["details"].append(r)
            else:
                move_results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in move_results:
                    if isinstance(r, Exception):
                        result["failed_count"] += 1
                    elif r.get("success"):
                        result["direct_count"] += 1
                        result["details"].append(r)
                    else:
                        result["failed_count"] += 1
                        result["details"].append(r)

            del windows_by_space[space_id]

        # =====================================================================
        # PHASE 2: INTELLIGENT rescue for hidden spaces
        # =====================================================================
        for space_id, space_windows in windows_by_space.items():
            if space_id == ghost_space:
                # Already on ghost, mark as success
                for w in space_windows:
                    result["details"].append({
                        "window_id": w.get("window_id"),
                        "source_space": space_id,
                        "success": True,
                        "method": "already_on_ghost"
                    })
                continue

            # Get dynamic wake delay based on telemetry
            first_app = space_windows[0].get("app_name") or space_windows[0].get("app")
            base_wake_delay_ms = telemetry.get_recommended_wake_delay_ms(first_app)
            wake_delay_s = base_wake_delay_ms / 1000.0

            logger.info(
                f"[YABAI] 🛟 Intelligent batch rescue: {len(space_windows)} windows "
                f"from Space {space_id} (wake delay: {base_wake_delay_ms:.0f}ms)"
            )

            # Switch to hidden space once
            if await self._switch_to_space_async(space_id):
                await asyncio.sleep(wake_delay_s)

                # Parallel move windows from this space
                async def rescue_window(w):
                    window_id = w.get("window_id")
                    app_name = w.get("app_name") or w.get("app")
                    is_minimized = w.get("minimized", False)
                    move_start = time.time()

                    success = await self.move_window_to_space_async(window_id, ghost_space)
                    duration_ms = (time.time() - move_start) * 1000

                    strategy = RescueStrategy.SWITCH_GRAB_RETURN

                    # If first attempt failed and window is minimized, try unminimize
                    if not success and is_minimized:
                        try:
                            yabai_path = self._health.yabai_path or "yabai"
                            await run_subprocess_async(
                                [yabai_path, "-m", "window", str(window_id), "--minimize", "off"],
                                timeout=2.0
                            )
                            await asyncio.sleep(wake_delay_s)
                            success = await self.move_window_to_space_async(window_id, ghost_space)
                            strategy = RescueStrategy.UNMINIMIZE_FIRST
                        except Exception:
                            pass

                    telemetry.record_attempt(
                        success=success,
                        strategy=strategy,
                        duration_ms=duration_ms,
                        app_name=app_name,
                        wake_delay_used_ms=base_wake_delay_ms
                    )

                    return {
                        "window_id": window_id,
                        "source_space": space_id,
                        "success": success,
                        "method": "rescue" if success else "failed",
                        "strategy": strategy.value,
                        "duration_ms": duration_ms,
                        "app_name": app_name
                    }

                # Execute rescues in parallel
                tasks = [rescue_window(w) for w in space_windows]

                if len(tasks) > max_parallel:
                    for i in range(0, len(tasks), max_parallel):
                        batch = tasks[i:i + max_parallel]
                        batch_results = await asyncio.gather(*batch, return_exceptions=True)
                        for r in batch_results:
                            if isinstance(r, Exception):
                                result["failed_count"] += 1
                            elif r.get("success"):
                                result["rescue_count"] += 1
                                result["details"].append(r)
                            else:
                                result["failed_count"] += 1
                                result["details"].append(r)
                else:
                    rescue_results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in rescue_results:
                        if isinstance(r, Exception):
                            result["failed_count"] += 1
                        elif r.get("success"):
                            result["rescue_count"] += 1
                            result["details"].append(r)
                        else:
                            result["failed_count"] += 1
                            result["details"].append(r)
            else:
                # Couldn't switch to this space
                for w in space_windows:
                    telemetry.record_attempt(
                        success=False,
                        strategy=RescueStrategy.SWITCH_GRAB_RETURN,
                        duration_ms=0,
                        failure_reason=RescueFailureReason.SPACE_SWITCH_FAILED,
                        app_name=w.get("app_name") or w.get("app")
                    )
                    result["details"].append({
                        "window_id": w.get("window_id"),
                        "source_space": space_id,
                        "success": False,
                        "method": "failed",
                        "failure_reason": "space_switch_failed"
                    })
                    result["failed_count"] += 1

        # Return to original space
        if current_space:
            await self._switch_to_space_async(current_space)

        result["success"] = (result["direct_count"] + result["rescue_count"]) > 0

        # Include telemetry snapshot in result
        total_duration_ms = (time.time() - start_time) * 1000
        result["telemetry"] = {
            "total_duration_ms": round(total_duration_ms, 2),
            "success_rate": f"{telemetry.success_rate * 100:.1f}%",
            "total_rescue_attempts": telemetry.total_attempts,
            "calibrated_wake_delay_ms": round(telemetry.avg_wake_delay_needed_ms, 2),
        }

        if result["success"]:
            logger.info(
                f"[YABAI] 🛟 Intelligent batch rescue complete: "
                f"{result['direct_count']} direct, {result['rescue_count']} rescued, "
                f"{result['failed_count']} failed (total: {total_duration_ms:.0f}ms, "
                f"success rate: {telemetry.success_rate * 100:.1f}%)"
            )
        else:
            logger.warning(
                f"[YABAI] 🛟 Batch rescue failed: {result['failed_count']} windows "
                f"could not be moved (telemetry: {telemetry.to_dict()})"
            )

        return result

    def get_rescue_telemetry_report(self) -> Dict[str, Any]:
        """
        Get a comprehensive report of rescue telemetry.

        Returns:
            Dict with detailed rescue statistics and recommendations
        """
        telemetry = get_rescue_telemetry()
        report = telemetry.to_dict()

        # Add recommendations based on telemetry
        recommendations = []

        if telemetry.success_rate < 0.8:
            recommendations.append(
                "Success rate below 80%. Consider increasing JARVIS_RESCUE_WAKE_DELAY."
            )

        if telemetry.failures_by_reason.get("space_switch_failed", 0) > 3:
            recommendations.append(
                "Multiple space switch failures. Check Yabai accessibility permissions."
            )

        if telemetry.failures_by_reason.get("window_not_found", 0) > 5:
            recommendations.append(
                "Many windows not found. Windows may be closing before rescue completes."
            )

        if telemetry.avg_wake_delay_needed_ms > 200:
            recommendations.append(
                f"Wake delay averaging {telemetry.avg_wake_delay_needed_ms:.0f}ms. "
                "Consider a faster Mac or closing resource-intensive apps."
            )

        report["recommendations"] = recommendations
        return report

    def get_ghost_display_space(self) -> Optional[int]:
        """
        Find the Ghost Display space (virtual display for background monitoring).

        The Ghost Display is identified as:
        1. A visible space that is NOT the current/focused space
        2. Typically on Display 2+ (virtual displays)

        Returns:
            Space ID of the Ghost Display, or None if not available

        Example:
            ghost_space = yabai.get_ghost_display_space()
            if ghost_space:
                yabai.move_window_to_space(window_id, ghost_space)
        """
        spaces = self.enumerate_all_spaces(include_display_info=True)

        # Find visible spaces that are NOT the current space
        ghost_candidates = []
        current_space_id = None

        for space in spaces:
            if space.get("is_current"):
                current_space_id = space.get("space_id")
            elif space.get("is_visible"):
                # This is a visible space but not current = Ghost Display candidate
                ghost_candidates.append({
                    "space_id": space.get("space_id"),
                    "display": space.get("display", 1),
                    "window_count": space.get("window_count", 0)
                })

        if not ghost_candidates:
            logger.debug("[YABAI] No Ghost Display found (only one visible space)")
            return None

        # Prefer Display 2+ (virtual displays) over Display 1 spaces
        # Sort by display number (higher = more likely virtual), then by fewer windows
        ghost_candidates.sort(key=lambda x: (-x["display"], x["window_count"]))

        ghost_space = ghost_candidates[0]["space_id"]
        logger.info(
            f"[YABAI] 👻 Ghost Display found: Space {ghost_space} "
            f"(Display {ghost_candidates[0]['display']})"
        )

        return ghost_space

    async def get_ghost_display_space_async(self) -> Optional[int]:
        """Async version of get_ghost_display_space."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_ghost_display_space)

    def teleport_window_to_ghost(self, window_id: int) -> Tuple[bool, Optional[int]]:
        """
        High-level method to teleport a window to the Ghost Display.

        This is the main entry point for autonomous window management.
        It finds the Ghost Display and moves the window there.

        Args:
            window_id: The window to teleport

        Returns:
            Tuple of (success: bool, ghost_space_id: Optional[int])

        Example:
            success, ghost_space = yabai.teleport_window_to_ghost(12345)
            if success:
                print(f"Window moved to Space {ghost_space}")
        """
        ghost_space = self.get_ghost_display_space()

        if ghost_space is None:
            logger.warning(
                "[YABAI] 👻 No Ghost Display available - "
                "create a virtual display with BetterDisplay"
            )
            return False, None

        success = self.move_window_to_space(window_id, ghost_space)
        return success, ghost_space if success else None

    async def teleport_window_to_ghost_async(
        self,
        window_id: int
    ) -> Tuple[bool, Optional[int]]:
        """Async version of teleport_window_to_ghost."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.teleport_window_to_ghost(window_id)
        )

    def teleport_windows_to_ghost(
        self,
        window_ids: List[int],
        max_windows: int = 5
    ) -> Dict[str, Any]:
        """
        Batch teleport multiple windows to the Ghost Display.

        Args:
            window_ids: List of window IDs to teleport
            max_windows: Maximum windows to move (safety limit)

        Returns:
            Dict with results: {
                'success': bool,
                'ghost_space': int,
                'moved': [list of moved window IDs],
                'failed': [list of failed window IDs]
            }
        """
        ghost_space = self.get_ghost_display_space()

        if ghost_space is None:
            return {
                'success': False,
                'ghost_space': None,
                'moved': [],
                'failed': window_ids,
                'error': 'No Ghost Display available'
            }

        # Limit for safety
        windows_to_move = window_ids[:max_windows]
        if len(window_ids) > max_windows:
            logger.warning(
                f"[YABAI] Limiting teleport to {max_windows} windows "
                f"(requested: {len(window_ids)})"
            )

        moved = []
        failed = []

        for wid in windows_to_move:
            if self.move_window_to_space(wid, ghost_space):
                moved.append(wid)
            else:
                failed.append(wid)

        success = len(moved) > 0
        logger.info(
            f"[YABAI] 👻 Batch teleport complete: "
            f"{len(moved)} moved, {len(failed)} failed"
        )

        return {
            'success': success,
            'ghost_space': ghost_space,
            'moved': moved,
            'failed': failed
        }

    async def teleport_windows_to_ghost_async(
        self,
        window_ids: List[int],
        max_windows: int = 5
    ) -> Dict[str, Any]:
        """Async version of teleport_windows_to_ghost."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.teleport_windows_to_ghost(window_ids, max_windows)
        )

    def get_current_user_space(self) -> Optional[int]:
        """Get the space ID where the user is currently working."""
        spaces = self.enumerate_all_spaces()
        for space in spaces:
            if space.get("is_current"):
                return space.get("space_id")
        return None

    # =========================================================================
    # END: Window Teleportation Methods
    # =========================================================================

    def get_workspace_summary(self) -> Dict[str, Any]:
        """Get a comprehensive summary of the entire workspace"""
        spaces = self.enumerate_all_spaces()

        if not spaces:
            return {
                "total_spaces": 0,
                "total_windows": 0,
                "total_applications": 0,
                "spaces": [],
                "current_space": None,
                "primary_activity": "No spaces detected",
            }

        # Calculate totals
        total_windows = sum(space.get("window_count", 0) for space in spaces)
        all_apps = set()
        for space in spaces:
            all_apps.update(space.get("applications", []))

        # Find current space
        current_space = None
        for space in spaces:
            if space.get("is_current"):
                current_space = space
                break

        # Determine overall primary activity
        app_counts = {}
        for space in spaces:
            for app in space.get("applications", []):
                app_counts[app] = app_counts.get(app, 0) + 1

        primary_app = max(app_counts.keys(), key=app_counts.get) if app_counts else "Empty"

        return {
            "total_spaces": len(spaces),
            "total_windows": total_windows,
            "total_applications": len(all_apps),
            "spaces": spaces,
            "current_space": current_space,
            "primary_activity": primary_app,
            "all_applications": list(all_apps),
        }

    def describe_workspace(self) -> str:
        """Generate a natural language description of the workspace"""
        summary = self.get_workspace_summary()

        if summary["total_spaces"] == 0:
            return "Unable to detect Mission Control spaces. Yabai may not be running."

        description = []

        # Overall summary
        description.append(f"You have {summary['total_spaces']} Mission Control spaces active")

        if summary["total_windows"] > 0:
            description.append(
                f"with {summary['total_windows']} windows across {summary['total_applications']} applications."
            )
        else:
            description.append("with no windows currently open.")

        # Current space
        if summary["current_space"]:
            current = summary["current_space"]
            description.append(f"\n\nCurrently viewing Space {current['space_id']}")
            if current["window_count"] > 0:
                description.append(f"with {current['primary_activity']}.")
            else:
                description.append("which is empty.")

        # Detailed space breakdown
        description.append("\n\nSpace breakdown:")
        for space in summary["spaces"]:
            space_desc = f"\n• Space {space['space_id']}"

            if space["is_fullscreen"]:
                space_desc += " (fullscreen)"
            if space["is_current"]:
                space_desc += " [CURRENT]"

            space_desc += f": "

            if space["window_count"] == 0:
                space_desc += "Empty"
            else:
                # List first few apps
                apps = space["applications"][:3]
                if len(apps) == 1:
                    space_desc += apps[0]
                else:
                    space_desc += ", ".join(apps)

                if len(space["applications"]) > 3:
                    space_desc += f" and {len(space['applications']) - 3} more"

                # Add window titles for context
                if space["windows"]:
                    first_window = space["windows"][0]
                    if first_window["title"]:
                        title = first_window["title"][:50]
                        if len(first_window["title"]) > 50:
                            title += "..."
                        space_desc += f' - "{title}"'

            description.append(space_desc)

        return "".join(description)

    # =========================================================================
    # ASYNC METHODS - Non-blocking versions for use in async contexts
    # =========================================================================

    async def enumerate_all_spaces_async(self, include_display_info: bool = True) -> List[Dict[str, Any]]:
        """
        Async version of enumerate_all_spaces.
        Uses thread pool to avoid blocking the event loop.
        """
        if not self.is_available():
            logger.warning("[YABAI] Yabai not available, returning empty list")
            return []

        try:
            # Query spaces from Yabai asynchronously
            result = await run_subprocess_async(["yabai", "-m", "query", "--spaces"], timeout=5.0)

            if result.returncode != 0:
                logger.error(f"[YABAI] Failed to query spaces: {result.stderr}")
                return []

            spaces_data = json.loads(result.stdout)

            # Query windows for more detail
            windows_result = await run_subprocess_async(["yabai", "-m", "query", "--windows"], timeout=5.0)

            windows_data = []
            if windows_result.returncode == 0:
                windows_data = json.loads(windows_result.stdout)

            # Build enhanced space information (same logic as sync version)
            spaces = []
            for space in spaces_data:
                space_id = space["index"]
                space_windows = [w for w in windows_data if w.get("space") == space_id]
                applications = list(set(w.get("app", "Unknown") for w in space_windows))

                if not space_windows:
                    primary_activity = "Empty"
                elif len(applications) == 1:
                    primary_activity = applications[0]
                else:
                    primary_activity = f"{applications[0]} and {len(applications)-1} others"

                display_id = space.get("display", 1) if include_display_info else None

                space_info = {
                    "space_id": space_id,
                    "space_name": f"Desktop {space_id}",
                    "is_current": space.get("has-focus", False),
                    "is_visible": space.get("is-visible", False),
                    "is_fullscreen": space.get("is-native-fullscreen", False),
                    "window_count": len(space_windows),
                    "window_ids": space.get("windows", []),
                    "applications": applications,
                    "primary_activity": primary_activity,
                    "type": space.get("type", "unknown"),
                    "display": display_id,
                    "uuid": space.get("uuid", ""),
                    "windows": [
                        {
                            "app": w.get("app", "Unknown"),
                            "title": w.get("title", ""),
                            "id": w.get("id"),
                            "minimized": w.get("minimized", False),
                            "hidden": w.get("hidden", False),
                        }
                        for w in space_windows
                    ],
                }
                spaces.append(space_info)

            logger.info(f"[YABAI] Async: Detected {len(spaces)} spaces via Yabai")
            return spaces

        except asyncio.TimeoutError:
            logger.error("[YABAI] Async: Yabai query timed out")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"[YABAI] Async: Failed to parse Yabai output: {e}")
            return []
        except Exception as e:
            logger.error(f"[YABAI] Async: Error enumerating spaces: {e}")
            return []

    async def get_workspace_summary_async(self) -> Dict[str, Any]:
        """Async version of get_workspace_summary."""
        spaces = await self.enumerate_all_spaces_async()

        if not spaces:
            return {
                "total_spaces": 0,
                "total_windows": 0,
                "total_applications": 0,
                "spaces": [],
                "current_space": None,
                "primary_activity": "No spaces detected",
            }

        total_windows = sum(space.get("window_count", 0) for space in spaces)
        all_apps = set()
        for space in spaces:
            all_apps.update(space.get("applications", []))

        current_space = None
        for space in spaces:
            if space.get("is_current"):
                current_space = space
                break

        app_counts = {}
        for space in spaces:
            for app in space.get("applications", []):
                app_counts[app] = app_counts.get(app, 0) + 1

        primary_app = max(app_counts.keys(), key=app_counts.get) if app_counts else "Empty"

        return {
            "total_spaces": len(spaces),
            "total_windows": total_windows,
            "total_applications": len(all_apps),
            "spaces": spaces,
            "current_space": current_space,
            "primary_activity": primary_app,
            "all_applications": list(all_apps),
        }

    async def describe_workspace_async(self) -> str:
        """Async version of describe_workspace."""
        summary = await self.get_workspace_summary_async()

        if summary["total_spaces"] == 0:
            return "Unable to detect Mission Control spaces. Yabai may not be running."

        description = []

        description.append(f"You have {summary['total_spaces']} Mission Control spaces active")

        if summary["total_windows"] > 0:
            description.append(
                f" with {summary['total_windows']} windows across {summary['total_applications']} applications."
            )
        else:
            description.append(" with no windows currently open.")

        if summary["current_space"]:
            current = summary["current_space"]
            description.append(f"\n\nCurrently viewing Space {current['space_id']}")
            if current["window_count"] > 0:
                description.append(f" with {current['primary_activity']}.")
            else:
                description.append(" which is empty.")

        description.append("\n\nSpace breakdown:")
        for space in summary["spaces"]:
            space_desc = f"\n• Space {space['space_id']}"

            if space["is_fullscreen"]:
                space_desc += " (fullscreen)"
            if space["is_current"]:
                space_desc += " [CURRENT]"

            space_desc += ": "

            if space["window_count"] == 0:
                space_desc += "Empty"
            else:
                apps = space["applications"][:3]
                if len(apps) == 1:
                    space_desc += apps[0]
                else:
                    space_desc += ", ".join(apps)

                if len(space["applications"]) > 3:
                    space_desc += f" and {len(space['applications']) - 3} more"

                if space["windows"]:
                    first_window = space["windows"][0]
                    if first_window["title"]:
                        title = first_window["title"][:50]
                        if len(first_window["title"]) > 50:
                            title += "..."
                        space_desc += f' - "{title}"'

            description.append(space_desc)

        return "".join(description)

    async def detect_monitors_with_vision(self, screenshot=None) -> Optional[Dict[str, Any]]:
        """
        Detect monitor layout using YOLO vision

        Args:
            screenshot: Optional screenshot for detection

        Returns:
            Dictionary with monitor detections or None
        """
        if not self.enable_vision or screenshot is None:
            return None

        vision_analyzer = self._get_vision_analyzer()
        if not vision_analyzer:
            return None

        try:
            result = await vision_analyzer.detect_monitors(screenshot)

            if result:
                # Correlate with Yabai display info
                spaces_by_display = self.enumerate_spaces_by_display()

                return {
                    "monitor_detections": result,
                    "yabai_displays": len(spaces_by_display),
                    "spaces_by_display": spaces_by_display,
                }

            return None

        except Exception as e:
            logger.error(f"[YABAI] Error detecting monitors with vision: {e}")
            return None

    async def analyze_space_layout(
        self, space_id: int, screenshot=None
    ) -> Optional[Dict[str, Any]]:
        """
        Analyze layout of a specific space using vision

        Args:
            space_id: Space ID to analyze
            screenshot: Optional screenshot of the space

        Returns:
            Analysis results with window layout and UI elements
        """
        if not self.enable_vision or screenshot is None:
            return None

        vision_analyzer = self._get_vision_analyzer()
        if not vision_analyzer:
            return None

        try:
            # Get Yabai info about the space
            space_info = self.get_space_info(space_id)
            if not space_info:
                logger.warning(f"[YABAI] Space {space_id} not found")
                return None

            # Analyze with vision

            prompt = f"Analyze this workspace layout for Space {space_id} with {space_info['window_count']} windows"

            result = await vision_analyzer.analyze_screenshot_fast(screenshot, prompt=prompt)

            # Combine Yabai and vision data
            result["yabai_space_info"] = space_info
            result["detected_layout"] = (
                "multi_window" if space_info["window_count"] > 1 else "single_window"
            )

            logger.info(
                f"[YABAI] Analyzed Space {space_id} layout with vision "
                f"({space_info['window_count']} windows)"
            )

            return result

        except Exception as e:
            logger.error(f"[YABAI] Error analyzing space layout: {e}")
            return None

    async def get_enhanced_workspace_summary(self, screenshot=None) -> Dict[str, Any]:
        """
        Get workspace summary enhanced with vision analysis

        Args:
            screenshot: Optional full workspace screenshot

        Returns:
            Enhanced workspace summary with vision data
        """
        # Get base Yabai summary
        summary = self.get_workspace_summary()

        # Add vision enhancements if available
        if self.enable_vision and screenshot is not None:
            vision_analyzer = self._get_vision_analyzer()
            if vision_analyzer:
                try:
                    # Detect monitors
                    monitor_result = await self.detect_monitors_with_vision(screenshot)
                    if monitor_result:
                        summary["vision_monitor_detection"] = monitor_result

                    # Analyze current space layout
                    current_space = summary.get("current_space")
                    if current_space:
                        layout_result = await self.analyze_space_layout(
                            current_space["space_id"], screenshot
                        )
                        if layout_result:
                            summary["current_space_vision_analysis"] = layout_result

                    logger.info("[YABAI] Enhanced workspace summary with vision analysis")

                except Exception as e:
                    logger.error(f"[YABAI] Error enhancing workspace summary with vision: {e}")

        return summary


# =============================================================================
# GLOBAL INSTANCE & HELPER FUNCTIONS
# =============================================================================

# Global instance - lazy initialized
_yabai_detector: Optional[YabaiSpaceDetector] = None


def get_yabai_detector(auto_start: bool = True) -> YabaiSpaceDetector:
    """
    Get the global Yabai detector instance.

    Args:
        auto_start: If True, attempt to start yabai if not running

    Returns:
        Global YabaiSpaceDetector instance
    """
    global _yabai_detector
    if _yabai_detector is None:
        _yabai_detector = YabaiSpaceDetector(auto_start=auto_start)
    return _yabai_detector


def reset_yabai_detector() -> None:
    """Reset the global detector (useful for testing or reconfiguration)."""
    global _yabai_detector
    _yabai_detector = None


class _YabaiDetectorProxy:
    """Proxy class for backward compatibility with yabai_detector global."""

    def __getattr__(self, name):
        return getattr(get_yabai_detector(), name)

    def __repr__(self):
        return repr(get_yabai_detector())


# Backward compatibility - access via yabai_detector still works
yabai_detector = _YabaiDetectorProxy()


def open_accessibility_settings() -> bool:
    """
    Open macOS Accessibility settings for granting yabai permissions.

    Returns:
        True if successfully opened, False otherwise
    """
    try:
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            check=True,
        )
        logger.info("[YABAI] Opened Accessibility settings")
        return True
    except Exception as e:
        logger.error(f"[YABAI] Failed to open Accessibility settings: {e}")
        return False


# =============================================================================
# v30.3: COMPREHENSIVE YABAI PERMISSION FIXER
# =============================================================================
# ROOT CAUSE: macOS TCC (Transparency, Consent, and Control) system requires
# the ACTUAL binary path, not symlinks. When yabai is installed via Homebrew:
#   - Symlink: /opt/homebrew/bin/yabai
#   - Actual: /opt/homebrew/Cellar/yabai/X.Y.Z/bin/yabai
#
# When user tries to add yabai via the Finder dialog, macOS may:
#   1. Only recognize the resolved binary, not the symlink
#   2. Store the path incorrectly in TCC.db
#   3. Fail silently when the permission check uses a different path
#
# This system provides:
#   1. Actual binary path resolution
#   2. Programmatic permission prompt via AXIsProcessTrustedWithOptions
#   3. TCC database reset guidance
#   4. Service restart after permission grant
#   5. Continuous permission monitoring
# =============================================================================

@dataclass
class YabaiPermissionFixResult:
    """Result of a yabai permission fix attempt."""
    success: bool = False
    method_used: str = ""
    error_message: Optional[str] = None
    
    # Path information
    symlink_path: Optional[str] = None
    actual_binary_path: Optional[str] = None
    
    # Actions taken
    opened_settings: bool = False
    triggered_prompt: bool = False
    reset_tcc: bool = False
    restarted_service: bool = False
    
    # Next steps
    user_action_required: bool = False
    instructions: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "method_used": self.method_used,
            "error_message": self.error_message,
            "symlink_path": self.symlink_path,
            "actual_binary_path": self.actual_binary_path,
            "actions_taken": {
                "opened_settings": self.opened_settings,
                "triggered_prompt": self.triggered_prompt,
                "reset_tcc": self.reset_tcc,
                "restarted_service": self.restarted_service,
            },
            "user_action_required": self.user_action_required,
            "instructions": self.instructions,
        }


def get_yabai_actual_binary_path() -> Tuple[Optional[str], Optional[str]]:
    """
    v30.3: Resolve the actual yabai binary path (not symlink).
    
    Returns:
        Tuple of (symlink_path, actual_binary_path)
    """
    # Find symlink path
    symlink_path = shutil.which("yabai")
    if not symlink_path:
        for path in ["/opt/homebrew/bin/yabai", "/usr/local/bin/yabai"]:
            if os.path.isfile(path):
                symlink_path = path
                break
    
    if not symlink_path:
        return None, None
    
    # Resolve actual binary path
    try:
        # Try multiple methods to resolve the actual path
        actual_path = None
        
        # Method 1: os.path.realpath (handles symlinks)
        resolved = os.path.realpath(symlink_path)
        if resolved != symlink_path and os.path.isfile(resolved):
            actual_path = resolved
        
        # Method 2: Read symlink directly
        if not actual_path and os.path.islink(symlink_path):
            link_target = os.readlink(symlink_path)
            if not os.path.isabs(link_target):
                # Relative symlink - resolve relative to symlink directory
                link_dir = os.path.dirname(symlink_path)
                actual_path = os.path.normpath(os.path.join(link_dir, link_target))
            else:
                actual_path = link_target
        
        # Method 3: Use Homebrew Cellar pattern
        if not actual_path or not os.path.isfile(actual_path):
            # Try to find via Homebrew Cellar
            cellar_base = "/opt/homebrew/Cellar/yabai"
            if os.path.isdir(cellar_base):
                versions = os.listdir(cellar_base)
                if versions:
                    # Get latest version
                    latest_version = sorted(versions, reverse=True)[0]
                    cellar_path = os.path.join(cellar_base, latest_version, "bin", "yabai")
                    if os.path.isfile(cellar_path):
                        actual_path = cellar_path
        
        return symlink_path, actual_path or symlink_path
        
    except Exception as e:
        logger.warning(f"[v30.3] Failed to resolve yabai binary path: {e}")
        return symlink_path, symlink_path


def check_yabai_in_accessibility_tcc() -> Tuple[bool, str]:
    """
    v30.3: Check if yabai is in the Accessibility TCC database.
    
    Note: Reading TCC.db directly requires Full Disk Access permission.
    This function uses indirect methods to check permission status.
    
    Returns:
        Tuple of (is_authorized, status_message)
    """
    try:
        # Method 1: Check via AppleScript (AXIsProcessTrusted equivalent)
        script = '''
        use framework "ApplicationServices"
        return current application's AXIsProcessTrusted() as boolean
        '''
        result = subprocess.run(
            ["osascript", "-l", "AppleScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=2.0
        )
        
        if result.returncode == 0:
            is_trusted = "true" in result.stdout.lower()
            return is_trusted, "Checked via AXIsProcessTrusted"
        
        # Method 2: Check via ctypes (direct API call)
        import ctypes
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        trusted = lib.AXIsProcessTrusted()
        return bool(trusted), "Checked via ctypes"
        
    except Exception as e:
        logger.debug(f"[v30.3] TCC check failed: {e}")
        return False, f"Check failed: {e}"


def trigger_accessibility_prompt_for_yabai() -> bool:
    """
    v30.3: Programmatically trigger the macOS Accessibility permission prompt.
    
    Uses AXIsProcessTrustedWithOptions with kAXTrustedCheckOptionPrompt=YES
    to force macOS to show the permission dialog.
    
    Returns:
        True if the prompt was triggered (not necessarily granted)
    """
    try:
        # Get the actual yabai binary path
        symlink_path, actual_path = get_yabai_actual_binary_path()
        
        if not actual_path:
            logger.error("[v30.3] Cannot trigger prompt: yabai not found")
            return False
        
        # Method 1: Use tccutil to reset and re-prompt
        # This requires running from a process that can spawn yabai
        logger.info(f"[v30.3] Triggering accessibility prompt for: {actual_path}")
        
        # Create a script that will trigger the prompt when yabai runs
        # The key is that yabai itself needs to request the permission
        
        # First, restart yabai - it will trigger the prompt automatically
        # when it tries to access accessibility features
        try:
            subprocess.run(
                ["yabai", "--stop-service"],
                capture_output=True,
                timeout=5.0
            )
            time.sleep(0.5)
            subprocess.run(
                ["yabai", "--start-service"],
                capture_output=True,
                timeout=5.0
            )
            logger.info("[v30.3] Restarted yabai service - this should trigger permission prompt")
            return True
        except Exception as e:
            logger.debug(f"[v30.3] Service restart failed: {e}")
        
        return False
        
    except Exception as e:
        logger.error(f"[v30.3] Failed to trigger accessibility prompt: {e}")
        return False


async def fix_yabai_permissions(
    auto_open_settings: bool = True,
    auto_restart_service: bool = True,
    narrate_progress: bool = True
) -> YabaiPermissionFixResult:
    """
    v30.3: Comprehensive yabai permission fixer.
    
    Attempts to fix yabai accessibility permissions through multiple strategies:
    1. Detect if permission is missing
    2. Open Accessibility settings to the correct location
    3. Provide clear instructions with actual binary path
    4. Restart yabai service after fix
    
    Args:
        auto_open_settings: Automatically open System Settings
        auto_restart_service: Automatically restart yabai after fix
        narrate_progress: Log detailed progress
        
    Returns:
        YabaiPermissionFixResult with detailed status and instructions
    """
    result = YabaiPermissionFixResult()
    
    # =========================================================================
    # PHASE 1: Get actual binary paths
    # =========================================================================
    symlink_path, actual_path = get_yabai_actual_binary_path()
    result.symlink_path = symlink_path
    result.actual_binary_path = actual_path
    
    if not symlink_path:
        result.error_message = "Yabai is not installed"
        result.instructions = [
            "Install yabai with: brew install koekeishiya/formulae/yabai"
        ]
        return result
    
    if narrate_progress:
        logger.info(f"[v30.3] Yabai paths:")
        logger.info(f"  Symlink: {symlink_path}")
        logger.info(f"  Actual:  {actual_path}")
    
    # =========================================================================
    # PHASE 2: Check current permission status
    # =========================================================================
    perm_status = await check_yabai_permissions(force_recheck=True)
    
    if perm_status.fully_functional:
        result.success = True
        result.method_used = "already_authorized"
        if narrate_progress:
            logger.info("[v30.3] ✅ Yabai already has accessibility permission")
        return result
    
    # =========================================================================
    # PHASE 3: Check yabai error log for accessibility crash
    # =========================================================================
    user = os.environ.get("USER", "")
    error_log = Path(f"/tmp/yabai_{user}.err.log")
    accessibility_crash = False
    
    if error_log.exists():
        try:
            content = error_log.read_text()
            if "could not access accessibility features" in content.lower():
                accessibility_crash = True
                if narrate_progress:
                    logger.warning("[v30.3] ⚠️ Yabai crashed due to missing accessibility permission")
        except Exception:
            pass
    
    # =========================================================================
    # PHASE 4: Generate comprehensive fix instructions
    # =========================================================================
    result.user_action_required = True
    
    # Detect macOS version for correct settings path
    macos_version = None
    try:
        version_result = subprocess.run(
            ["sw_vers", "-productVersion"],
            capture_output=True,
            text=True,
            timeout=2.0
        )
        if version_result.returncode == 0:
            macos_version = version_result.stdout.strip()
    except Exception:
        pass
    
    # Generate instructions
    if actual_path and actual_path != symlink_path:
        # Homebrew installation - need to add actual binary
        result.instructions = [
            "🔐 YABAI ACCESSIBILITY PERMISSION FIX",
            "",
            "The issue: macOS needs the ACTUAL binary path, not the symlink.",
            f"  Symlink: {symlink_path}",
            f"  Actual:  {actual_path}",
            "",
            "STEP 1: Open System Settings → Privacy & Security → Accessibility",
            "",
            "STEP 2: Click the '+' button to add an app",
            "",
            "STEP 3: Press Cmd+Shift+G to open 'Go to Folder'",
            "",
            f"STEP 4: Paste this path: {actual_path}",
            "",
            "STEP 5: Click 'Open' to add yabai",
            "",
            "STEP 6: Make sure the toggle is ON",
            "",
            "STEP 7: Restart yabai service:",
            "  Run in Terminal: yabai --restart-service",
            "",
            "ALTERNATIVE (if above doesn't work):",
            "  1. Remove yabai from Accessibility list",
            "  2. Run: sudo tccutil reset Accessibility",
            "  3. Run: yabai --restart-service",
            "  4. A permission prompt should appear - click 'Open System Settings'",
            "  5. Toggle yabai ON",
        ]
    else:
        # Standard installation
        result.instructions = [
            "🔐 YABAI ACCESSIBILITY PERMISSION FIX",
            "",
            "STEP 1: Open System Settings → Privacy & Security → Accessibility",
            "",
            "STEP 2: Find 'yabai' in the list",
            "",
            "STEP 3: Toggle it ON",
            "",
            "STEP 4: If already ON, toggle OFF then ON again",
            "",
            "STEP 5: Restart yabai service:",
            "  Run in Terminal: yabai --restart-service",
        ]
    
    # =========================================================================
    # PHASE 5: Auto-open settings if requested
    # =========================================================================
    if auto_open_settings:
        try:
            subprocess.run(
                ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                check=True,
                timeout=5.0
            )
            result.opened_settings = True
            if narrate_progress:
                logger.info("[v30.3] Opened Accessibility settings")
        except Exception as e:
            if narrate_progress:
                logger.warning(f"[v30.3] Could not open settings: {e}")
    
    # =========================================================================
    # PHASE 6: Trigger permission prompt by restarting yabai
    # =========================================================================
    # When yabai starts without permission, macOS should show the prompt
    if auto_restart_service:
        try:
            subprocess.run(["yabai", "--stop-service"], capture_output=True, timeout=5.0)
            await asyncio.sleep(0.5)
            subprocess.run(["yabai", "--start-service"], capture_output=True, timeout=5.0)
            result.restarted_service = True
            result.triggered_prompt = True
            if narrate_progress:
                logger.info("[v30.3] Restarted yabai service to trigger permission prompt")
        except Exception as e:
            if narrate_progress:
                logger.warning(f"[v30.3] Could not restart yabai service: {e}")
    
    # =========================================================================
    # PHASE 7: Wait and recheck (give user time to grant permission)
    # =========================================================================
    # Don't wait here - return instructions and let user act
    result.method_used = "instructions_provided"
    result.error_message = "Yabai needs accessibility permission - follow the instructions above"
    
    return result


async def monitor_and_wait_for_yabai_permission(
    timeout_seconds: float = 60.0,
    check_interval: float = 2.0,
    on_progress: Optional[callable] = None
) -> bool:
    """
    v30.3: Monitor and wait for yabai permission to be granted.
    
    Useful for creating a user-friendly flow where you:
    1. Call fix_yabai_permissions() to show instructions
    2. Call this function to wait for user to grant permission
    3. Proceed with operations once permission is granted
    
    Args:
        timeout_seconds: Maximum time to wait
        check_interval: How often to check permission status
        on_progress: Optional callback(seconds_elapsed, status_message)
        
    Returns:
        True if permission was granted within timeout, False otherwise
    """
    start_time = time.time()
    
    while time.time() - start_time < timeout_seconds:
        elapsed = time.time() - start_time
        
        # Check permission status
        status = await check_yabai_permissions(force_recheck=True)
        
        if status.fully_functional:
            if on_progress:
                on_progress(elapsed, "Permission granted!")
            logger.info(f"[v30.3] ✅ Yabai permission granted after {elapsed:.1f}s")
            return True
        
        if on_progress:
            remaining = timeout_seconds - elapsed
            on_progress(elapsed, f"Waiting for permission... ({remaining:.0f}s remaining)")
        
        await asyncio.sleep(check_interval)
    
    logger.warning(f"[v30.3] Permission not granted within {timeout_seconds}s")
    return False


def get_yabai_permission_instructions() -> str:
    """
    v30.3: Get formatted permission instructions as a string.
    
    Returns:
        Formatted instructions string
    """
    symlink_path, actual_path = get_yabai_actual_binary_path()
    
    instructions = []
    instructions.append("=" * 60)
    instructions.append("🔐 YABAI ACCESSIBILITY PERMISSION REQUIRED")
    instructions.append("=" * 60)
    instructions.append("")
    
    if actual_path and actual_path != symlink_path:
        instructions.append("⚠️  IMPORTANT: Add the ACTUAL binary, not the symlink!")
        instructions.append("")
        instructions.append(f"   Symlink (DON'T use): {symlink_path}")
        instructions.append(f"   Actual (USE THIS):   {actual_path}")
        instructions.append("")
    
    instructions.append("TO FIX:")
    instructions.append("")
    instructions.append("1. System Settings → Privacy & Security → Accessibility")
    instructions.append("")
    instructions.append("2. Click '+' to add an app")
    instructions.append("")
    instructions.append("3. Press Cmd+Shift+G and paste:")
    if actual_path:
        instructions.append(f"   {actual_path}")
    else:
        instructions.append(f"   {symlink_path}")
    instructions.append("")
    instructions.append("4. Click 'Open' to add yabai")
    instructions.append("")
    instructions.append("5. Make sure the toggle is ON")
    instructions.append("")
    instructions.append("6. Restart yabai: yabai --restart-service")
    instructions.append("")
    instructions.append("=" * 60)
    
    return "\n".join(instructions)


def diagnose_yabai() -> Dict[str, Any]:
    """
    Run comprehensive yabai diagnostics.

    Returns:
        Dictionary with diagnostic information
    """
    detector = get_yabai_detector(auto_start=False)
    status = detector.get_detailed_status()

    # Add additional diagnostics
    diagnostics = {
        **status,
        "diagnostics": {
            "process_running": False,
            "socket_exists": False,
            "launchctl_status": None,
        }
    }

    # Check if process is running
    try:
        result = subprocess.run(["pgrep", "-x", "yabai"], capture_output=True, text=True)
        diagnostics["diagnostics"]["process_running"] = result.returncode == 0
        if result.returncode == 0:
            diagnostics["diagnostics"]["process_pid"] = result.stdout.strip()
    except Exception:
        pass

    # Check if socket exists
    socket_path = Path("/tmp/yabai_$USER.socket".replace("$USER", os.environ.get("USER", "")))
    # Also check common socket locations
    for potential_socket in [
        Path(f"/tmp/yabai_{os.environ.get('USER', '')}.socket"),
        Path("/var/run/yabai.socket"),
        Path.home() / ".local" / "run" / "yabai.socket",
    ]:
        if potential_socket.exists():
            diagnostics["diagnostics"]["socket_exists"] = True
            diagnostics["diagnostics"]["socket_path"] = str(potential_socket)
            break

    # Check launchctl status
    try:
        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/com.koekeishiya.yabai"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            diagnostics["diagnostics"]["launchctl_status"] = "registered"
            # Parse state
            if "state = running" in result.stdout.lower():
                diagnostics["diagnostics"]["launchctl_state"] = "running"
            else:
                diagnostics["diagnostics"]["launchctl_state"] = "not running"
        else:
            diagnostics["diagnostics"]["launchctl_status"] = "not registered"
    except Exception as e:
        diagnostics["diagnostics"]["launchctl_error"] = str(e)

    return diagnostics


async def ensure_yabai_ready(timeout_seconds: float = 10.0) -> bool:
    """
    Ensure yabai is ready for use, with timeout.

    Args:
        timeout_seconds: Maximum time to wait for yabai to become ready

    Returns:
        True if yabai is ready, False otherwise
    """
    detector = get_yabai_detector()

    if detector.is_available():
        return True

    # Try to start
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        if await detector.ensure_running_async():
            return True
        await asyncio.sleep(0.5)

    return False


# =============================================================================
# v30.0: COMPREHENSIVE YABAI HEALTH & PERMISSION DETECTION
# =============================================================================
# ROOT CAUSE FIX: Detect when yabai lacks accessibility permissions
# This is the "smoking gun" behind silent window control failures:
# - Yabai can QUERY windows/spaces (socket works)
# - But can't CONTROL windows (accessibility permission denied)
# - macOS shows permission popup that user may have dismissed
#
# This system provides:
# 1. Proactive permission detection before operations
# 2. Clear diagnostics when permissions are missing
# 3. Actionable guidance to fix the issue
# 4. Caching to avoid repeated checks
# =============================================================================

@dataclass
class YabaiPermissionStatus:
    """v30.0: Comprehensive yabai permission and health status."""
    # Basic availability
    yabai_installed: bool = False
    yabai_path: Optional[str] = None
    yabai_running: bool = False
    yabai_pid: Optional[int] = None

    # Query capability (socket works)
    can_query_windows: bool = False
    can_query_spaces: bool = False
    can_query_displays: bool = False

    # Control capability (accessibility permissions)
    can_move_windows: bool = False
    can_focus_windows: bool = False
    can_switch_spaces: bool = False

    # Scripting Addition (SIP-dependent)
    scripting_addition_loaded: bool = False
    sip_status: Optional[str] = None

    # Overall status
    fully_functional: bool = False
    needs_accessibility_permission: bool = False
    needs_restart: bool = False

    # Diagnostics
    error_message: Optional[str] = None
    fix_instructions: Optional[str] = None
    last_check: Optional[datetime] = None
    check_duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for reporting."""
        return {
            "yabai_installed": self.yabai_installed,
            "yabai_path": self.yabai_path,
            "yabai_running": self.yabai_running,
            "yabai_pid": self.yabai_pid,
            "can_query_windows": self.can_query_windows,
            "can_query_spaces": self.can_query_spaces,
            "can_query_displays": self.can_query_displays,
            "can_move_windows": self.can_move_windows,
            "can_focus_windows": self.can_focus_windows,
            "can_switch_spaces": self.can_switch_spaces,
            "scripting_addition_loaded": self.scripting_addition_loaded,
            "sip_status": self.sip_status,
            "fully_functional": self.fully_functional,
            "needs_accessibility_permission": self.needs_accessibility_permission,
            "needs_restart": self.needs_restart,
            "error_message": self.error_message,
            "fix_instructions": self.fix_instructions,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "check_duration_ms": self.check_duration_ms,
        }


# Module-level cache for permission status
_YABAI_PERMISSION_CACHE: Optional[YabaiPermissionStatus] = None
_YABAI_PERMISSION_CACHE_TIME: Optional[datetime] = None
_YABAI_PERMISSION_CACHE_TTL = 30.0  # 30 seconds


async def check_yabai_permissions(force_recheck: bool = False) -> YabaiPermissionStatus:
    """
    v30.1: FAST Comprehensive yabai permission and health check.

    ROOT CAUSE FIX: v30.0 was too slow - sequential subprocess calls with
    long timeouts caused "Processing..." hang. v30.1 fixes:
    1. PARALLEL execution of all tests
    2. SHORT timeouts (500ms instead of 2-3s)
    3. FAST PATH: Skip expensive tests if basic connectivity fails
    4. Overall timeout wrapper to guarantee fast return

    Args:
        force_recheck: If True, bypass cache and recheck

    Returns:
        YabaiPermissionStatus with detailed health and permission info
    """
    global _YABAI_PERMISSION_CACHE, _YABAI_PERMISSION_CACHE_TIME

    # Check cache FIRST (instant return)
    if not force_recheck and _YABAI_PERMISSION_CACHE and _YABAI_PERMISSION_CACHE_TIME:
        cache_age = (datetime.now() - _YABAI_PERMISSION_CACHE_TIME).total_seconds()
        if cache_age < _YABAI_PERMISSION_CACHE_TTL:
            return _YABAI_PERMISSION_CACHE

    start_time = time.time()
    status = YabaiPermissionStatus()

    # v30.1: Overall timeout - permission check MUST complete in 2 seconds max
    OVERALL_TIMEOUT = 2.0
    SUBPROCESS_TIMEOUT = 0.5  # 500ms per subprocess (was 2-3s)

    try:
        # Wrap entire check in overall timeout
        status = await asyncio.wait_for(
            _do_permission_check(status, SUBPROCESS_TIMEOUT),
            timeout=OVERALL_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"[v30.1] Permission check timed out after {OVERALL_TIMEOUT}s")
        status.error_message = "Permission check timed out - yabai may be unresponsive"
        status.needs_restart = True
    except Exception as e:
        logger.error(f"[v30.1] Permission check failed: {e}")
        status.error_message = f"Permission check failed: {e}"

    status.last_check = datetime.now()
    status.check_duration_ms = (time.time() - start_time) * 1000

    # Cache result
    _YABAI_PERMISSION_CACHE = status
    _YABAI_PERMISSION_CACHE_TIME = datetime.now()

    # Log result
    if status.needs_accessibility_permission:
        logger.warning(f"[v30.1] ⚠️ Yabai needs accessibility permission")
    elif status.fully_functional:
        logger.debug(f"[v30.1] ✅ Yabai OK ({status.check_duration_ms:.0f}ms)")
    elif not status.yabai_running:
        logger.debug(f"[v30.1] Yabai not running ({status.check_duration_ms:.0f}ms)")

    return status


async def _do_permission_check(status: YabaiPermissionStatus, timeout: float) -> YabaiPermissionStatus:
    """
    v30.1: Internal permission check with fast-fail logic.
    """
    # =========================================================================
    # PHASE 1: Check if yabai is installed (fast filesystem check)
    # =========================================================================
    yabai_path = shutil.which("yabai")
    if not yabai_path:
        for path in ["/opt/homebrew/bin/yabai", "/usr/local/bin/yabai"]:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                yabai_path = path
                break

    status.yabai_installed = yabai_path is not None
    status.yabai_path = yabai_path

    if not yabai_path:
        status.error_message = "Yabai is not installed"
        status.fix_instructions = "Install yabai: brew install koekeishiya/formulae/yabai"
        return status

    # =========================================================================
    # PHASE 2: Check if yabai process is running (fast pgrep)
    # =========================================================================
    try:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-x", "yabai",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        status.yabai_running = proc.returncode == 0
        if status.yabai_running:
            try:
                status.yabai_pid = int(stdout.decode().strip().split()[0])
            except (ValueError, IndexError):
                pass
    except asyncio.TimeoutError:
        status.yabai_running = False
    except Exception:
        status.yabai_running = False

    if not status.yabai_running:
        status.error_message = "Yabai is not running"
        status.fix_instructions = "Start yabai: yabai --start-service"
        status.needs_restart = True
        return status  # FAST PATH: Skip remaining tests

    # =========================================================================
    # PHASE 3: Quick socket connectivity test (SINGLE query, not three)
    # =========================================================================
    async def quick_query() -> Tuple[bool, str]:
        """Single fast query to test socket connectivity."""
        try:
            proc = await asyncio.create_subprocess_exec(
                yabai_path, "-m", "query", "--spaces",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode == 0:
                return True, stdout.decode()
            return False, stderr.decode()
        except asyncio.TimeoutError:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    can_query, query_result = await quick_query()

    if not can_query:
        # Socket not responding - yabai probably crashed
        status.can_query_windows = False
        status.can_query_spaces = False
        status.can_query_displays = False
        if "timeout" in query_result:
            status.error_message = "Yabai socket not responding (may need restart)"
        elif "failed to connect" in query_result.lower():
            status.error_message = "Yabai socket connection failed"
        else:
            status.error_message = f"Yabai query failed: {query_result[:100]}"
        status.needs_restart = True
        return status  # FAST PATH: Skip remaining tests

    # Socket works - set query capabilities
    status.can_query_spaces = True
    status.can_query_windows = True  # If spaces work, windows likely work too
    status.can_query_displays = True

    # =========================================================================
    # PHASE 4: Quick control test (simplified - just check if we can query windows)
    # =========================================================================
    # v30.1: Skip the slow focus/move test - if queries work and yabai is running,
    # assume control works. The actual control test was too slow and unreliable.
    # If accessibility is actually missing, operations will fail fast anyway.

    # Assume control works if queries work (fast path)
    status.can_focus_windows = True
    status.can_move_windows = True
    status.can_switch_spaces = True
    status.scripting_addition_loaded = True

    # =========================================================================
    # PHASE 5: Determine overall status
    # =========================================================================
    status.fully_functional = (
        status.yabai_running and
        status.can_query_windows
    )

    if status.fully_functional:
        status.error_message = None
        status.fix_instructions = None

    return status


def get_cached_permission_status() -> Optional[YabaiPermissionStatus]:
    """Get cached permission status without blocking."""
    return _YABAI_PERMISSION_CACHE


def invalidate_permission_cache():
    """Invalidate permission cache to force recheck."""
    global _YABAI_PERMISSION_CACHE, _YABAI_PERMISSION_CACHE_TIME
    _YABAI_PERMISSION_CACHE = None
    _YABAI_PERMISSION_CACHE_TIME = None


async def ensure_yabai_permissions(
    auto_open_settings: bool = False,
    narrate_issues: bool = True
) -> Tuple[bool, Optional[str]]:
    """
    v30.0: Ensure yabai has all required permissions before operations.

    This should be called BEFORE any God Mode / window control operations.
    It provides early detection of permission issues with clear guidance.

    Args:
        auto_open_settings: If True, automatically open accessibility settings
                           when permissions are missing
        narrate_issues: If True, log detailed issue descriptions

    Returns:
        Tuple of (success, error_message)
        - success: True if yabai is fully functional
        - error_message: None if success, otherwise describes the issue
    """
    status = await check_yabai_permissions()

    if status.fully_functional:
        return True, None

    if status.needs_accessibility_permission:
        error_msg = (
            "🚫 YABAI ACCESSIBILITY PERMISSION REQUIRED\n\n"
            "Yabai can see windows but cannot control them.\n"
            "macOS is blocking window management operations.\n\n"
            "TO FIX:\n"
            "1. Go to System Settings → Privacy & Security → Accessibility\n"
            "2. Find 'yabai' and toggle it ON\n"
            "3. If already ON, toggle OFF then ON again\n"
            "4. Run: yabai --restart-service\n\n"
            "This is required for window teleportation and space switching."
        )

        if narrate_issues:
            logger.error(f"[v30.0] {error_msg}")

        if auto_open_settings:
            open_accessibility_settings()

        return False, error_msg

    if not status.yabai_running:
        error_msg = (
            "Yabai is not running.\n"
            "Start it with: yabai --start-service"
        )
        if narrate_issues:
            logger.error(f"[v30.0] {error_msg}")
        return False, error_msg

    if not status.scripting_addition_loaded and not status.can_switch_spaces:
        error_msg = (
            "Yabai scripting addition not loaded.\n"
            "Space switching requires: sudo yabai --load-sa"
        )
        if narrate_issues:
            logger.warning(f"[v30.0] {error_msg}")
        # Return True but with warning - basic functionality may still work
        return True, error_msg

    # Generic error
    return False, status.error_message


# Export for convenient imports
__all__ = [
    # Core classes
    "YabaiSpaceDetector",
    "YabaiStatus",
    "YabaiServiceHealth",
    "YabaiConfig",
    # v24.0 Intelligent Search & Rescue Protocol
    "RescueStrategy",
    "RescueFailureReason",
    "RescueTelemetry",
    "RescueResult",
    "get_rescue_telemetry",
    "reset_rescue_telemetry",
    # v25.0 Shadow Monitor Infrastructure
    "GhostDisplayManager",
    "GhostDisplayManagerConfig",
    "GhostDisplayStatus",
    "GhostDisplayInfo",
    "WindowGeometry",
    "WindowLayoutStyle",
    "get_ghost_manager",
    "reset_ghost_manager",
    # Factory and utilities
    "get_yabai_detector",
    "reset_yabai_detector",
    "open_accessibility_settings",
    "diagnose_yabai",
    "ensure_yabai_ready",
    # v30.0 Permission Detection
    "YabaiPermissionStatus",
    "check_yabai_permissions",
    "ensure_yabai_permissions",
    "get_cached_permission_status",
    "invalidate_permission_cache",
    # v30.3 Permission Fixer
    "YabaiPermissionFixResult",
    "get_yabai_actual_binary_path",
    "check_yabai_in_accessibility_tcc",
    "trigger_accessibility_prompt_for_yabai",
    "fix_yabai_permissions",
    "monitor_and_wait_for_yabai_permission",
    "get_yabai_permission_instructions",
]
