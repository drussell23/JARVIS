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

        # Discover yabai
        self._discover_yabai()

        # Attempt auto-start if enabled
        if auto_start and self.config.auto_start:
            self._attempt_startup()

        self._initialized = True

    def _discover_yabai(self) -> None:
        """Discover yabai installation and version."""
        # Find yabai binary
        yabai_path = shutil.which("yabai")
        if not yabai_path:
            # Check common locations
            common_paths = [
                "/opt/homebrew/bin/yabai",
                "/usr/local/bin/yabai",
                str(Path.home() / ".local" / "bin" / "yabai"),
            ]
            for path in common_paths:
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    yabai_path = path
                    break

        self._health.yabai_path = yabai_path

        if yabai_path:
            try:
                result = subprocess.run(
                    [yabai_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    self._health.yabai_version = result.stdout.strip()
                    logger.info(f"[YABAI] Found yabai: {yabai_path} ({self._health.yabai_version})")
            except Exception as e:
                logger.warning(f"[YABAI] Could not get yabai version: {e}")
        else:
            logger.warning("[YABAI] Yabai not found - install with: brew install koekeishiya/formulae/yabai")

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
        """Property for backward compatibility."""
        return self._check_yabai_available()

    def _check_yabai_available(self) -> bool:
        """Check if Yabai is installed and running"""
        self._health.last_check = datetime.now()

        if not self._health.yabai_path:
            return False

        is_running = self._check_service_running()
        self._health.is_running = is_running

        if is_running:
            self._health.permissions_granted = True

        return is_running

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
        """Check if Yabai is installed and running"""
        try:
            # Check if yabai command exists
            result = subprocess.run(["which", "yabai"], capture_output=True, text=True)
            if result.returncode != 0:
                return False

            # Try to query yabai (will fail if not running)
            result = subprocess.run(
                ["yabai", "-m", "query", "--spaces"],
                capture_output=True,
                text=True,
                timeout=2,
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
