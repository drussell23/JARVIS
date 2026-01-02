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
from typing import Any, Callable, Dict, List, Optional, Tuple

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
        source_space: Optional[int] = None
    ) -> Tuple[bool, str]:
        """
        Move a window to a target space, using rescue protocol if needed.

        SEARCH & RESCUE PROTOCOL:
        1. Fast Path: Try direct move (works for visible windows)
        2. Rescue Path: If window is "dehydrated" on hidden space:
           a) Switch to source space to wake window
           b) Move the window while awake
           c) Return to original space immediately

        Args:
            window_id: The window ID to move
            target_space: The target space index (1-based)
            source_space: The window's current space (optional, for rescue)

        Returns:
            Tuple of (success, method):
            - success: True if window was moved
            - method: "direct" | "rescue" | "failed"

        Example:
            success, method = yabai.move_window_to_space_with_rescue(
                window_id=12345,
                target_space=10,  # Ghost Display
                source_space=5    # Window's current hidden space
            )
            if success:
                logger.info(f"Moved window via {method} path")
        """
        # =====================================================================
        # FAST PATH: Direct move (works for visible windows)
        # =====================================================================
        if self.move_window_to_space(window_id, target_space):
            return True, "direct"

        # =====================================================================
        # RESCUE PATH: Window is on hidden space, needs wake-up
        # =====================================================================
        if source_space is None:
            logger.warning(
                f"[YABAI] 🛟 Rescue needed for window {window_id} but source_space unknown"
            )
            return False, "failed"

        logger.info(
            f"[YABAI] 🛟 SEARCH & RESCUE: Window {window_id} is dehydrated on Space {source_space}"
        )

        # Remember where the user is
        current_space = self.get_current_user_space()
        if current_space is None:
            logger.error("[YABAI] Cannot rescue: unable to determine current space")
            return False, "failed"

        rescue_success = False
        try:
            # A) Switch to source space to wake the window
            logger.debug(f"[YABAI] 🛟 Step 1: Switching to Space {source_space} to wake window...")
            if not self._switch_to_space(source_space):
                logger.error(f"[YABAI] 🛟 Rescue failed: couldn't switch to Space {source_space}")
                return False, "failed"

            # Minimal wake time - just enough for Yabai to see the window
            # Using environment variable for tuning without code changes
            wake_delay = float(os.environ.get("JARVIS_RESCUE_WAKE_DELAY", "0.05"))
            time.sleep(wake_delay)

            # B) Move the window while it's awake
            logger.debug(f"[YABAI] 🛟 Step 2: Moving window {window_id} to Space {target_space}...")
            if self.move_window_to_space(window_id, target_space):
                rescue_success = True
                logger.info(
                    f"[YABAI] 🛟 Rescue SUCCESS: Window {window_id} moved to Space {target_space}"
                )
            else:
                logger.error(
                    f"[YABAI] 🛟 Rescue FAILED: Window {window_id} still not accessible"
                )

        except Exception as e:
            logger.error(f"[YABAI] 🛟 Rescue exception: {e}")

        finally:
            # C) ALWAYS return to original space (even on failure)
            logger.debug(f"[YABAI] 🛟 Step 3: Returning to Space {current_space}...")
            return_success = self._switch_to_space(current_space)
            if not return_success:
                logger.warning(
                    f"[YABAI] 🛟 Warning: Failed to return to Space {current_space}"
                )

        return rescue_success, "rescue" if rescue_success else "failed"

    async def move_window_to_space_with_rescue_async(
        self,
        window_id: int,
        target_space: int,
        source_space: Optional[int] = None
    ) -> Tuple[bool, str]:
        """
        Async version of move_window_to_space_with_rescue.

        Uses the full Search & Rescue protocol for hidden windows.
        See move_window_to_space_with_rescue for full documentation.
        """
        # =====================================================================
        # FAST PATH: Try direct async move first
        # =====================================================================
        if await self.move_window_to_space_async(window_id, target_space):
            return True, "direct"

        # =====================================================================
        # RESCUE PATH: Need to wake the window
        # =====================================================================
        if source_space is None:
            logger.warning(
                f"[YABAI] 🛟 Rescue needed for window {window_id} but source_space unknown"
            )
            return False, "failed"

        logger.info(
            f"[YABAI] 🛟 SEARCH & RESCUE (async): Window {window_id} dehydrated on Space {source_space}"
        )

        current_space = self.get_current_user_space()
        if current_space is None:
            return False, "failed"

        rescue_success = False
        try:
            # A) Switch to source space
            logger.debug(f"[YABAI] 🛟 Step 1: Switching to Space {source_space}...")
            if not await self._switch_to_space_async(source_space):
                return False, "failed"

            # Minimal wake delay
            wake_delay = float(os.environ.get("JARVIS_RESCUE_WAKE_DELAY", "0.05"))
            await asyncio.sleep(wake_delay)

            # B) Move window
            logger.debug(f"[YABAI] 🛟 Step 2: Moving window {window_id}...")
            if await self.move_window_to_space_async(window_id, target_space):
                rescue_success = True
                logger.info(
                    f"[YABAI] 🛟 Rescue SUCCESS: Window {window_id} → Space {target_space}"
                )

        except Exception as e:
            logger.error(f"[YABAI] 🛟 Rescue exception: {e}")

        finally:
            # C) ALWAYS return to original space
            logger.debug(f"[YABAI] 🛟 Step 3: Returning to Space {current_space}...")
            await self._switch_to_space_async(current_space)

        return rescue_success, "rescue" if rescue_success else "failed"

    async def rescue_windows_to_ghost_async(
        self,
        windows: List[Dict[str, Any]],
        ghost_space: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        High-level batch rescue: Move multiple windows to Ghost Display.

        This is the main entry point for Auto-Handoff, handling:
        - Direct moves for visible windows
        - Search & Rescue for hidden windows
        - Batch optimization (group by source space)
        - Progress tracking and narration

        Args:
            windows: List of window dicts with 'window_id' and 'space_id'
            ghost_space: Target Ghost Display space (auto-detected if None)

        Returns:
            Dict with:
                - success: bool (any windows moved)
                - direct_count: int (windows moved directly)
                - rescue_count: int (windows rescued from hidden)
                - failed_count: int (windows that couldn't be moved)
                - details: list of per-window results
        """
        result = {
            "success": False,
            "direct_count": 0,
            "rescue_count": 0,
            "failed_count": 0,
            "details": []
        }

        if not windows:
            return result

        # Auto-detect Ghost Display if not specified
        if ghost_space is None:
            ghost_space = self.get_ghost_display_space()
            if ghost_space is None:
                logger.warning("[YABAI] 🛟 No Ghost Display available for rescue")
                return result

        current_space = self.get_current_user_space()
        visible_spaces = {current_space, ghost_space}

        # Attempt to get all visible spaces for smarter routing
        try:
            from backend.vision.multi_space_window_detector import MultiSpaceWindowDetector
            detector = MultiSpaceWindowDetector()
            all_visible = await detector.get_all_visible_spaces()
            visible_spaces.update(all_visible)
        except Exception:
            pass

        # =====================================================================
        # BATCH OPTIMIZATION: Group windows by source space
        # =====================================================================
        # Instead of switching back and forth, we visit each hidden space once
        # and collect all its windows before returning.
        # =====================================================================
        from collections import defaultdict
        windows_by_space = defaultdict(list)
        for w in windows:
            source = w.get("space_id")
            windows_by_space[source].append(w)

        # Process visible spaces first (fast path)
        for space_id in list(windows_by_space.keys()):
            if space_id in visible_spaces and space_id != ghost_space:
                for w in windows_by_space[space_id]:
                    window_id = w.get("window_id")
                    success = await self.move_window_to_space_async(window_id, ghost_space)
                    detail = {
                        "window_id": window_id,
                        "source_space": space_id,
                        "success": success,
                        "method": "direct" if success else "failed"
                    }
                    result["details"].append(detail)
                    if success:
                        result["direct_count"] += 1
                    else:
                        result["failed_count"] += 1
                del windows_by_space[space_id]

        # Process hidden spaces (rescue path)
        for space_id, space_windows in windows_by_space.items():
            if space_id == ghost_space:
                # Already on ghost, skip
                for w in space_windows:
                    result["details"].append({
                        "window_id": w.get("window_id"),
                        "source_space": space_id,
                        "success": True,
                        "method": "already_on_ghost"
                    })
                continue

            # Batch rescue from this hidden space
            logger.info(
                f"[YABAI] 🛟 Batch rescue: {len(space_windows)} windows from Space {space_id}"
            )

            # Switch to hidden space once
            if await self._switch_to_space_async(space_id):
                wake_delay = float(os.environ.get("JARVIS_RESCUE_WAKE_DELAY", "0.05"))
                await asyncio.sleep(wake_delay)

                # Move all windows from this space
                for w in space_windows:
                    window_id = w.get("window_id")
                    success = await self.move_window_to_space_async(window_id, ghost_space)
                    detail = {
                        "window_id": window_id,
                        "source_space": space_id,
                        "success": success,
                        "method": "rescue" if success else "failed"
                    }
                    result["details"].append(detail)
                    if success:
                        result["rescue_count"] += 1
                    else:
                        result["failed_count"] += 1
            else:
                # Couldn't switch to this space
                for w in space_windows:
                    result["details"].append({
                        "window_id": w.get("window_id"),
                        "source_space": space_id,
                        "success": False,
                        "method": "failed"
                    })
                    result["failed_count"] += 1

        # Return to original space
        if current_space:
            await self._switch_to_space_async(current_space)

        result["success"] = (result["direct_count"] + result["rescue_count"]) > 0

        if result["success"]:
            logger.info(
                f"[YABAI] 🛟 Batch rescue complete: "
                f"{result['direct_count']} direct, {result['rescue_count']} rescued, "
                f"{result['failed_count']} failed"
            )

        return result

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


# Export for convenient imports
__all__ = [
    "YabaiSpaceDetector",
    "YabaiStatus",
    "YabaiServiceHealth",
    "YabaiConfig",
    "get_yabai_detector",
    "reset_yabai_detector",
    "open_accessibility_settings",
    "diagnose_yabai",
    "ensure_yabai_ready",
]
