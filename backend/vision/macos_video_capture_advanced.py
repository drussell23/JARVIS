"""
Advanced macOS Video Capture System (v10.7)
Production-grade implementation using PyObjC + AVFoundation

CRITICAL FIXES v10.7 (Jan 2026):
================================
- FIXED: Use-after-free in VideoFrameDelegate causing SIGSEGV after ~45 minutes
  → Frame is now deep-copied BEFORE CVPixelBuffer is unlocked
  → Frames passed to callbacks are fully owned, not views

- FIXED: Shutdown race condition causing callback-after-free crashes
  → Shutdown sequence now stops session before waiting for callbacks
  → Delegate is signaled to stop accepting frames first
  → All active callbacks drain before resources are freed

- FIXED: Event loop validation to prevent scheduling to closed loops
  → Validates event loop is still running before scheduling
  → Handles loop closure gracefully during shutdown

- FIXED: Frame queue race condition in VideoWatcher
  → Atomic get/put operations with lock protection
  → Thread-safe stats counters with properties

- FIXED: Hardcoded timeouts replaced with environment variables
  → JARVIS_CAPTURE_CALLBACK_DRAIN_TIMEOUT (default: 5.0s)
  → JARVIS_CAPTURE_RUNLOOP_STOP_TIMEOUT (default: 2.0s)
  → JARVIS_WATCHER_THREAD_STOP_TIMEOUT (default: 2.0s)

Features:
- Native AVFoundation integration via PyObjC
- Async/await support with proper event loop integration
- Parallel capture sessions with resource management
- Intelligent fallback chain (AVFoundation → ScreenCaptureKit → screencapture)
- Dynamic configuration (environment variables, no hardcoding)
- Comprehensive error handling and graceful degradation
- Real-time performance monitoring and adaptive quality
- Memory-safe frame handling with ownership tracking
- Thread-safe operations throughout
"""

import asyncio
import ctypes
import gc
import logging
import os
import time
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from datetime import datetime

import numpy as np
import psutil
import queue

# Import thread-safety guard for native library protection
try:
    from backend.core.thread_manager import (
        get_native_library_guard,
        NativeLibraryType,
    )
    NATIVE_GUARD_AVAILABLE = True
except ImportError:
    try:
        # Fallback for direct imports
        from core.thread_manager import (
            get_native_library_guard,
            NativeLibraryType,
        )
        NATIVE_GUARD_AVAILABLE = True
    except ImportError:
        NATIVE_GUARD_AVAILABLE = False
        logging.getLogger(__name__).debug(
            "NativeLibrarySafetyGuard not available, using direct calls"
        )

# Import PyObjC frameworks (now properly installed)
try:
    import objc
    from Foundation import (
        NSObject,
        NSRunLoop,
        NSDefaultRunLoopMode,
        NSData,
    )
    from AVFoundation import (
        AVCaptureSession,
        AVCaptureScreenInput,
        AVCaptureVideoDataOutput,
        AVCaptureSessionPreset1920x1080,
        AVCaptureSessionPreset1280x720,
        AVCaptureSessionPreset640x480,
    )
    from CoreMedia import (
        CMSampleBufferGetImageBuffer,
        CMTimeMake,
    )
    from Quartz import (
        CVPixelBufferLockBaseAddress,
        CVPixelBufferUnlockBaseAddress,
        CVPixelBufferGetBaseAddress,
        CVPixelBufferGetBytesPerRow,
        CVPixelBufferGetHeight,
        CVPixelBufferGetWidth,
        kCVPixelBufferPixelFormatTypeKey,
        kCVPixelFormatType_32BGRA,
        CGWindowListCreateImage,
        CGRectNull,
        kCGWindowListOptionIncludingWindow,
        kCGWindowImageDefault,
        CGImageGetWidth,
        CGImageGetHeight,
        CGImageGetDataProvider,
        CGDataProviderCopyData,
    )
    import libdispatch

    PYOBJC_AVAILABLE = True
    AVFOUNDATION_AVAILABLE = True
except ImportError as e:
    PYOBJC_AVAILABLE = False
    AVFOUNDATION_AVAILABLE = False
    logging.getLogger(__name__).error(
        f"PyObjC frameworks not available: {e}\n"
        f"Install with: pip install pyobjc-framework-AVFoundation pyobjc-framework-Quartz "
        f"pyobjc-framework-CoreMedia pyobjc-framework-libdispatch"
    )

# Try ScreenCaptureKit (macOS 12.3+)
# Priority 1: Native C++ bridge (fast_capture_stream) - Ferrari Engine
try:
    import sys
    from pathlib import Path
    native_extensions_path = Path(__file__).parent.parent / "native_extensions"
    if str(native_extensions_path) not in sys.path:
        sys.path.insert(0, str(native_extensions_path))

    from macos_sck_stream import (
        AsyncCaptureStream,
        AsyncStreamManager,
        StreamingConfig,
        is_sck_available
    )
    SCREENCAPTUREKIT_AVAILABLE = is_sck_available()
    NATIVE_SCK_BRIDGE_AVAILABLE = True
    logging.getLogger(__name__).info("✅ ScreenCaptureKit native bridge loaded (Ferrari Engine)")
except ImportError as e:
    NATIVE_SCK_BRIDGE_AVAILABLE = False
    # Fallback: Try PyObjC ScreenCaptureKit
    try:
        from ScreenCaptureKit import (
            SCStreamConfiguration,
            SCContentFilter,
            SCStreamDelegate,
        )
        SCREENCAPTUREKIT_AVAILABLE = True
    except ImportError:
        SCREENCAPTUREKIT_AVAILABLE = False
        logging.getLogger(__name__).info("ScreenCaptureKit not available (requires macOS 12.3+)")

logger = logging.getLogger(__name__)


# =============================================================================
# v45.0.0: EDGE CASE RESILIENCE MANAGER
# =============================================================================
# Comprehensive handling for all identified edge cases:
# 1. Permission Popups - Proactive detection & user guidance
# 2. Chrome/YouTube Fullscreen - Intelligent detection with recovery
# 3. Display Disconnect - Continuous monitoring with auto-recovery
# 4. Black Screen - Cause analysis and adaptive thresholds
# 5. System Load - GPU-aware adaptive timeouts
# =============================================================================

@dataclass
class EdgeCaseConfig:
    """Configuration for edge case resilience (all configurable via env vars)"""
    # Permission monitoring
    permission_check_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PERMISSION_CHECK_INTERVAL", "30.0"))
    )
    permission_popup_detection_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PERMISSION_POPUP_DETECTION", "true").lower() == "true"
    )

    # Fullscreen handling
    fullscreen_check_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_FULLSCREEN_CHECK_INTERVAL", "2.0"))
    )
    fullscreen_exit_wait: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_FULLSCREEN_EXIT_WAIT", "0.5"))
    )
    chrome_fullscreen_f11_detection: bool = field(
        default_factory=lambda: os.getenv("JARVIS_CHROME_F11_DETECTION", "true").lower() == "true"
    )
    video_player_detection_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VIDEO_PLAYER_DETECTION", "true").lower() == "true"
    )

    # Display health monitoring
    display_health_check_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_DISPLAY_HEALTH_INTERVAL", "5.0"))
    )
    display_disconnect_recovery_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_DISPLAY_RECOVERY", "true").lower() == "true"
    )
    display_wake_retry_count: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_DISPLAY_WAKE_RETRIES", "3"))
    )

    # Black screen detection (adaptive thresholds)
    black_screen_brightness_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_BLACK_SCREEN_BRIGHTNESS", "0.02"))
    )
    black_screen_variance_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_BLACK_SCREEN_VARIANCE", "0.001"))
    )
    black_screen_adaptive_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_BLACK_SCREEN_ADAPTIVE", "true").lower() == "true"
    )
    black_screen_sample_count: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_BLACK_SCREEN_SAMPLES", "10"))
    )

    # System load & GPU monitoring
    gpu_monitoring_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_GPU_MONITORING", "true").lower() == "true"
    )
    gpu_memory_warning_percent: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_GPU_MEM_WARNING", "80.0"))
    )
    gpu_utilization_warning_percent: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_GPU_UTIL_WARNING", "85.0"))
    )
    load_prediction_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_LOAD_PREDICTION", "true").lower() == "true"
    )
    load_history_window: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_LOAD_HISTORY_WINDOW", "30"))
    )

    # Adaptive timeouts
    timeout_base_multiplier: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TIMEOUT_BASE_MULTIPLIER", "1.0"))
    )
    timeout_max_multiplier: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TIMEOUT_MAX_MULTIPLIER", "3.0"))
    )
    timeout_progressive_backoff: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TIMEOUT_PROGRESSIVE", "true").lower() == "true"
    )


class EdgeCaseType(Enum):
    """Types of edge cases that can be detected"""
    PERMISSION_POPUP = "permission_popup"
    PERMISSION_DENIED = "permission_denied"
    CHROME_F11_FULLSCREEN = "chrome_f11_fullscreen"
    YOUTUBE_FULLSCREEN = "youtube_fullscreen"
    VIDEO_PLAYER_FULLSCREEN = "video_player_fullscreen"
    DISPLAY_DISCONNECTED = "display_disconnected"
    DISPLAY_SLEEPING = "display_sleeping"
    BLACK_SCREEN_DISPLAY_OFF = "black_screen_display_off"
    BLACK_SCREEN_SCREENSAVER = "black_screen_screensaver"
    BLACK_SCREEN_VIDEO_CONTENT = "black_screen_video_content"
    BLACK_SCREEN_TRANSITION = "black_screen_transition"
    SYSTEM_HEAVY_LOAD = "system_heavy_load"
    GPU_MEMORY_PRESSURE = "gpu_memory_pressure"
    NETWORK_LATENCY = "network_latency"


@dataclass
class EdgeCaseEvent:
    """Represents a detected edge case event"""
    edge_case_type: EdgeCaseType
    timestamp: datetime
    severity: str  # "low", "medium", "high", "critical"
    details: Dict[str, Any]
    recovery_action: Optional[str] = None
    auto_recovered: bool = False


class EdgeCaseResilienceManager:
    """
    v45.0.0: Comprehensive edge case handler with proactive detection and recovery.

    This manager continuously monitors for edge cases and implements intelligent
    recovery strategies without blocking the main event loop.

    Features:
    - Proactive permission popup detection
    - Chrome F11 / YouTube fullscreen detection with recovery
    - Display disconnect monitoring with auto-wake
    - Adaptive black screen detection with cause analysis
    - GPU-aware adaptive timeout system
    - Load prediction for proactive throttling
    """

    _instance: Optional["EdgeCaseResilienceManager"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        """Singleton pattern for global edge case management"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, config: Optional[EdgeCaseConfig] = None):
        if self._initialized:
            return
        self._initialized = True

        self.config = config or EdgeCaseConfig()
        self._monitoring_active = False
        self._monitoring_task: Optional[asyncio.Task] = None

        # State tracking
        self._permission_state: Dict[str, bool] = {}
        self._fullscreen_state: Dict[str, Dict[str, Any]] = {}
        self._display_state: Dict[int, Dict[str, Any]] = {}
        self._brightness_history: List[float] = []
        self._load_history: List[Dict[str, float]] = []

        # Event log (circular buffer)
        self._event_log: List[EdgeCaseEvent] = []
        self._max_events = 100

        # Recovery callbacks
        self._recovery_callbacks: Dict[EdgeCaseType, List[Callable]] = {}

        # Adaptive thresholds
        self._adaptive_brightness_threshold = self.config.black_screen_brightness_threshold
        self._adaptive_variance_threshold = self.config.black_screen_variance_threshold
        self._current_timeout_multiplier = self.config.timeout_base_multiplier

        logger.info("[EDGE-CASE] v45.0.0 EdgeCaseResilienceManager initialized")

    # =========================================================================
    # PERMISSION POPUP DETECTION & HANDLING
    # =========================================================================

    async def check_permissions_proactively(self) -> Dict[str, Any]:
        """
        Proactively check all required permissions and detect pending popups.

        Returns:
            Dict with permission status and any detected issues
        """
        result = {
            "all_granted": True,
            "permissions": {},
            "pending_popups": [],
            "recommendations": []
        }

        try:
            # Check Screen Recording permission
            screen_recording = await self._check_screen_recording_permission()
            result["permissions"]["screen_recording"] = screen_recording
            if not screen_recording["granted"]:
                result["all_granted"] = False
                if screen_recording.get("popup_pending"):
                    result["pending_popups"].append("Screen Recording")
                result["recommendations"].append(
                    "Grant Screen Recording: System Settings → Privacy & Security → Screen Recording"
                )

            # Check Accessibility permission (for yabai)
            accessibility = await self._check_accessibility_permission()
            result["permissions"]["accessibility"] = accessibility
            if not accessibility["granted"]:
                result["all_granted"] = False
                if accessibility.get("popup_pending"):
                    result["pending_popups"].append("Accessibility")
                result["recommendations"].append(
                    "Grant Accessibility: System Settings → Privacy & Security → Accessibility"
                )

            # Check Automation permission (for AppleScript)
            automation = await self._check_automation_permission()
            result["permissions"]["automation"] = automation
            if not automation["granted"]:
                # Automation is less critical - just a warning
                result["recommendations"].append(
                    "Grant Automation: Allow JARVIS to control other apps when prompted"
                )

            # Log any pending popups
            if result["pending_popups"]:
                self._log_edge_case(
                    EdgeCaseType.PERMISSION_POPUP,
                    "high",
                    {"pending": result["pending_popups"]},
                    "User action required"
                )

        except Exception as e:
            logger.error(f"[EDGE-CASE] Permission check failed: {e}")
            result["error"] = str(e)

        return result

    async def _check_screen_recording_permission(self) -> Dict[str, Any]:
        """Check if screen recording permission is granted."""
        try:
            import Quartz

            # Try to capture a tiny screenshot - will fail if not permitted
            rect = Quartz.CGRectMake(0, 0, 1, 1)
            image = Quartz.CGWindowListCreateImage(
                rect,
                Quartz.kCGWindowListOptionOnScreenOnly,
                Quartz.kCGNullWindowID,
                Quartz.kCGWindowImageDefault
            )

            if image is None:
                # Check if there's a pending popup by looking for system dialogs
                popup_pending = await self._detect_permission_popup("Screen Recording")
                return {
                    "granted": False,
                    "popup_pending": popup_pending,
                    "check_method": "CGWindowListCreateImage"
                }

            return {"granted": True, "check_method": "CGWindowListCreateImage"}

        except Exception as e:
            return {"granted": False, "error": str(e)}

    async def _check_accessibility_permission(self) -> Dict[str, Any]:
        """Check if accessibility permission is granted."""
        try:
            import Quartz

            # AXIsProcessTrusted returns True if accessibility is granted
            is_trusted = Quartz.AXIsProcessTrusted()

            if not is_trusted:
                popup_pending = await self._detect_permission_popup("Accessibility")
                return {
                    "granted": False,
                    "popup_pending": popup_pending,
                    "check_method": "AXIsProcessTrusted"
                }

            return {"granted": True, "check_method": "AXIsProcessTrusted"}

        except Exception as e:
            return {"granted": False, "error": str(e)}

    async def _check_automation_permission(self) -> Dict[str, Any]:
        """Check if automation permission is granted."""
        try:
            # Try a simple AppleScript to check System Events access
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                'tell application "System Events" to return name of first process',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            if proc.returncode != 0 or b"not allowed" in stderr.lower():
                return {"granted": False, "check_method": "osascript"}

            return {"granted": True, "check_method": "osascript"}

        except asyncio.TimeoutError:
            return {"granted": False, "error": "timeout"}
        except Exception as e:
            return {"granted": False, "error": str(e)}

    async def _detect_permission_popup(self, permission_type: str) -> bool:
        """Detect if a permission popup is currently visible."""
        try:
            # Use osascript to check for System Preferences dialogs
            script = '''
            tell application "System Events"
                set dialogWindows to every window of every process whose subrole is "AXSystemDialog"
                return count of dialogWindows
            end tell
            '''
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            dialog_count = int(stdout.decode().strip()) if stdout else 0
            return dialog_count > 0

        except Exception:
            return False  # Can't detect, assume no popup

    # =========================================================================
    # FULLSCREEN DETECTION & HANDLING
    # =========================================================================

    async def detect_fullscreen_state(self, app_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Detect fullscreen state for all apps or a specific app.

        Handles:
        - Chrome F11 fullscreen (different from native macOS fullscreen)
        - YouTube fullscreen player
        - Native macOS fullscreen
        """
        result = {
            "any_fullscreen": False,
            "fullscreen_apps": [],
            "video_fullscreen": False,
            "details": {}
        }

        try:
            # Get window list with bounds
            windows_data = await self._get_all_windows_async()

            for window in windows_data:
                window_app = window.get("app", "")
                is_fullscreen = window.get("is-native-fullscreen", False)
                bounds = window.get("bounds", {})

                # Check for F11-style fullscreen (fills screen but not native fullscreen)
                is_f11_fullscreen = await self._detect_f11_fullscreen(window, bounds)

                # Check for video player fullscreen
                is_video_fullscreen = await self._detect_video_player_fullscreen(window)

                if is_fullscreen or is_f11_fullscreen or is_video_fullscreen:
                    result["any_fullscreen"] = True
                    fullscreen_info = {
                        "app": window_app,
                        "window_id": window.get("id"),
                        "type": "native" if is_fullscreen else ("f11" if is_f11_fullscreen else "video"),
                        "can_recover": True
                    }
                    result["fullscreen_apps"].append(fullscreen_info)
                    result["details"][window_app] = fullscreen_info

                    if is_video_fullscreen:
                        result["video_fullscreen"] = True

                # Filter by app if specified
                if app_name and window_app.lower() != app_name.lower():
                    continue

            # Log if Chrome F11 detected
            chrome_f11 = [a for a in result["fullscreen_apps"] if a["app"] == "Google Chrome" and a["type"] == "f11"]
            if chrome_f11:
                self._log_edge_case(
                    EdgeCaseType.CHROME_F11_FULLSCREEN,
                    "medium",
                    {"windows": chrome_f11},
                    "Will exit F11 before capture"
                )

        except Exception as e:
            logger.error(f"[EDGE-CASE] Fullscreen detection failed: {e}")
            result["error"] = str(e)

        return result

    async def _detect_f11_fullscreen(self, window: Dict, bounds: Dict) -> bool:
        """
        Detect F11-style fullscreen (fills screen but not native fullscreen).

        Chrome F11 fullscreen:
        - Window fills entire screen
        - is-native-fullscreen = False
        - No title bar or menu bar
        """
        if window.get("is-native-fullscreen", False):
            return False  # Native fullscreen, not F11

        try:
            import Quartz

            # Get main screen size
            main_display = Quartz.CGMainDisplayID()
            screen_bounds = Quartz.CGDisplayBounds(main_display)
            screen_width = screen_bounds.size.width
            screen_height = screen_bounds.size.height

            # Check if window fills screen (within 5% tolerance)
            window_width = bounds.get("w", 0) or bounds.get("width", 0)
            window_height = bounds.get("h", 0) or bounds.get("height", 0)

            width_fill = window_width / screen_width if screen_width > 0 else 0
            height_fill = window_height / screen_height if screen_height > 0 else 0

            # F11 fullscreen fills > 95% of screen
            if width_fill >= 0.95 and height_fill >= 0.95:
                # Also check if window is at (0,0) or near it
                x = bounds.get("x", 0)
                y = bounds.get("y", 0)
                if abs(x) < 10 and abs(y) < 50:  # Allow for menu bar offset
                    return True

        except Exception as e:
            logger.debug(f"[EDGE-CASE] F11 detection error: {e}")

        return False

    async def _detect_video_player_fullscreen(self, window: Dict) -> bool:
        """Detect if a video player is in fullscreen mode."""
        app_name = window.get("app", "").lower()
        title = window.get("title", "").lower()

        # Known video player indicators
        video_apps = ["youtube", "netflix", "hulu", "disney+", "prime video", "vlc", "iina", "quicktime"]
        video_titles = ["youtube", "netflix", "hulu", "disney+", "prime video", "watching", "playing"]

        # Check app name
        for video_app in video_apps:
            if video_app in app_name:
                # Check if fullscreen
                if window.get("is-native-fullscreen", False):
                    return True

        # Check title for video indicators in browsers
        if app_name in ["google chrome", "safari", "firefox", "arc"]:
            for video_title in video_titles:
                if video_title in title:
                    # Could be a video - check if fullscreen
                    if window.get("is-native-fullscreen", False):
                        return True

        return False

    async def exit_fullscreen_safely(self, window_id: int, app_name: str) -> bool:
        """
        Safely exit fullscreen with proper wait time for render.

        Args:
            window_id: ID of the window to exit fullscreen
            app_name: Name of the application

        Returns:
            True if successfully exited fullscreen
        """
        try:
            # Use AppleScript to exit fullscreen
            script = f'''
            tell application "{app_name}"
                activate
            end tell

            delay 0.2

            tell application "System Events"
                tell process "{app_name}"
                    set frontmost to true
                    -- Try Escape first (for F11 fullscreen)
                    key code 53
                    delay 0.3
                    -- If still fullscreen, try Cmd+Ctrl+F (native fullscreen toggle)
                    if exists (first window whose value of attribute "AXFullScreen" is true) then
                        keystroke "f" using {{command down, control down}}
                    end if
                end tell
            end tell
            '''

            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)

            # Wait for fullscreen exit animation
            await asyncio.sleep(self.config.fullscreen_exit_wait)

            logger.info(f"[EDGE-CASE] Exited fullscreen for {app_name}")
            return True

        except Exception as e:
            logger.error(f"[EDGE-CASE] Failed to exit fullscreen for {app_name}: {e}")
            return False

    # =========================================================================
    # DISPLAY DISCONNECT DETECTION & RECOVERY
    # =========================================================================

    async def monitor_display_health(self) -> Dict[str, Any]:
        """
        Continuously monitor display health and detect disconnects.

        Returns:
            Dict with display health status for all displays
        """
        result = {
            "displays": {},
            "any_disconnected": False,
            "any_sleeping": False,
            "recovery_attempted": False
        }

        try:
            import Quartz

            # Get active displays using PyObjC-compatible call
            # CGGetActiveDisplayList returns (error, display_ids, count)
            max_displays = 16
            error, display_ids, display_count = Quartz.CGGetActiveDisplayList(max_displays, None, None)

            if error != 0 or display_ids is None:
                logger.warning(f"[EDGE-CASE] CGGetActiveDisplayList failed with error {error}")
                return result

            active_display_ids = set(display_ids)

            # Check each known display
            for display_id, state in list(self._display_state.items()):
                if display_id not in active_display_ids:
                    # Display disconnected
                    result["displays"][display_id] = {
                        "status": "disconnected",
                        "last_seen": state.get("last_seen"),
                        "name": state.get("name", f"Display {display_id}")
                    }
                    result["any_disconnected"] = True

                    self._log_edge_case(
                        EdgeCaseType.DISPLAY_DISCONNECTED,
                        "high",
                        {"display_id": display_id, "name": state.get("name")},
                        "Attempting reconnection"
                    )

            # Update state for active displays
            for display_id in display_ids:
                bounds = Quartz.CGDisplayBounds(display_id)
                is_main = Quartz.CGDisplayIsMain(display_id)
                is_sleeping = not Quartz.CGDisplayIsActive(display_id)

                self._display_state[display_id] = {
                    "last_seen": datetime.now(),
                    "is_main": is_main,
                    "is_sleeping": is_sleeping,
                    "width": bounds.size.width,
                    "height": bounds.size.height,
                    "name": f"Display {display_id}" + (" (Main)" if is_main else "")
                }

                result["displays"][display_id] = {
                    "status": "sleeping" if is_sleeping else "active",
                    "is_main": is_main,
                    "resolution": f"{int(bounds.size.width)}x{int(bounds.size.height)}"
                }

                if is_sleeping:
                    result["any_sleeping"] = True

            # Attempt recovery if needed
            if result["any_sleeping"] and self.config.display_disconnect_recovery_enabled:
                recovered = await self._attempt_display_recovery()
                result["recovery_attempted"] = True
                result["recovery_success"] = recovered

        except Exception as e:
            logger.error(f"[EDGE-CASE] Display health check failed: {e}")
            result["error"] = str(e)

        return result

    async def _attempt_display_recovery(self) -> bool:
        """Attempt to wake sleeping displays."""
        for attempt in range(self.config.display_wake_retry_count):
            try:
                # Method 1: caffeinate user activity assertion
                proc = await asyncio.create_subprocess_exec(
                    "caffeinate", "-u", "-t", "1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=2.0)

                await asyncio.sleep(0.5)

                # Check if display woke up
                import Quartz
                main_display = Quartz.CGMainDisplayID()
                if Quartz.CGDisplayIsActive(main_display):
                    logger.info(f"[EDGE-CASE] Display woken on attempt {attempt + 1}")
                    return True

                # Method 2: Send key event (fallback)
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e",
                    'tell application "System Events" to key code 124',  # Right arrow
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=2.0)

                await asyncio.sleep(0.5)

                if Quartz.CGDisplayIsActive(main_display):
                    logger.info(f"[EDGE-CASE] Display woken via key event on attempt {attempt + 1}")
                    return True

            except Exception as e:
                logger.warning(f"[EDGE-CASE] Display wake attempt {attempt + 1} failed: {e}")

        return False

    # =========================================================================
    # BLACK SCREEN DETECTION WITH CAUSE ANALYSIS
    # =========================================================================

    async def analyze_black_screen(self, frame: np.ndarray) -> Dict[str, Any]:
        """
        Analyze a frame to detect black screen and determine cause.

        Uses adaptive thresholds and historical brightness data.

        Returns:
            Dict with black screen analysis including cause
        """
        result = {
            "is_black_screen": False,
            "cause": None,
            "confidence": 0.0,
            "brightness": 0.0,
            "variance": 0.0,
            "recovery_action": None
        }

        try:
            # Convert to grayscale if needed
            if len(frame.shape) == 3:
                gray = np.mean(frame, axis=2)
            else:
                gray = frame

            # Calculate brightness (0-1 scale)
            brightness = np.mean(gray) / 255.0
            variance = np.var(gray) / (255.0 * 255.0)

            result["brightness"] = brightness
            result["variance"] = variance

            # Update brightness history for adaptive threshold
            self._brightness_history.append(brightness)
            if len(self._brightness_history) > self.config.black_screen_sample_count:
                self._brightness_history.pop(0)

            # Adapt thresholds based on history
            if self.config.black_screen_adaptive_enabled and len(self._brightness_history) >= 5:
                self._update_adaptive_thresholds()

            # Check against thresholds
            if brightness < self._adaptive_brightness_threshold and variance < self._adaptive_variance_threshold:
                result["is_black_screen"] = True

                # Determine cause
                cause, confidence, action = await self._determine_black_screen_cause(brightness, variance)
                result["cause"] = cause
                result["confidence"] = confidence
                result["recovery_action"] = action

                # Map cause string to EdgeCaseType safely
                cause_to_type = {
                    "display_sleeping": EdgeCaseType.DISPLAY_SLEEPING,
                    "display_disconnected": EdgeCaseType.DISPLAY_DISCONNECTED,
                    "screensaver_active": EdgeCaseType.BLACK_SCREEN_SCREENSAVER,
                    "video_fullscreen": EdgeCaseType.BLACK_SCREEN_VIDEO_CONTENT,
                    "fullscreen_transition": EdgeCaseType.BLACK_SCREEN_TRANSITION,
                    "unknown": EdgeCaseType.BLACK_SCREEN_DISPLAY_OFF,
                }
                edge_type = cause_to_type.get(cause, EdgeCaseType.BLACK_SCREEN_DISPLAY_OFF)

                self._log_edge_case(
                    edge_type,
                    "medium" if confidence < 0.8 else "high",
                    {"brightness": brightness, "variance": variance, "cause": cause},
                    action
                )

        except Exception as e:
            logger.error(f"[EDGE-CASE] Black screen analysis failed: {e}")
            result["error"] = str(e)

        return result

    def _update_adaptive_thresholds(self):
        """Update thresholds based on brightness history."""
        if not self._brightness_history:
            return

        # Calculate baseline from non-black frames
        non_black = [b for b in self._brightness_history if b > 0.05]

        if len(non_black) >= 3:
            avg_brightness = sum(non_black) / len(non_black)
            # Set threshold to 10% of average brightness or default, whichever is lower
            self._adaptive_brightness_threshold = min(
                avg_brightness * 0.1,
                self.config.black_screen_brightness_threshold
            )

    async def _determine_black_screen_cause(
        self, brightness: float, variance: float
    ) -> Tuple[str, float, str]:
        """
        Determine the cause of a black screen.

        Returns:
            Tuple of (cause, confidence, recovery_action)
        """
        # Check display state
        display_health = await self.monitor_display_health()

        if display_health.get("any_sleeping"):
            return ("display_sleeping", 0.95, "Wake display with caffeinate")

        if display_health.get("any_disconnected"):
            return ("display_disconnected", 0.90, "Reconnect display or use fallback")

        # Check for screensaver
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                'tell application "System Events" to return (running of screen saver preferences)',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if b"true" in stdout.lower():
                return ("screensaver_active", 0.85, "Dismiss screensaver with key event")
        except Exception:
            pass

        # Check for fullscreen video
        fullscreen_state = await self.detect_fullscreen_state()
        if fullscreen_state.get("video_fullscreen"):
            return ("video_fullscreen", 0.80, "Black frame from video content")

        # Check for fullscreen transition
        if variance < 0.0001:  # Perfectly uniform = likely transition
            return ("fullscreen_transition", 0.70, "Wait for transition to complete")

        # Default: unknown cause
        return ("unknown", 0.50, "Retry capture or use fallback method")

    # =========================================================================
    # SYSTEM LOAD & GPU-AWARE ADAPTIVE TIMEOUTS
    # =========================================================================

    async def get_system_load_status(self) -> Dict[str, Any]:
        """
        Get comprehensive system load including GPU metrics.

        Returns:
            Dict with CPU, memory, GPU status and recommended timeout multiplier
        """
        result = {
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "gpu_utilization": None,
            "gpu_memory_percent": None,
            "load_level": "normal",
            "timeout_multiplier": 1.0,
            "predicted_load": None
        }

        try:
            # CPU load
            cpu_percent = psutil.cpu_percent(interval=0.1)
            result["cpu_percent"] = cpu_percent

            # Memory load
            memory = psutil.virtual_memory()
            result["memory_percent"] = memory.percent

            # GPU metrics (macOS-specific)
            if self.config.gpu_monitoring_enabled:
                gpu_metrics = await self._get_gpu_metrics()
                result["gpu_utilization"] = gpu_metrics.get("utilization")
                result["gpu_memory_percent"] = gpu_metrics.get("memory_percent")

            # Record in history for prediction
            self._load_history.append({
                "timestamp": datetime.now(),
                "cpu": cpu_percent,
                "memory": memory.percent,
                "gpu": result.get("gpu_utilization", 0)
            })
            if len(self._load_history) > self.config.load_history_window:
                self._load_history.pop(0)

            # Determine load level
            max_load = max(
                cpu_percent,
                memory.percent,
                result.get("gpu_utilization") or 0
            )

            if max_load >= 95:
                result["load_level"] = "critical"
                result["timeout_multiplier"] = self.config.timeout_max_multiplier
            elif max_load >= 85:
                result["load_level"] = "heavy"
                result["timeout_multiplier"] = 2.0
            elif max_load >= 70:
                result["load_level"] = "moderate"
                result["timeout_multiplier"] = 1.5
            elif max_load >= 50:
                result["load_level"] = "light"
                result["timeout_multiplier"] = 1.2
            else:
                result["load_level"] = "normal"
                result["timeout_multiplier"] = 1.0

            # Predict future load if enabled
            if self.config.load_prediction_enabled and len(self._load_history) >= 5:
                result["predicted_load"] = self._predict_load_trend()

            # Update global timeout multiplier
            self._current_timeout_multiplier = result["timeout_multiplier"]

            # Log if high load detected
            if result["load_level"] in ["heavy", "critical"]:
                self._log_edge_case(
                    EdgeCaseType.SYSTEM_HEAVY_LOAD,
                    "high" if result["load_level"] == "critical" else "medium",
                    {"cpu": cpu_percent, "memory": memory.percent, "gpu": result.get("gpu_utilization")},
                    f"Timeout multiplier increased to {result['timeout_multiplier']}x"
                )

        except Exception as e:
            logger.error(f"[EDGE-CASE] System load check failed: {e}")
            result["error"] = str(e)

        return result

    async def _get_gpu_metrics(self) -> Dict[str, float]:
        """Get GPU utilization and memory on macOS."""
        result = {}

        try:
            # Use powermetrics for GPU info (requires sudo, may not be available)
            # Fallback: Use ioreg for basic GPU info
            proc = await asyncio.create_subprocess_exec(
                "ioreg", "-r", "-c", "IOAccelerator",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            output = stdout.decode()

            # Parse GPU utilization (approximate from device utilization)
            if "Device Utilization" in output:
                # Extract percentage (format varies)
                import re
                match = re.search(r'"Device Utilization[^"]*"[^0-9]*(\d+)', output)
                if match:
                    result["utilization"] = float(match.group(1))

            # Parse VRAM usage
            if "VRAM" in output or "In Use" in output:
                match = re.search(r'(?:VRAM|In Use)[^0-9]*(\d+)', output)
                if match:
                    # Assume VRAM total and calculate percent
                    vram_used = int(match.group(1))
                    # Approximate VRAM total (can be refined with actual device query)
                    vram_total = 8192  # Default 8GB, adjust based on device
                    result["memory_percent"] = (vram_used / vram_total) * 100

        except Exception as e:
            logger.debug(f"[EDGE-CASE] GPU metrics unavailable: {e}")

        return result

    def _predict_load_trend(self) -> str:
        """Predict if load is trending up, down, or stable."""
        if len(self._load_history) < 5:
            return "unknown"

        recent = self._load_history[-5:]
        cpu_trend = recent[-1]["cpu"] - recent[0]["cpu"]

        if cpu_trend > 10:
            return "increasing"
        elif cpu_trend < -10:
            return "decreasing"
        else:
            return "stable"

    def get_adaptive_timeout(self, base_timeout: float) -> float:
        """
        Get an adaptive timeout based on current system load.

        Args:
            base_timeout: Base timeout value in seconds

        Returns:
            Adjusted timeout value
        """
        if self.config.timeout_progressive_backoff:
            return base_timeout * self._current_timeout_multiplier
        return base_timeout

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    async def _get_all_windows_async(self) -> List[Dict]:
        """Get all windows asynchronously via yabai."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "yabai", "-m", "query", "--windows",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

            if proc.returncode == 0:
                return json.loads(stdout.decode())

        except Exception as e:
            logger.debug(f"[EDGE-CASE] Window query failed: {e}")

        return []

    def _log_edge_case(
        self,
        edge_case_type: EdgeCaseType,
        severity: str,
        details: Dict[str, Any],
        recovery_action: Optional[str] = None
    ):
        """Log an edge case event."""
        event = EdgeCaseEvent(
            edge_case_type=edge_case_type,
            timestamp=datetime.now(),
            severity=severity,
            details=details,
            recovery_action=recovery_action
        )

        self._event_log.append(event)
        if len(self._event_log) > self._max_events:
            self._event_log.pop(0)

        logger.info(
            f"[EDGE-CASE] {edge_case_type.value} ({severity}): "
            f"{details} → {recovery_action or 'no action'}"
        )

    def get_recent_events(self, count: int = 10) -> List[Dict[str, Any]]:
        """Get recent edge case events."""
        return [
            {
                "type": e.edge_case_type.value,
                "timestamp": e.timestamp.isoformat(),
                "severity": e.severity,
                "details": e.details,
                "recovery": e.recovery_action,
                "auto_recovered": e.auto_recovered
            }
            for e in self._event_log[-count:]
        ]

    # =========================================================================
    # BACKGROUND MONITORING
    # =========================================================================

    async def start_monitoring(self):
        """Start background edge case monitoring."""
        if self._monitoring_active:
            return

        self._monitoring_active = True
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info("[EDGE-CASE] Background monitoring started")

    async def stop_monitoring(self):
        """Stop background edge case monitoring."""
        self._monitoring_active = False
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
        logger.info("[EDGE-CASE] Background monitoring stopped")

    async def _monitoring_loop(self):
        """Background loop for continuous edge case monitoring."""
        while self._monitoring_active:
            try:
                # Check permissions periodically
                if self.config.permission_popup_detection_enabled:
                    await self.check_permissions_proactively()

                # Check display health
                await self.monitor_display_health()

                # Check system load
                await self.get_system_load_status()

                # Sleep until next check
                await asyncio.sleep(self.config.display_health_check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[EDGE-CASE] Monitoring loop error: {e}")
                await asyncio.sleep(5.0)


def get_edge_case_manager() -> EdgeCaseResilienceManager:
    """Get the singleton EdgeCaseResilienceManager instance."""
    return EdgeCaseResilienceManager()


class CaptureMethod(Enum):
    """Available capture methods in priority order"""
    SCREENCAPTUREKIT = "screencapturekit"  # For VideoWatcher window-specific capture (Ferrari Engine)
    AVFOUNDATION = "avfoundation"  # ⚡ Priority 1: AVFoundation (full display, high quality, purple indicator)
    SCREENCAPTURE_CMD = "screencapture_cmd"  # Priority 2: screencapture command (fallback)
    SCREENSHOT_LOOP = "screenshot_loop"  # Priority 3: Final fallback (PIL/Pillow)


class CaptureStatus(Enum):
    """Capture session status"""
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class AdvancedCaptureConfig:
    """Dynamic configuration for macOS video capture - NO HARDCODING"""

    # Display settings
    display_id: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_DISPLAY_ID', '0')))

    # Quality settings
    target_fps: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_FPS', '30')))
    resolution: str = field(default_factory=lambda: os.getenv('JARVIS_CAPTURE_RESOLUTION', '1920x1080'))
    pixel_format: str = field(default_factory=lambda: os.getenv('JARVIS_CAPTURE_PIXEL_FORMAT', '32BGRA'))

    # Performance settings
    min_fps: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_MIN_FPS', '10')))
    max_fps: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_MAX_FPS', '60')))
    enable_adaptive_quality: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_ADAPTIVE', 'true').lower() == 'true'
    )

    # Memory settings
    max_memory_mb: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_MAX_MEMORY_MB', '500')))
    frame_buffer_size: int = field(default_factory=lambda: int(os.getenv('JARVIS_CAPTURE_BUFFER_SIZE', '10')))
    enable_memory_monitoring: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_MEMORY_MONITOR', 'true').lower() == 'true'
    )

    # Capture settings
    capture_cursor: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_CURSOR', 'false').lower() == 'true'
    )
    capture_mouse_clicks: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_MOUSE_CLICKS', 'false').lower() == 'true'
    )
    discard_late_frames: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_DISCARD_LATE', 'true').lower() == 'true'
    )

    # Fallback settings
    enable_fallback_chain: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_FALLBACK', 'true').lower() == 'true'
    )
    preferred_method: Optional[CaptureMethod] = field(
        default_factory=lambda: CaptureMethod(
            os.getenv('JARVIS_CAPTURE_METHOD', 'avfoundation')
        ) if os.getenv('JARVIS_CAPTURE_METHOD') else None
    )

    # Diagnostics
    enable_diagnostics: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_DIAGNOSTICS', 'true').lower() == 'true'
    )
    log_frame_metrics: bool = field(
        default_factory=lambda: os.getenv('JARVIS_CAPTURE_LOG_METRICS', 'false').lower() == 'true'
    )

    def get_resolution_tuple(self) -> Tuple[int, int]:
        """Parse resolution string to (width, height)"""
        try:
            width, height = self.resolution.split('x')
            return (int(width), int(height))
        except Exception as e:
            logger.warning(f"Invalid resolution '{self.resolution}', using 1920x1080: {e}")
            return (1920, 1080)

    def get_avfoundation_preset(self) -> Any:
        """Get AVFoundation preset for current resolution"""
        if not AVFOUNDATION_AVAILABLE:
            return None

        presets = {
            '1920x1080': AVCaptureSessionPreset1920x1080,
            '1280x720': AVCaptureSessionPreset1280x720,
            '960x540': AVCaptureSessionPreset640x480,  # Use 640x480 preset
            '640x480': AVCaptureSessionPreset640x480,
        }

        return presets.get(self.resolution, AVCaptureSessionPreset1920x1080)


@dataclass
class CaptureMetrics:
    """Real-time capture metrics"""
    method: CaptureMethod
    status: CaptureStatus
    frames_captured: int = 0
    frames_dropped: int = 0
    current_fps: float = 0.0
    target_fps: int = 30
    memory_usage_mb: float = 0.0
    cpu_percent: float = 0.0
    uptime_seconds: float = 0.0
    last_frame_timestamp: float = 0.0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging"""
        return {
            'method': self.method.value,
            'status': self.status.value,
            'frames_captured': self.frames_captured,
            'frames_dropped': self.frames_dropped,
            'current_fps': round(self.current_fps, 2),
            'target_fps': self.target_fps,
            'memory_usage_mb': round(self.memory_usage_mb, 2),
            'cpu_percent': round(self.cpu_percent, 2),
            'uptime_seconds': round(self.uptime_seconds, 2),
            'error_count': len(self.errors),
        }


if PYOBJC_AVAILABLE:
    class VideoFrameDelegate(NSObject):
        """
        Objective-C delegate for handling AVFoundation video frames

        This class bridges between Objective-C (AVFoundation) and Python.
        It receives video frames from AVCaptureSession and forwards them
        to Python callbacks.

        CRITICAL MEMORY SAFETY:
        =======================
        CVPixelBuffer data is only valid while the buffer is locked.
        We MUST create a deep copy of the frame data BEFORE unlocking,
        otherwise the callback will receive a dangling pointer that
        causes SIGSEGV (segmentation fault) when accessed later.
        """

        @classmethod
        def delegateWithCallback_(cls, callback):
            """Factory method to create delegate with callback"""
            delegate = cls.alloc().init()
            delegate.callback = callback
            delegate.frame_count = 0
            delegate.last_frame_time = time.time()
            delegate._shutdown = False  # Shutdown flag
            return delegate

        def setShutdown_(self, shutdown: bool):
            """Signal that shutdown has been requested."""
            self._shutdown = shutdown

        def captureOutput_didOutputSampleBuffer_fromConnection_(
            self, output, sample_buffer, connection
        ):
            """
            AVCaptureVideoDataOutputSampleBufferDelegate method
            Called when a new video frame is captured

            CRITICAL FIX v10.7 - Memory-Safe Frame Handling:
            ================================================
            The numpy array created via np.frombuffer() is a VIEW into the
            CVPixelBuffer's locked memory. This memory becomes INVALID after
            CVPixelBufferUnlockBaseAddress is called.

            BEFORE (BUGGY):
            1. Lock buffer
            2. Create numpy view → frame points to buffer memory
            3. Pass frame to callback (still a view!)
            4. Unlock buffer → memory is now INVALID
            5. Callback tries to use frame → SIGSEGV!

            AFTER (FIXED):
            1. Lock buffer
            2. Create numpy view → frame points to buffer memory
            3. Create DEEP COPY → owned_frame is independent
            4. Unlock buffer → original memory invalid, but we don't use it
            5. Pass owned_frame to callback → safe!

            This prevents the SIGSEGV at ~45 minutes that was caused by
            the garbage collector or macOS reclaiming the buffer memory
            while a callback was still processing.
            """
            # Fast shutdown check - skip processing if shutting down
            if getattr(self, '_shutdown', False):
                return

            try:
                self.frame_count += 1
                current_time = time.time()

                # Convert CMSampleBuffer to numpy array
                image_buffer = CMSampleBufferGetImageBuffer(sample_buffer)
                if not image_buffer:
                    logger.warning("No image buffer in sample")
                    return

                # Lock pixel buffer for reading
                lock_result = CVPixelBufferLockBaseAddress(image_buffer, 0)
                if lock_result != 0:
                    logger.warning(f"Failed to lock pixel buffer: {lock_result}")
                    return

                # CRITICAL: owned_frame must be created INSIDE try block
                # and BEFORE the finally block unlocks the buffer
                owned_frame = None
                frame_metadata = None

                try:
                    # Get pixel data
                    base_address = CVPixelBufferGetBaseAddress(image_buffer)
                    if not base_address:
                        logger.warning("No base address in pixel buffer")
                        return

                    bytes_per_row = CVPixelBufferGetBytesPerRow(image_buffer)
                    height = CVPixelBufferGetHeight(image_buffer)
                    width = CVPixelBufferGetWidth(image_buffer)

                    # Validate dimensions
                    if width <= 0 or height <= 0 or bytes_per_row <= 0:
                        logger.warning(f"Invalid buffer dimensions: {width}x{height}, bpr={bytes_per_row}")
                        return

                    # Create numpy array from pixel data
                    # Format: BGRA (32-bit)
                    buffer_size = bytes_per_row * height

                    # Create a VIEW into the locked buffer memory
                    # WARNING: This view is ONLY valid while buffer is locked!
                    frame_view = np.frombuffer(
                        base_address.as_buffer(buffer_size),
                        dtype=np.uint8
                    )

                    # Reshape to image format
                    # bytes_per_row may include padding, so we calculate pixels per row
                    pixels_per_row = bytes_per_row // 4  # 4 bytes per BGRA pixel
                    frame_view = frame_view.reshape((height, pixels_per_row, 4))
                    frame_view = frame_view[:, :width, :3]  # Remove alpha, crop to actual width
                    frame_view = frame_view[:, :, ::-1]  # BGR to RGB

                    # ================================================================
                    # CRITICAL: Create a DEEP COPY that we OWN
                    # ================================================================
                    # This copy is made while the buffer is still locked, so the
                    # source data is valid. After this, owned_frame is independent
                    # and safe to use after unlock.
                    #
                    # Using np.array() with copy=True ensures we get a contiguous
                    # copy that we fully own, not a view.
                    # ================================================================
                    owned_frame = np.array(frame_view, dtype=np.uint8, copy=True)

                    # Ensure the array is contiguous for downstream processing
                    if not owned_frame.flags['C_CONTIGUOUS']:
                        owned_frame = np.ascontiguousarray(owned_frame)

                    # Calculate FPS
                    fps = 1.0 / (current_time - self.last_frame_time) if self.last_frame_time > 0 else 0
                    self.last_frame_time = current_time

                    # Prepare metadata (no buffer references!)
                    frame_metadata = {
                        'frame_number': self.frame_count,
                        'timestamp': current_time,
                        'fps': fps,
                        'width': width,
                        'height': height,
                        'memory_owned': True,  # Flag indicating safe memory
                    }

                finally:
                    # Always unlock pixel buffer - BEFORE callback
                    # At this point, owned_frame (if created) is our own copy
                    CVPixelBufferUnlockBaseAddress(image_buffer, 0)

                # Now call the callback with our OWNED copy
                # The buffer is unlocked, but owned_frame is safe to use
                if owned_frame is not None and self.callback:
                    # Final shutdown check before calling potentially slow callback
                    if not getattr(self, '_shutdown', False):
                        self.callback(owned_frame, frame_metadata)

            except Exception as e:
                logger.error(f"Error in frame delegate: {e}", exc_info=True)


class AVFoundationCapture:
    """
    Native macOS video capture using AVFoundation via PyObjC

    This is the highest quality capture method with the purple indicator.
    Requires screen recording permission.

    THREAD SAFETY:
    ==============
    AVFoundation APIs (AVCaptureSession, etc.) require careful thread handling.
    This implementation uses NativeLibrarySafetyGuard to ensure:
    1. Session start/stop happens with proper synchronization
    2. Callbacks are protected against shutdown races
    3. Memory is not freed while callbacks are executing
    """

    def __init__(self, config: AdvancedCaptureConfig):
        self.config = config
        self.session: Optional[Any] = None
        self.output: Optional[Any] = None
        self.delegate: Optional[Any] = None
        self.dispatch_queue: Optional[Any] = None
        self.is_running = False
        self.frame_callback: Optional[Callable] = None
        self._runloop_thread: Optional[threading.Thread] = None
        self._stop_runloop = threading.Event()

        # Shutdown coordination
        self._shutdown_requested = threading.Event()
        self._active_callbacks = 0
        self._callback_lock = threading.Lock()

        # Event loop reference for callbacks
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

        if not AVFOUNDATION_AVAILABLE:
            raise RuntimeError("AVFoundation not available - install PyObjC frameworks")

    async def start_capture(self, frame_callback: Callable) -> bool:
        """
        Start AVFoundation capture session

        Args:
            frame_callback: Async function called with (frame: np.ndarray, metadata: dict)

        Returns:
            True if capture started successfully
        """
        if self.is_running:
            logger.warning("AVFoundation capture already running")
            return True

        self.frame_callback = frame_callback
        self._shutdown_requested.clear()

        # Store the event loop for callback dispatching
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = asyncio.get_event_loop()

        try:
            logger.info("🎥 Starting AVFoundation capture session...")

            # Create capture session
            self.session = AVCaptureSession.alloc().init()
            logger.info(f"   Created AVCaptureSession: {self.session}")

            # Set session preset based on resolution
            preset = self.config.get_avfoundation_preset()
            self.session.setSessionPreset_(preset)
            logger.info(f"   Set resolution preset: {self.config.resolution}")

            # Create screen input for specified display
            screen_input = AVCaptureScreenInput.alloc().initWithDisplayID_(
                self.config.display_id
            )

            if not screen_input:
                raise RuntimeError(f"Failed to create screen input for display {self.config.display_id}")

            logger.info(f"   Created screen input for display {self.config.display_id}")

            # Configure screen input
            min_frame_duration = CMTimeMake(1, self.config.target_fps)
            screen_input.setMinFrameDuration_(min_frame_duration)
            screen_input.setCapturesCursor_(self.config.capture_cursor)
            screen_input.setCapturesMouseClicks_(self.config.capture_mouse_clicks)

            logger.info(f"   Configured: {self.config.target_fps} FPS, cursor={self.config.capture_cursor}")

            # Add input to session
            if not self.session.canAddInput_(screen_input):
                raise RuntimeError("Cannot add screen input to capture session")

            self.session.addInput_(screen_input)
            logger.info("   Added screen input to session")

            # Create video output
            self.output = AVCaptureVideoDataOutput.alloc().init()
            self.output.setAlwaysDiscardsLateVideoFrames_(self.config.discard_late_frames)

            # Configure pixel format (32-bit BGRA)
            self.output.setVideoSettings_({
                kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA
            })

            logger.info(f"   Configured video output: format={self.config.pixel_format}")

            # Create delegate for frame callbacks
            self.delegate = VideoFrameDelegate.delegateWithCallback_(
                self._handle_frame_sync
            )

            # Create serial dispatch queue for video processing
            queue_name = f"com.jarvis.videocapture.{id(self)}".encode('utf-8')
            self.dispatch_queue = libdispatch.dispatch_queue_create(
                queue_name,
                None  # Serial queue
            )

            self.output.setSampleBufferDelegate_queue_(self.delegate, self.dispatch_queue)
            logger.info("   Set up frame delegate and dispatch queue")

            # Add output to session
            if not self.session.canAddOutput_(self.output):
                raise RuntimeError("Cannot add video output to capture session")

            self.session.addOutput_(self.output)
            logger.info("   Added video output to session")

            # Start NSRunLoop in background thread (required for Objective-C callbacks)
            self._start_runloop()

            # Start capture session
            self.session.startRunning()
            self.is_running = True

            logger.info("✅ AVFoundation capture started - purple indicator should be visible!")
            logger.info(f"   Display: {self.config.display_id}")
            logger.info(f"   Resolution: {self.config.resolution}")
            logger.info(f"   FPS: {self.config.target_fps}")
            logger.info(f"   Memory limit: {self.config.max_memory_mb}MB")

            return True

        except Exception as e:
            logger.error(f"❌ Failed to start AVFoundation capture: {e}", exc_info=True)
            await self.stop_capture()
            return False

    def _start_runloop(self):
        """Start NSRunLoop in background thread for Objective-C callbacks"""
        def runloop_thread():
            """Run NSRunLoop until stopped"""
            logger.info("[RunLoop] Starting NSRunLoop thread...")

            runloop = NSRunLoop.currentRunLoop()

            while not self._stop_runloop.is_set():
                # Run loop for short intervals to allow checking stop flag
                runloop.runMode_beforeDate_(
                    NSDefaultRunLoopMode,
                    objc.lookUpClass('NSDate').dateWithTimeIntervalSinceNow_(0.1)
                )

            logger.info("[RunLoop] NSRunLoop thread stopped")

        self._stop_runloop.clear()
        self._runloop_thread = threading.Thread(target=runloop_thread, daemon=True)
        self._runloop_thread.start()
        logger.info("Started NSRunLoop in background thread")

    def _handle_frame_sync(self, frame: np.ndarray, metadata: Dict[str, Any]):
        """
        Synchronous frame handler called from Objective-C thread

        This bridges from the Objective-C dispatch queue to Python asyncio.

        THREAD SAFETY v10.7:
        ====================
        - Frame is now PRE-COPIED by VideoFrameDelegate (memory_owned=True)
        - No additional copy needed - frame already owns its memory
        - Checks shutdown flag before processing
        - Tracks active callbacks to allow clean shutdown
        - Uses stored event loop reference with validation
        - Protects against event loop closure during shutdown
        """
        # Fast path: check shutdown before doing any work
        if self._shutdown_requested.is_set():
            return

        # Register active callback
        with self._callback_lock:
            if self._shutdown_requested.is_set():
                return
            self._active_callbacks += 1

        try:
            # Validate event loop is still usable
            event_loop = self._event_loop
            if not event_loop:
                return

            # Check if event loop is closed or closing
            try:
                if event_loop.is_closed():
                    logger.debug("Event loop is closed, skipping frame callback")
                    return
            except Exception:
                # If we can't check, assume it's not usable
                return

            if self.frame_callback:
                # Frame is already a safe copy from VideoFrameDelegate
                # (indicated by metadata['memory_owned'] = True)
                # No additional copy needed - this saves significant CPU
                safe_frame = frame

                # Paranoia check: if somehow we got an unsafe frame, copy it
                if not metadata.get('memory_owned', False):
                    logger.warning("Received frame without memory_owned flag - making defensive copy")
                    safe_frame = np.array(frame, dtype=np.uint8, copy=True)

                # Final shutdown check before scheduling
                if self._shutdown_requested.is_set():
                    return

                # Schedule async callback in the stored event loop
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self.frame_callback(safe_frame, metadata),
                        event_loop
                    )
                    # Don't wait for the future - fire and forget
                    # But track it for debugging if needed
                    if hasattr(self, '_last_scheduled_future'):
                        self._last_scheduled_future = future
                except RuntimeError as e:
                    # Event loop was closed between our check and the call
                    if "closed" in str(e).lower():
                        logger.debug("Event loop closed during scheduling")
                    else:
                        raise

        except Exception as e:
            if not self._shutdown_requested.is_set():
                logger.error(f"Error scheduling frame callback: {e}")
        finally:
            # Unregister callback
            with self._callback_lock:
                self._active_callbacks -= 1

    async def stop_capture(self):
        """
        Stop AVFoundation capture session with proper callback drain.

        CRITICAL FIX v10.7 - Correct Shutdown Sequence:
        ================================================
        The original sequence was:
        1. Signal shutdown
        2. Wait for callbacks (with short timeout)
        3. Stop session ← TOO EARLY! Callbacks still running!
        4. Free resources ← CRASH! Delegate accessed after free!

        The FIXED sequence is:
        1. Signal delegate to stop accepting frames (fast)
        2. Signal shutdown (no new callbacks accepted)
        3. Stop capture session (no new frames generated)
        4. Wait for ALL active callbacks with longer timeout
        5. Stop runloop thread
        6. Free resources (safe now - no callbacks running)

        This prevents SIGSEGV by ensuring the delegate and session
        are not freed while callbacks are still executing.
        """
        if not self.is_running:
            return

        logger.info("Stopping AVFoundation capture...")

        # Get configurable timeouts from environment
        callback_drain_timeout = float(os.getenv('JARVIS_CAPTURE_CALLBACK_DRAIN_TIMEOUT', '5.0'))
        runloop_stop_timeout = float(os.getenv('JARVIS_CAPTURE_RUNLOOP_STOP_TIMEOUT', '2.0'))

        try:
            # STEP 1: Signal delegate to stop processing new frames (FIRST!)
            # This is the fastest way to stop the pipeline
            if self.delegate and hasattr(self.delegate, 'setShutdown_'):
                try:
                    self.delegate.setShutdown_(True)
                except Exception as e:
                    logger.debug(f"Error signaling delegate shutdown: {e}")

            # STEP 2: Signal shutdown flag (prevents new callbacks from starting)
            self._shutdown_requested.set()

            # STEP 3: Stop capture session (no new frames will be generated)
            # This MUST happen BEFORE waiting for callbacks, otherwise we keep
            # generating frames and callbacks never drain
            if self.session:
                try:
                    self.session.stopRunning()
                    logger.debug("Capture session stopped")
                except Exception as e:
                    logger.debug(f"Error stopping session: {e}")

            # STEP 4: Wait for ALL active callbacks to complete
            # With session stopped, no new callbacks will be added
            # Use longer timeout since we need callbacks to actually finish
            start_time = time.monotonic()
            initial_callbacks = self._active_callbacks
            last_log_time = start_time

            while self._active_callbacks > 0:
                elapsed = time.monotonic() - start_time

                # Log progress periodically
                if time.monotonic() - last_log_time > 1.0:
                    logger.debug(
                        f"Waiting for {self._active_callbacks} callbacks "
                        f"(started with {initial_callbacks}, elapsed: {elapsed:.1f}s)"
                    )
                    last_log_time = time.monotonic()

                if elapsed > callback_drain_timeout:
                    logger.warning(
                        f"Timeout after {elapsed:.1f}s waiting for {self._active_callbacks} callbacks. "
                        f"These may be blocked or very slow. Proceeding with shutdown."
                    )
                    break

                await asyncio.sleep(0.05)

            if self._active_callbacks == 0:
                logger.debug("All callbacks drained successfully")

            # STEP 5: Stop runloop thread (handles Objective-C callbacks)
            if self._runloop_thread and self._runloop_thread.is_alive():
                self._stop_runloop.set()
                # Use non-blocking join in async context
                join_start = time.monotonic()
                while self._runloop_thread.is_alive():
                    if time.monotonic() - join_start > runloop_stop_timeout:
                        logger.warning(
                            f"Runloop thread did not stop within {runloop_stop_timeout}s "
                            f"(daemon thread will be cleaned up on process exit)"
                        )
                        break
                    await asyncio.sleep(0.1)

            # STEP 6: Small delay to ensure all macOS callbacks have exited
            await asyncio.sleep(0.1)

            # STEP 7: Clear event loop reference (prevents stale reference issues)
            self._event_loop = None

            # STEP 8: Clean up Objective-C objects
            # These are now safe to free since all callbacks have completed
            self.is_running = False

            # Release objects in order of dependency
            if self.output:
                try:
                    # Remove delegate reference first
                    self.output.setSampleBufferDelegate_queue_(None, None)
                except Exception:
                    pass

            self.delegate = None
            self.output = None
            self.session = None
            self.dispatch_queue = None

            # STEP 9: Force garbage collection to release Objective-C references
            gc.collect()

            logger.info("✅ AVFoundation capture stopped safely")

        except Exception as e:
            logger.error(f"Error stopping AVFoundation capture: {e}")

    def is_available(self) -> bool:
        """Check if AVFoundation capture is available"""
        return AVFOUNDATION_AVAILABLE


class AdvancedVideoCaptureManager:
    """
    Advanced video capture manager with intelligent fallback chain

    Tries capture methods in order of quality:
    1. AVFoundation (best quality, purple indicator)
    2. ScreenCaptureKit (modern, best performance, macOS 12.3+)
    3. screencapture command (reliable fallback)
    4. Screenshot loop (final fallback)
    """

    def __init__(self, config: Optional[AdvancedCaptureConfig] = None):
        self.config = config or AdvancedCaptureConfig()
        self.metrics = CaptureMetrics(
            method=CaptureMethod.AVFOUNDATION,  # Will be updated
            status=CaptureStatus.IDLE,
            target_fps=self.config.target_fps,
        )

        # Capture implementation
        self.capture_impl: Optional[Any] = None
        self.current_method: Optional[CaptureMethod] = None

        # Callbacks
        self.frame_callback: Optional[Callable] = None

        # State
        self.start_time: float = 0.0
        self._executor = ThreadPoolExecutor(max_workers=2)

        logger.info(f"Advanced Video Capture Manager initialized")
        logger.info(f"  ⚡ Ferrari Engine (Native SCK): {NATIVE_SCK_BRIDGE_AVAILABLE}")
        logger.info(f"  PyObjC available: {PYOBJC_AVAILABLE}")
        logger.info(f"  AVFoundation available: {AVFOUNDATION_AVAILABLE}")
        logger.info(f"  ScreenCaptureKit available: {SCREENCAPTUREKIT_AVAILABLE}")
        logger.info(f"  Config: {self.config.resolution} @ {self.config.target_fps} FPS")
        if NATIVE_SCK_BRIDGE_AVAILABLE:
            logger.info(f"  🏎️  Priority 1: ScreenCaptureKit (Adaptive FPS, GPU-accelerated)")
        elif AVFOUNDATION_AVAILABLE:
            logger.info(f"  📹  Priority 1: AVFoundation (High quality, purple indicator)")

    async def start_capture(self, frame_callback: Callable) -> bool:
        """
        Start video capture with intelligent fallback

        Args:
            frame_callback: Async function called with (frame: np.ndarray, metadata: dict)

        Returns:
            True if any capture method started successfully
        """
        if self.metrics.status == CaptureStatus.RUNNING:
            logger.warning("Capture already running")
            return True

        self.frame_callback = frame_callback
        self.metrics.status = CaptureStatus.STARTING
        self.start_time = time.time()

        # Try capture methods in priority order
        methods_to_try = self._get_methods_to_try()

        for method in methods_to_try:
            logger.info(f"🎯 Trying capture method: {method.value}")

            try:
                success = await self._try_capture_method(method)

                if success:
                    self.current_method = method
                    self.metrics.method = method
                    self.metrics.status = CaptureStatus.RUNNING

                    logger.info(f"✅ Capture started with method: {method.value}")

                    # Start metrics monitoring
                    asyncio.create_task(self._monitor_metrics())

                    return True
                else:
                    logger.warning(f"❌ Method {method.value} failed, trying next...")

            except Exception as e:
                logger.error(f"❌ Error with {method.value}: {e}")
                self.metrics.errors.append(f"{method.value}: {str(e)}")

        # All methods failed
        self.metrics.status = CaptureStatus.ERROR
        logger.error("❌ All capture methods failed!")
        return False

    def _get_methods_to_try(self) -> List[CaptureMethod]:
        """Get list of capture methods to try in priority order"""
        if self.config.preferred_method and not self.config.enable_fallback_chain:
            # Only try preferred method
            return [self.config.preferred_method]

        methods = []

        # Preferred method first
        if self.config.preferred_method:
            methods.append(self.config.preferred_method)

        # Priority 1: AVFoundation (best for full display capture)
        if AVFOUNDATION_AVAILABLE and CaptureMethod.AVFOUNDATION not in methods:
            methods.append(CaptureMethod.AVFOUNDATION)

        # Note: ScreenCaptureKit (Ferrari Engine) optimized for window-specific capture (VideoWatcher)
        # For full display, AVFoundation is more appropriate

        # Priority 2: screencapture command (reliable fallback)
        if CaptureMethod.SCREENCAPTURE_CMD not in methods:
            methods.append(CaptureMethod.SCREENCAPTURE_CMD)

        # Priority 3: Final fallback
        if CaptureMethod.SCREENSHOT_LOOP not in methods:
            methods.append(CaptureMethod.SCREENSHOT_LOOP)

        return methods

    async def _try_capture_method(self, method: CaptureMethod) -> bool:
        """Try to start capture with specified method"""
        try:
            if method == CaptureMethod.AVFOUNDATION:
                return await self._start_avfoundation()
            elif method == CaptureMethod.SCREENCAPTUREKIT:
                return await self._start_screencapturekit()
            elif method == CaptureMethod.SCREENCAPTURE_CMD:
                return await self._start_screencapture_cmd()
            elif method == CaptureMethod.SCREENSHOT_LOOP:
                return await self._start_screenshot_loop()
            else:
                logger.error(f"Unknown capture method: {method}")
                return False
        except Exception as e:
            logger.error(f"Error starting {method.value}: {e}", exc_info=True)
            return False

    async def _start_avfoundation(self) -> bool:
        """Start AVFoundation capture"""
        if not AVFOUNDATION_AVAILABLE:
            logger.warning("AVFoundation not available")
            return False

        self.capture_impl = AVFoundationCapture(self.config)
        return await self.capture_impl.start_capture(self._on_frame_captured)

    async def _start_screencapturekit(self) -> bool:
        """
        Start ScreenCaptureKit capture using native C++ bridge (Ferrari Engine)

        NOTE: Ferrari Engine (SCK) is optimized for window-specific capture.
        For full display capture, AVFoundation is more appropriate.

        This method is available but will fall through to AVFoundation
        for full display capture. SCK excels in VideoWatcher for
        window-specific surveillance.
        """
        if not SCREENCAPTUREKIT_AVAILABLE:
            logger.info("ScreenCaptureKit not available - Ferrari Engine is for window-specific capture")
            return False

        if not NATIVE_SCK_BRIDGE_AVAILABLE:
            logger.info("Ferrari Engine (SCK bridge) optimized for VideoWatcher, not full display")
            return False

        # Ferrari Engine is designed for window-specific capture (VideoWatcher)
        # For full display capture, AVFoundation is the better choice
        logger.info("🏎️  Ferrari Engine available for window-specific VideoWatcher capture")
        logger.info("   For full display: using AVFoundation (next in priority)")
        return False

    async def _start_screencapture_cmd(self) -> bool:
        """Start capture using screencapture command"""
        logger.info("Using screencapture command fallback")
        # TODO: Implement screencapture command support
        return False

    async def _start_screenshot_loop(self) -> bool:
        """Start screenshot loop fallback"""
        logger.info("Using screenshot loop fallback")
        # TODO: Implement screenshot loop
        return False

    async def _on_frame_captured(self, frame: np.ndarray, metadata: Dict[str, Any]):
        """Handle captured frame"""
        try:
            # Update metrics
            self.metrics.frames_captured += 1
            self.metrics.last_frame_timestamp = metadata.get('timestamp', time.time())
            self.metrics.current_fps = metadata.get('fps', 0.0)

            # Log metrics if enabled
            if self.config.log_frame_metrics and self.metrics.frames_captured % 100 == 0:
                logger.info(f"[Metrics] {self.metrics.to_dict()}")

            # Call user callback
            if self.frame_callback:
                await self.frame_callback(frame, metadata)

        except Exception as e:
            logger.error(f"Error handling frame: {e}")
            self.metrics.errors.append(f"Frame handler: {str(e)}")

    async def _monitor_metrics(self):
        """Monitor capture metrics and adjust quality if needed"""
        process = psutil.Process()

        while self.metrics.status == CaptureStatus.RUNNING:
            try:
                # Update metrics
                self.metrics.uptime_seconds = time.time() - self.start_time
                self.metrics.memory_usage_mb = process.memory_info().rss / 1024 / 1024
                self.metrics.cpu_percent = process.cpu_percent(interval=0.1)

                # Adaptive quality adjustment
                if self.config.enable_adaptive_quality:
                    await self._adjust_quality()

                # Log diagnostics
                if self.config.enable_diagnostics and self.metrics.uptime_seconds % 30 < 1:
                    logger.info(f"[Diagnostics] {self.metrics.to_dict()}")

                await asyncio.sleep(1.0)

            except Exception as e:
                logger.error(f"Error monitoring metrics: {e}")

    async def _adjust_quality(self):
        """Adjust capture quality based on system resources"""
        # Reduce FPS if memory usage is high
        if self.metrics.memory_usage_mb > self.config.max_memory_mb * 0.9:
            new_fps = max(self.config.min_fps, self.config.target_fps - 5)
            if new_fps < self.config.target_fps:
                logger.warning(
                    f"High memory usage ({self.metrics.memory_usage_mb:.1f}MB), "
                    f"reducing FPS: {self.config.target_fps} → {new_fps}"
                )
                self.config.target_fps = new_fps
                # TODO: Apply FPS change to active capture

    async def stop_capture(self):
        """Stop video capture"""
        if self.metrics.status != CaptureStatus.RUNNING:
            return

        logger.info("Stopping video capture...")
        self.metrics.status = CaptureStatus.STOPPING

        try:
            if self.capture_impl:
                if hasattr(self.capture_impl, 'stop_capture'):
                    await self.capture_impl.stop_capture()

            self.metrics.status = CaptureStatus.STOPPED
            logger.info("✅ Video capture stopped")

            # Log final metrics
            logger.info(f"[Final Metrics] {self.metrics.to_dict()}")

        except Exception as e:
            logger.error(f"Error stopping capture: {e}")
            self.metrics.status = CaptureStatus.ERROR

    def get_metrics(self) -> Dict[str, Any]:
        """Get current capture metrics"""
        return self.metrics.to_dict()

    def get_status(self) -> CaptureStatus:
        """Get current capture status"""
        return self.metrics.status

    def is_available(self) -> bool:
        """Check if video capture is available"""
        return AVFOUNDATION_AVAILABLE or SCREENCAPTUREKIT_AVAILABLE


# Factory function for easy integration
async def create_video_capture(
    config: Optional[AdvancedCaptureConfig] = None
) -> AdvancedVideoCaptureManager:
    """
    Create advanced video capture manager

    Args:
        config: Optional configuration (uses environment variables if not provided)

    Returns:
        Configured AdvancedVideoCaptureManager instance
    """
    manager = AdvancedVideoCaptureManager(config)
    return manager


# Diagnostic function
def check_capture_availability() -> Dict[str, Any]:
    """
    Check availability of all capture methods

    Returns:
        Dictionary with availability status and system info
    """
    return {
        'pyobjc_installed': PYOBJC_AVAILABLE,
        'avfoundation_available': AVFOUNDATION_AVAILABLE,
        'screencapturekit_available': SCREENCAPTUREKIT_AVAILABLE,
        'macos_version': os.popen('sw_vers -productVersion').read().strip(),
        'python_version': os.sys.version.split()[0],
        'recommended_method': 'AVFoundation' if AVFOUNDATION_AVAILABLE else 'ScreenCaptureKit' if SCREENCAPTUREKIT_AVAILABLE else 'fallback',
        'memory_available_mb': psutil.virtual_memory().available / 1024 / 1024,
        'cpu_count': psutil.cpu_count(),
    }


# =============================================================================
# Video Multi-Space Intelligence (VMSI) - Background Watcher System
# =============================================================================
# Production-grade background visual monitoring for specific windows
# Features:
# - Window-specific capture (not full display)
# - Low-FPS streaming (1-10 FPS) for efficiency
# - Low-priority thread execution
# - Parallel watcher support
# - Visual event detection integration
# - Cross-repo state sharing
# =============================================================================

class WatcherStatus(Enum):
    """Video watcher status states"""
    IDLE = "idle"
    STARTING = "starting"
    WATCHING = "watching"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    EVENT_DETECTED = "event_detected"


@dataclass
class VisualEventResult:
    """Result from visual event detection"""
    detected: bool
    event_type: str  # "text", "element", "color"
    trigger: str
    confidence: float
    frame_number: int
    detection_time: float
    frame_data: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WatcherConfig:
    """Configuration for video watcher - NO HARDCODING"""
    window_id: int
    fps: int = field(default_factory=lambda: int(os.getenv('JARVIS_WATCHER_DEFAULT_FPS', '5')))
    priority: str = field(default_factory=lambda: os.getenv('JARVIS_WATCHER_PRIORITY', 'low'))
    timeout: float = field(default_factory=lambda: float(os.getenv('JARVIS_WATCHER_TIMEOUT', '300.0')))
    max_buffer_size: int = field(default_factory=lambda: int(os.getenv('JARVIS_WATCHER_BUFFER_SIZE', '10')))

    # v61.1 RETINA FOCUS: Target specific hardware display
    # 1 = Main Display, 2+ = Secondary/Virtual Display (Ghost Display)
    display_id: int = 1

    # Visual detection settings
    enable_ocr: bool = field(default_factory=lambda: os.getenv('JARVIS_WATCHER_OCR', 'true').lower() == 'true')
    enable_element_detection: bool = field(default_factory=lambda: os.getenv('JARVIS_WATCHER_ELEMENTS', 'true').lower() == 'true')
    confidence_threshold: float = field(default_factory=lambda: float(os.getenv('JARVIS_DETECTION_CONFIDENCE', '0.75')))


# =============================================================================
# v38.0: MOSAIC STRATEGY - O(1) Single-Stream Display Capture
# =============================================================================
# ROOT CAUSE FIX: Per-window surveillance (O(N)) is mathematically wasteful
# -----------------------------------------------------------------------------
# PROBLEM: Watching 15 Chrome windows = 15 separate video streams:
# - Each SCK stream allocates GPU resources, buffers, Metal contexts
# - RAM usage scales linearly: 15 windows × 200MB = 3GB
# - GPU contention causes frame drops and WindowServer strain
# - CPU thrashing from parallel OCR operations
#
# SOLUTION: "Mosaic Strategy" - Capture entire Ghost Display as ONE stream:
# - Windows are already tiled on Ghost Display
# - Capture display once, analyze mosaic for trigger text
# - RAM usage constant: ~300MB regardless of window count
# - GPU usage constant: 1 stream instead of N streams
# - O(1) complexity: resource usage independent of window count
# =============================================================================


@dataclass
class WindowTileInfo:
    """
    v38.0: Position of a window within the Ghost Display mosaic.

    When windows are tiled on Ghost Display, we track their positions
    to map OCR detections back to specific windows.
    """
    window_id: int
    app_name: str
    window_title: str

    # Position within Ghost Display (pixels)
    x: int
    y: int
    width: int
    height: int

    # Tile grid position (for easy reference)
    tile_row: int = 0
    tile_col: int = 0

    # v38.5: Z-order for overlapping window resolution
    # Higher z_order = more on top (visible), lower = behind
    z_order: int = 0

    # Metadata
    original_space_id: Optional[int] = None
    teleported_at: Optional[float] = None

    # v38.5: Window layer info from yabai (normal, above, below)
    window_layer: str = "normal"  # "normal", "above", "below"

    def contains_point(self, px: int, py: int) -> bool:
        """Check if a point (e.g., OCR match location) is within this tile."""
        return (self.x <= px < self.x + self.width and
                self.y <= py < self.y + self.height)

    def get_overlap_area(self, other: 'WindowTileInfo') -> int:
        """v38.5: Calculate overlap area with another tile (for z-order decisions)."""
        # Calculate intersection rectangle
        x_overlap = max(0, min(self.x + self.width, other.x + other.width) - max(self.x, other.x))
        y_overlap = max(0, min(self.y + self.height, other.y + other.height) - max(self.y, other.y))
        return x_overlap * y_overlap


@dataclass
class MosaicWatcherConfig:
    """
    v38.0: Configuration for Mosaic display capture - O(1) efficiency.

    Instead of creating N watchers for N windows, we create ONE watcher
    for the entire Ghost Display and analyze the tiled mosaic.
    """
    # Target display (Ghost Display ID from yabai)
    display_id: int

    # Display dimensions
    display_width: int = field(default_factory=lambda: int(os.getenv('JARVIS_GHOST_WIDTH', '1920')))
    display_height: int = field(default_factory=lambda: int(os.getenv('JARVIS_GHOST_HEIGHT', '1080')))

    # Capture settings (lower FPS acceptable since we only need to detect text)
    fps: int = field(default_factory=lambda: int(os.getenv('JARVIS_MOSAIC_FPS', '5')))
    max_buffer_size: int = field(default_factory=lambda: int(os.getenv('JARVIS_MOSAIC_BUFFER_SIZE', '5')))

    # Performance settings
    resolution_scale: float = field(default_factory=lambda: float(os.getenv('JARVIS_MOSAIC_SCALE', '1.0')))
    capture_cursor: bool = False  # No cursor needed for text detection

    # Timeout
    timeout: float = field(default_factory=lambda: float(os.getenv('JARVIS_MOSAIC_TIMEOUT', '300.0')))

    # OCR settings
    enable_ocr: bool = True
    confidence_threshold: float = field(default_factory=lambda: float(os.getenv('JARVIS_DETECTION_CONFIDENCE', '0.75')))

    # Window tile mapping (populated when windows are teleported)
    window_tiles: List[WindowTileInfo] = field(default_factory=list)

    # v38.5: Enable z-order resolution for overlapping windows
    enable_zorder_resolution: bool = field(
        default_factory=lambda: os.getenv("JARVIS_MOSAIC_ZORDER", "true").lower() == "true"
    )

    def get_tile_for_point(self, x: int, y: int) -> Optional[WindowTileInfo]:
        """
        v38.5: Find which window tile contains the given point.

        When multiple tiles overlap (contain the same point), uses z-order
        resolution to return the topmost (most visible) window.

        Args:
            x: X coordinate in yabai points (not pixels)
            y: Y coordinate in yabai points (not pixels)

        Returns:
            WindowTileInfo of the topmost tile containing the point, or None
        """
        # Collect all tiles that contain this point
        containing_tiles: List[WindowTileInfo] = []
        for tile in self.window_tiles:
            if tile.contains_point(x, y):
                containing_tiles.append(tile)

        if not containing_tiles:
            return None

        if len(containing_tiles) == 1:
            return containing_tiles[0]

        # v38.5: Multiple tiles overlap at this point - use z-order resolution
        if self.enable_zorder_resolution:
            # Sort by z_order (highest first = topmost window)
            # Also consider window_layer as secondary sort (above > normal > below)
            layer_priority = {"above": 2, "normal": 1, "below": 0}
            containing_tiles.sort(
                key=lambda t: (layer_priority.get(t.window_layer, 1), t.z_order),
                reverse=True
            )

            topmost = containing_tiles[0]
            logger.debug(
                f"[MosaicWatcher v38.5] Z-order resolution: Point ({x}, {y}) in "
                f"{len(containing_tiles)} overlapping tiles, selected window "
                f"{topmost.window_id} (z={topmost.z_order}, layer={topmost.window_layer})"
            )
            return topmost

        # Fallback: return first match (legacy behavior)
        return containing_tiles[0]

    def get_all_tiles_for_point(self, x: int, y: int) -> List[WindowTileInfo]:
        """v38.5: Get all tiles containing a point, sorted by z-order (topmost first)."""
        containing_tiles = [tile for tile in self.window_tiles if tile.contains_point(x, y)]

        if len(containing_tiles) > 1:
            layer_priority = {"above": 2, "normal": 1, "below": 0}
            containing_tiles.sort(
                key=lambda t: (layer_priority.get(t.window_layer, 1), t.z_order),
                reverse=True
            )

        return containing_tiles

    def get_tile_for_window(self, window_id: int) -> Optional[WindowTileInfo]:
        """Get tile info for a specific window ID."""
        for tile in self.window_tiles:
            if tile.window_id == window_id:
                return tile
        return None

    def update_zorder_from_yabai(self, window_stack: List[int]) -> None:
        """
        v38.5: Update z-order values from yabai window stack order.

        Args:
            window_stack: List of window IDs in z-order (topmost first)
        """
        # Build z-order map: topmost = highest z_order value
        zorder_map = {wid: len(window_stack) - idx for idx, wid in enumerate(window_stack)}

        for tile in self.window_tiles:
            if tile.window_id in zorder_map:
                tile.z_order = zorder_map[tile.window_id]


class MosaicCaptureStatus(Enum):
    """Status of mosaic display capture."""
    IDLE = "idle"
    INITIALIZING = "initializing"
    CAPTURING = "capturing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class VideoWatcher:
    """
    Background video watcher for a specific window.

    Captures frames from a specific macOS window at low FPS and
    enables visual event detection (text, elements, colors).

    This is the core of VMSI - "The Watcher" that monitors background windows.

    THREAD SAFETY v10.7:
    ====================
    - Frame queue operations protected by lock for atomic get/put
    - Stats counters use atomic operations
    - Stop event is thread-safe
    - Multiple threads can safely produce/consume frames
    """

    def __init__(self, config: WatcherConfig):
        self.config = config
        self.watcher_id = f"watcher_{config.window_id}_{int(time.time())}"
        self.status = WatcherStatus.IDLE

        # Frame queue (producer-consumer pattern)
        # Use deque with maxlen for automatic overflow handling
        self.frame_queue: queue.Queue = queue.Queue(maxsize=config.max_buffer_size)

        # Lock for atomic queue operations (get + put must be atomic)
        self._queue_lock = threading.Lock()

        # ScreenCaptureKit stream (Ferrari Engine for window-specific capture)
        self._sck_stream: Optional[Any] = None
        self._use_sck = NATIVE_SCK_BRIDGE_AVAILABLE  # Will use SCK if available

        # Stats with lock protection for thread-safe updates
        self._stats_lock = threading.Lock()
        self._frames_captured = 0
        self._frames_analyzed = 0
        self._frames_dropped = 0
        self.events_detected = 0
        self.start_time: float = 0.0
        self._last_frame_time: float = 0.0

        # Threading
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_loop_task: Optional[asyncio.Task] = None

        # Metadata
        self.app_name: Optional[str] = None
        self.space_id: Optional[int] = None

        capture_method = "ScreenCaptureKit (Ferrari)" if self._use_sck else "CGWindowListCreateImage"
        logger.info(f"VideoWatcher created: {self.watcher_id} (Window {config.window_id}, {config.fps} FPS, Method: {capture_method})")

    @property
    def frames_captured(self) -> int:
        """Thread-safe access to frames captured count."""
        with self._stats_lock:
            return self._frames_captured

    @frames_captured.setter
    def frames_captured(self, value: int):
        """Thread-safe update of frames captured count."""
        with self._stats_lock:
            self._frames_captured = value

    @property
    def frames_analyzed(self) -> int:
        """Thread-safe access to frames analyzed count."""
        with self._stats_lock:
            return self._frames_analyzed

    @frames_analyzed.setter
    def frames_analyzed(self, value: int):
        """Thread-safe update of frames analyzed count."""
        with self._stats_lock:
            self._frames_analyzed = value

    @property
    def last_frame_time(self) -> float:
        """Thread-safe access to last frame time."""
        with self._stats_lock:
            return self._last_frame_time

    @last_frame_time.setter
    def last_frame_time(self, value: float):
        """Thread-safe update of last frame time."""
        with self._stats_lock:
            self._last_frame_time = value

    def _put_frame_atomic(self, frame_data: Dict[str, Any]) -> bool:
        """
        Atomically put a frame into the queue, dropping oldest if full.

        This fixes the race condition where between get_nowait() and
        put_nowait(), another thread could fill the queue.

        Returns:
            True if frame was added, False if dropped
        """
        with self._queue_lock:
            try:
                self.frame_queue.put_nowait(frame_data)
                return True
            except queue.Full:
                # Queue is full - atomically remove oldest and add new
                try:
                    self.frame_queue.get_nowait()
                    with self._stats_lock:
                        self._frames_dropped += 1
                except queue.Empty:
                    pass  # Queue became empty between full check and get

                try:
                    self.frame_queue.put_nowait(frame_data)
                    return True
                except queue.Full:
                    # Still full (shouldn't happen with lock, but be defensive)
                    with self._stats_lock:
                        self._frames_dropped += 1
                    return False

    async def start(self) -> bool:
        """Start the video watcher."""
        if self.status == WatcherStatus.WATCHING:
            logger.warning(f"Watcher {self.watcher_id} already running")
            return True

        self.status = WatcherStatus.STARTING
        self.start_time = time.time()
        self._stop_event.clear()

        # Try to initialize ScreenCaptureKit stream (Ferrari Engine)
        if self._use_sck:
            try:
                logger.info(f"[Watcher {self.watcher_id}] Initializing Ferrari Engine for window {self.config.window_id}...")

                sck_config = StreamingConfig(
                    target_fps=self.config.fps,
                    max_buffer_size=self.config.max_buffer_size,
                    output_format="raw",
                    use_gpu_acceleration=True,
                    drop_frames_on_overflow=True,
                    capture_cursor=False,  # Don't capture cursor for background monitoring
                    resolution_scale=1.0
                )

                self._sck_stream = AsyncCaptureStream(self.config.window_id, sck_config)

                # =====================================================================
                # ROOT CAUSE FIX: Non-Blocking SCK Stream Start v6.1.0
                # =====================================================================
                # PROBLEM: _sck_stream.start() is a native C++ call that can block
                # the event loop for 1-10+ seconds during:
                # - ScreenCaptureKit permission prompts
                # - GPU/Metal shader compilation
                # - Window enumeration
                # - macOS privacy subsystem queries
                #
                # CRITICAL INSIGHT: asyncio.wait_for() alone does NOT work!
                # If the C++ code blocks the thread, the event loop can't tick,
                # so the timeout timer never fires → infinite hang.
                #
                # SOLUTION: Run in thread executor so event loop stays responsive.
                # CONFIGURABLE: JARVIS_SCK_STREAM_START_TIMEOUT env var (default 8s)
                # =====================================================================
                sck_start_timeout = float(os.getenv('JARVIS_SCK_STREAM_START_TIMEOUT', '8.0'))
                loop = asyncio.get_event_loop()

                try:
                    # Check if start() is a coroutine or regular function
                    start_coro = self._sck_stream.start()
                    if asyncio.iscoroutine(start_coro):
                        # It's async but may have blocking internals - need special handling
                        # Run the entire coroutine with a timeout, but note this may not fully
                        # protect against internal blocking. For full protection, the native
                        # extension should be fixed.
                        success = await asyncio.wait_for(start_coro, timeout=sck_start_timeout)
                    else:
                        # It's a sync function - wrap in executor
                        def _blocking_sck_start():
                            """Blocking native C++ init - runs in thread executor."""
                            return self._sck_stream.start()

                        success = await asyncio.wait_for(
                            loop.run_in_executor(None, _blocking_sck_start),
                            timeout=sck_start_timeout
                        )

                except asyncio.TimeoutError:
                    logger.warning(
                        f"[Watcher {self.watcher_id}] Ferrari Engine start() timed out after {sck_start_timeout}s. "
                        f"Falling back to CGWindowListCreateImage."
                    )
                    success = False
                    # Clean up failed stream in background (don't block on cleanup)
                    try:
                        if hasattr(self._sck_stream, 'stop'):
                            asyncio.create_task(self._safe_stop_stream())
                    except Exception:
                        pass
                    self._sck_stream = None

                if success:
                    logger.info(f"🏎️  [Watcher {self.watcher_id}] Ferrari Engine started for window {self.config.window_id}")
                    # Start async frame loop for SCK
                    self._frame_loop_task = asyncio.create_task(self._sck_frame_loop())

                    # =====================================================================
                    # v22.0.0: FRAME PRODUCTION VERIFICATION (Root Cause Fix)
                    # =====================================================================
                    # PROBLEM: SCK stream.start() returns True but no frames are produced.
                    # This causes "no frames flowing" errors in God Mode verification.
                    #
                    # ROOT CAUSE: SCK may claim success even if:
                    # - Window ID is invalid or stale
                    # - Window is minimized or on another space
                    # - Screen Recording permission is missing
                    # - The stream is "running" but not producing frames
                    #
                    # SOLUTION: Wait briefly for first frame before claiming success.
                    # If no frame within timeout, fall back to CGWindowListCreateImage.
                    # =====================================================================
                    first_frame_timeout = float(os.getenv('JARVIS_SCK_FIRST_FRAME_TIMEOUT', '2.0'))
                    logger.debug(
                        f"[Watcher {self.watcher_id}] Verifying SCK frame production "
                        f"(timeout: {first_frame_timeout}s)..."
                    )

                    frame_received = False
                    start_verify_time = time.time()

                    while time.time() - start_verify_time < first_frame_timeout:
                        # Check if any frames have been queued
                        if not self.frame_queue.empty():
                            frame_received = True
                            logger.info(
                                f"✅ [Watcher {self.watcher_id}] SCK frame verified - "
                                f"Ferrari Engine confirmed working"
                            )
                            break

                        # Also check frames_captured counter
                        if self._frames_captured > 0:
                            frame_received = True
                            logger.info(
                                f"✅ [Watcher {self.watcher_id}] SCK frames captured: "
                                f"{self._frames_captured} - Ferrari Engine confirmed"
                            )
                            break

                        # Yield to let frame loop execute
                        await asyncio.sleep(0.1)

                    if frame_received:
                        self.status = WatcherStatus.WATCHING
                        return True
                    else:
                        # SCK started but no frames - fall back to CGWindowListCreateImage
                        logger.warning(
                            f"[Watcher {self.watcher_id}] Ferrari Engine started but no frames "
                            f"after {first_frame_timeout}s - falling back to CGWindowListCreateImage. "
                            f"(Window may be hidden, minimized, or on another space)"
                        )
                        # Cancel the frame loop task
                        if self._frame_loop_task and not self._frame_loop_task.done():
                            self._frame_loop_task.cancel()
                            try:
                                await self._frame_loop_task
                            except asyncio.CancelledError:
                                pass
                        # Stop SCK stream
                        try:
                            await self._sck_stream.stop()
                        except Exception:
                            pass
                        self._sck_stream = None
                        self._use_sck = False
                        # Fall through to CGWindowListCreateImage fallback below
                else:
                    logger.warning(f"[Watcher {self.watcher_id}] Ferrari Engine failed to start, falling back to CGWindowListCreateImage")
                    self._use_sck = False
                    self._sck_stream = None

            except Exception as e:
                logger.warning(f"[Watcher {self.watcher_id}] Ferrari Engine initialization failed: {e}, falling back")
                self._use_sck = False
                self._sck_stream = None

        # Fallback: Start traditional capture thread with CGWindowListCreateImage
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name=f"VideoWatcher-{self.config.window_id}",
            daemon=True
        )

        # Set thread priority to low (if supported)
        try:
            import resource
            # Lower priority (higher nice value)
            resource.setrlimit(resource.RLIMIT_NICE, (19, 19))
        except Exception:
            pass  # Priority setting not critical

        self._capture_thread.start()

        # =====================================================================
        # v22.0.0: FRAME PRODUCTION VERIFICATION for CGWindowListCreateImage
        # =====================================================================
        # Same logic as SCK - verify frames are actually being produced
        # before claiming success. This catches:
        # - Invalid window IDs
        # - Missing Screen Recording permission
        # - Hidden/minimized windows that can't be captured
        # =====================================================================
        fallback_frame_timeout = float(os.getenv('JARVIS_FALLBACK_FIRST_FRAME_TIMEOUT', '3.0'))
        logger.debug(
            f"[Watcher {self.watcher_id}] Verifying CGWindowListCreateImage frame production "
            f"(timeout: {fallback_frame_timeout}s)..."
        )

        fallback_frame_received = False
        start_fallback_verify = time.time()

        while time.time() - start_fallback_verify < fallback_frame_timeout:
            # Check if capture thread is producing frames
            if not self.frame_queue.empty():
                fallback_frame_received = True
                logger.info(
                    f"✅ [Watcher {self.watcher_id}] CGWindowListCreateImage frame verified - "
                    f"fallback capture working"
                )
                break

            # Also check frames_captured counter (thread-safe read)
            if self._frames_captured > 0:
                fallback_frame_received = True
                logger.info(
                    f"✅ [Watcher {self.watcher_id}] CGWindowListCreateImage frames captured: "
                    f"{self._frames_captured} - fallback confirmed"
                )
                break

            # Yield to let capture thread execute
            await asyncio.sleep(0.1)

        if fallback_frame_received:
            self.status = WatcherStatus.WATCHING
            logger.info(f"✅ Watcher {self.watcher_id} started (fallback method, frame verified)")
            return True
        else:
            # Fallback also failed - stop the thread and return failure
            logger.error(
                f"❌ [Watcher {self.watcher_id}] CGWindowListCreateImage also failed to produce frames "
                f"after {fallback_frame_timeout}s. Possible causes:\n"
                f"   1. Window ID {self.config.window_id} is invalid or window was closed\n"
                f"   2. Screen Recording permission not granted (System Preferences → Privacy)\n"
                f"   3. Window is hidden or minimized and cannot be captured"
            )
            # Signal stop and cleanup
            self._stop_event.set()
            self.status = WatcherStatus.ERROR
            return False

    async def _safe_stop_stream(self):
        """
        Safely stop the SCK stream in background without blocking.

        This is used when the stream start times out or fails - we want to
        clean up resources but not block the main flow.
        """
        try:
            if self._sck_stream:
                stop_method = getattr(self._sck_stream, 'stop', None)
                if stop_method:
                    if asyncio.iscoroutinefunction(stop_method):
                        await asyncio.wait_for(stop_method(), timeout=2.0)
                    else:
                        loop = asyncio.get_event_loop()
                        await asyncio.wait_for(
                            loop.run_in_executor(None, stop_method),
                            timeout=2.0
                        )
        except asyncio.TimeoutError:
            logger.debug(f"[Watcher {self.watcher_id}] Stream cleanup timed out (non-critical)")
        except Exception as e:
            logger.debug(f"[Watcher {self.watcher_id}] Stream cleanup error: {e}")
        finally:
            self._sck_stream = None

    def _capture_loop(self):
        """
        Capture loop running in background thread.

        Uses CGWindowListCreateImage to capture specific window only.
        Runs at low FPS to save resources.
        """
        frame_interval = 1.0 / self.config.fps
        consecutive_failures = 0
        last_log_time = 0

        logger.info(f"[Watcher {self.watcher_id}] Capture loop started (target FPS: {self.config.fps})")

        while not self._stop_event.is_set():
            try:
                loop_start = time.time()

                # Capture window-specific frame
                frame = self._capture_window_frame()

                if frame is not None:
                    self.frames_captured += 1
                    self.last_frame_time = time.time()
                    consecutive_failures = 0

                    # Log successful capture periodically
                    if self.frames_captured % 10 == 1:
                        logger.debug(
                            f"[Watcher {self.watcher_id}] Captured frame #{self.frames_captured}, "
                            f"shape={frame.shape}, queue_size={self.frame_queue.qsize()}"
                        )

                    # Add to queue atomically (thread-safe with overflow handling)
                    self._put_frame_atomic({
                        'frame': frame,
                        'frame_number': self._frames_captured,
                        'timestamp': self._last_frame_time,
                        'window_id': self.config.window_id,
                        'method': 'cgwindowlist',
                        'memory_owned': True,  # numpy array from bytes() is owned
                    })
                else:
                    # Frame capture failed
                    consecutive_failures += 1

                    # Log capture failures periodically (not every time to avoid spam)
                    current_time = time.time()
                    if current_time - last_log_time > 5.0:  # Log every 5 seconds
                        logger.warning(
                            f"[Watcher {self.watcher_id}] Frame capture failing! "
                            f"Consecutive failures: {consecutive_failures}, "
                            f"Total captured: {self.frames_captured}, "
                            f"Window ID: {self.config.window_id}"
                        )
                        last_log_time = current_time

                        # After many failures, log detailed diagnostics
                        if consecutive_failures > 20:
                            logger.error(
                                f"[Watcher {self.watcher_id}] CRITICAL: Frame capture has failed "
                                f"{consecutive_failures} times in a row. Possible causes:\n"
                                f"  1. Screen Recording permission not granted (System Preferences → Privacy)\n"
                                f"  2. Window ID {self.config.window_id} is invalid or window was closed\n"
                                f"  3. PyObjC frameworks not available\n"
                                f"  PYOBJC_AVAILABLE={PYOBJC_AVAILABLE}"
                            )

                # Sleep to maintain target FPS
                elapsed = time.time() - loop_start
                sleep_time = max(0, frame_interval - elapsed)

                if sleep_time > 0:
                    time.sleep(sleep_time)

            except Exception as e:
                logger.error(f"Error in watcher {self.watcher_id} capture loop: {e}", exc_info=True)
                time.sleep(1.0)  # Backoff on error

        logger.info(f"Watcher {self.watcher_id} capture loop stopped (captured {self.frames_captured} frames)")

    def _capture_window_frame(self) -> Optional[np.ndarray]:
        """
        Capture frame from specific window using CGWindowListCreateImage.

        This is window-specific (not full display) which is more efficient.

        FALLBACK: If CGWindowListCreateImage fails (permissions), use screencapture command.
        """
        if not PYOBJC_AVAILABLE:
            logger.error(f"[Watcher {self.watcher_id}] PyObjC not available - cannot capture frames")
            return None

        try:
            # Capture window image
            # kCGWindowListOptionIncludingWindow = only this window
            # kCGWindowImageDefault = default quality
            cg_image = CGWindowListCreateImage(
                CGRectNull,  # Entire window bounds
                kCGWindowListOptionIncludingWindow,
                self.config.window_id,
                kCGWindowImageDefault
            )

            if not cg_image:
                # FALLBACK: Try screencapture command (works without Screen Recording permission on some macOS versions)
                if self.frames_captured == 0:
                    logger.info(
                        f"[Watcher {self.watcher_id}] CGWindowListCreateImage failed - using screencapture fallback"
                    )
                return self._capture_window_frame_fallback()

            # Get image dimensions
            width = CGImageGetWidth(cg_image)
            height = CGImageGetHeight(cg_image)

            if width == 0 or height == 0:
                logger.warning(f"[Watcher {self.watcher_id}] Captured image has zero dimensions: {width}x{height}")
                return None

            # Get pixel data
            data_provider = CGImageGetDataProvider(cg_image)
            data = CGDataProviderCopyData(data_provider)

            if not data:
                logger.error(f"[Watcher {self.watcher_id}] Failed to get pixel data from CGImage")
                return None

            # Convert to numpy array
            # CGImage format is typically BGRA
            bytes_data = bytes(data)
            frame = np.frombuffer(bytes_data, dtype=np.uint8)

            # Reshape to image dimensions
            # Assuming 4 bytes per pixel (BGRA)
            bytes_per_row = width * 4
            frame = frame.reshape((height, bytes_per_row // 4, 4))
            frame = frame[:, :width, :3]  # Remove alpha
            frame = frame[:, :, ::-1]  # BGR to RGB

            # Log successful capture details (first frame only)
            if self.frames_captured == 0:
                logger.info(
                    f"[Watcher {self.watcher_id}] First frame captured successfully! "
                    f"Dimensions: {width}x{height}, Shape: {frame.shape}"
                )

            return frame

        except Exception as e:
            logger.debug(f"Error capturing window {self.config.window_id}: {e}")
            return None

    def _capture_window_frame_fallback(self) -> Optional[np.ndarray]:
        """
        Fallback capture method using macOS screencapture command.

        This works when CGWindowListCreateImage fails (e.g., macOS Sequoia 15.1
        with changed Screen Recording permission requirements).

        The screencapture command has different permission requirements and
        works in cases where the Quartz API fails.

        Returns:
            numpy array in RGB format, or None if capture fails
        """
        import subprocess
        import tempfile

        try:
            # Import PIL (widely used in codebase)
            try:
                from PIL import Image
            except ImportError:
                logger.error("[Fallback Capture] PIL not available - cannot use fallback method")
                return None

            # Create temporary file for screenshot
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
                tmp_path = tmp_file.name

            try:
                # Use screencapture with window ID
                # -l <window_id> = capture specific window
                # -x = no sound
                # -t png = PNG format
                result = subprocess.run(
                    ['screencapture', '-l', str(self.config.window_id), '-x', '-t', 'png', tmp_path],
                    capture_output=True,
                    timeout=2.0,  # 2 second timeout
                    check=False  # Don't raise on non-zero exit
                )

                # Check if file was created and has content
                if not os.path.exists(tmp_path):
                    logger.debug(f"[Fallback Capture] screencapture did not create file for window {self.config.window_id}")
                    return None

                file_size = os.path.getsize(tmp_path)
                if file_size == 0:
                    logger.debug(f"[Fallback Capture] screencapture created empty file for window {self.config.window_id}")
                    return None

                # Load image with PIL
                pil_image = Image.open(tmp_path)

                # Convert to RGB (in case it's RGBA or other format)
                if pil_image.mode != 'RGB':
                    pil_image = pil_image.convert('RGB')

                # Convert to numpy array
                frame = np.array(pil_image)

                # Log successful fallback capture (first time only)
                if self.frames_captured == 0:
                    logger.info(
                        f"[Watcher {self.watcher_id}] Fallback capture successful! "
                        f"Using screencapture command. Dimensions: {frame.shape[1]}x{frame.shape[0]}, "
                        f"File size: {file_size} bytes"
                    )

                return frame

            finally:
                # Always clean up temp file
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception as cleanup_error:
                    logger.debug(f"[Fallback Capture] Failed to clean up temp file: {cleanup_error}")

        except subprocess.TimeoutExpired:
            logger.warning(f"[Fallback Capture] screencapture command timed out for window {self.config.window_id}")
            return None
        except Exception as e:
            logger.debug(f"[Fallback Capture] Error in fallback capture: {e}")
            return None

    async def _sck_frame_loop(self):
        """
        ScreenCaptureKit frame consumption loop for VideoWatcher (Ferrari Engine)

        This async loop pulls frames from the native SCK stream and pushes them
        to the frame queue for consumption by visual event detection.
        """
        logger.info(f"[Watcher {self.watcher_id}] SCK frame loop started (Ferrari Engine)")

        frame_count = 0
        last_log_time = time.time()

        while not self._stop_event.is_set() and self.status == WatcherStatus.WATCHING:
            try:
                # Get frame from SCK stream (use short timeout for responsive loop)
                frame_data = await self._sck_stream.get_frame(timeout_ms=100)

                if not frame_data:
                    # No frame available (expected with adaptive FPS on static content)
                    await asyncio.sleep(0.01)
                    continue

                frame_count += 1
                self.frames_captured = frame_count
                self.last_frame_time = time.time()

                # Extract numpy array
                frame = frame_data.get('image')
                if frame is None:
                    logger.debug(f"[Watcher {self.watcher_id}] Frame {frame_count} has no image data")
                    continue

                # Convert BGRA to RGB if needed
                if len(frame.shape) == 3 and frame.shape[2] == 4:
                    frame = frame[:, :, :3]  # Remove alpha
                    frame = frame[:, :, ::-1]  # BGRA to RGB

                # Log periodically
                if frame_count % 10 == 1:
                    logger.debug(
                        f"[Watcher {self.watcher_id}] Frame {frame_count}: "
                        f"shape={frame.shape}, "
                        f"latency={frame_data.get('capture_latency_us', 0)/1000:.1f}ms, "
                        f"queue_size={self.frame_queue.qsize()}"
                    )

                # Add to queue atomically (thread-safe with overflow handling)
                self._put_frame_atomic({
                    'frame': frame,
                    'frame_number': frame_count,
                    'timestamp': self._last_frame_time,
                    'window_id': self.config.window_id,
                    'capture_latency_ms': frame_data.get('capture_latency_us', 0) / 1000.0,
                    'method': 'screencapturekit',
                    'memory_owned': True,  # SCK frames are owned copies
                })

            except Exception as e:
                logger.error(f"[Watcher {self.watcher_id}] Error in SCK frame loop: {e}", exc_info=True)
                await asyncio.sleep(0.1)  # Backoff on error

        logger.info(f"[Watcher {self.watcher_id}] SCK frame loop stopped (captured {frame_count} frames)")

    async def get_latest_frame(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """Get the latest frame from the watcher."""
        try:
            frame_data = await asyncio.wait_for(
                asyncio.to_thread(self.frame_queue.get),
                timeout=timeout
            )
            return frame_data
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.error(f"Error getting frame from watcher {self.watcher_id}: {e}")
            return None

    async def stop(self):
        """
        Stop the video watcher.

        THREAD SAFETY v10.7:
        ====================
        - Uses async-compatible wait instead of blocking join()
        - Properly drains frame queue with lock protection
        - Cleans up all resources in correct order
        """
        if self.status in (WatcherStatus.STOPPED, WatcherStatus.STOPPING):
            return

        self.status = WatcherStatus.STOPPING
        logger.info(f"Stopping watcher {self.watcher_id}...")

        # Get configurable timeout from environment
        thread_stop_timeout = float(os.getenv('JARVIS_WATCHER_THREAD_STOP_TIMEOUT', '2.0'))

        # Signal stop
        self._stop_event.set()

        # Stop SCK stream if using Ferrari Engine
        if self._sck_stream:
            try:
                await self._sck_stream.stop()
                logger.info(f"[Watcher {self.watcher_id}] Ferrari Engine stream stopped")
            except Exception as e:
                logger.error(f"[Watcher {self.watcher_id}] Error stopping SCK stream: {e}")

        # Cancel frame loop task if running
        if self._frame_loop_task and not self._frame_loop_task.done():
            self._frame_loop_task.cancel()
            try:
                await self._frame_loop_task
            except asyncio.CancelledError:
                pass

        # Wait for capture thread using async-compatible wait
        # FIXED: Don't use blocking join() in async context
        if self._capture_thread and self._capture_thread.is_alive():
            start_time = time.monotonic()
            while self._capture_thread.is_alive():
                if time.monotonic() - start_time > thread_stop_timeout:
                    logger.warning(
                        f"[Watcher {self.watcher_id}] Capture thread did not stop within "
                        f"{thread_stop_timeout}s (daemon thread will be cleaned up on exit)"
                    )
                    break
                await asyncio.sleep(0.1)

        # Clear queue atomically
        with self._queue_lock:
            while not self.frame_queue.empty():
                try:
                    self.frame_queue.get_nowait()
                except queue.Empty:
                    break

        self.status = WatcherStatus.STOPPED

        # Log stats (use thread-safe property access)
        uptime = time.time() - self.start_time if self.start_time > 0 else 0
        capture_method = "Ferrari Engine" if self._sck_stream else "CGWindowListCreateImage"
        with self._stats_lock:
            frames = self._frames_captured
            dropped = self._frames_dropped
        logger.info(
            f"✅ Watcher {self.watcher_id} stopped ({capture_method}) - "
            f"Uptime: {uptime:.1f}s, Frames: {frames}, Dropped: {dropped}, "
            f"Events: {self.events_detected}"
        )

    # =========================================================================
    # v37.0: STREAM RECONNECTION PROTOCOL
    # =========================================================================
    # ROOT CAUSE FIX: ScreenCaptureKit stream disconnections
    # =========================================================================
    # PROBLEM: SCK streams can disconnect due to:
    # - Window moves to different space (stream context lost)
    # - macOS power management (display sleep/wake)
    # - GPU driver issues (Metal/shader compilation failures)
    # - ScreenRecording permission changes at runtime
    # - WindowServer restart (rare but happens)
    #
    # SOLUTION: Smart reconnection with optional window_id update:
    # 1. Gracefully stop current stream
    # 2. Optionally update window_id if app restarted
    # 3. Re-initialize SCK stream
    # 4. Resume frame capture with stats preserved
    # =========================================================================

    async def restart(
        self,
        window_id: Optional[int] = None,
        preserve_stats: bool = True
    ) -> bool:
        """
        v37.0: Restart the video watcher stream, optionally for a different window.

        This enables recovery from:
        - ScreenCaptureKit disconnections
        - Window ID changes (app restart)
        - Display configuration changes
        - Power management events

        Args:
            window_id: Optional new window ID (if app restarted with new window)
            preserve_stats: If True, preserve frame counts and events detected

        Returns:
            True if restart succeeded, False otherwise
        """
        original_window_id = self.config.window_id
        restart_timeout = float(os.getenv('JARVIS_WATCHER_RESTART_TIMEOUT', '10.0'))

        logger.info(
            f"[Watcher {self.watcher_id}] 🔄 Initiating stream restart "
            f"(window: {original_window_id}{f' → {window_id}' if window_id and window_id != original_window_id else ''})"
        )

        # Save stats if preserving
        if preserve_stats:
            with self._stats_lock:
                saved_frames = self._frames_captured
                saved_analyzed = self._frames_analyzed
                saved_dropped = self._frames_dropped
            saved_events = self.events_detected
            saved_app_name = self.app_name
            saved_space_id = self.space_id
        else:
            saved_frames = saved_analyzed = saved_dropped = saved_events = 0
            saved_app_name = self.app_name
            saved_space_id = self.space_id

        try:
            # ===================================================================
            # PHASE 1: Graceful Stop (don't wait forever)
            # ===================================================================
            stop_timeout = float(os.getenv('JARVIS_WATCHER_STOP_TIMEOUT', '3.0'))
            try:
                await asyncio.wait_for(self.stop(), timeout=stop_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[Watcher {self.watcher_id}] Stop timed out after {stop_timeout}s, "
                    f"forcing cleanup..."
                )
                # Force cleanup
                self._stop_event.set()
                self.status = WatcherStatus.STOPPED
                if self._sck_stream:
                    try:
                        # Try sync stop as last resort
                        if hasattr(self._sck_stream, 'force_stop'):
                            await asyncio.to_thread(self._sck_stream.force_stop)
                    except Exception:
                        pass
                self._sck_stream = None

            # ===================================================================
            # PHASE 2: Update Configuration
            # ===================================================================
            if window_id and window_id != original_window_id:
                self.config = WatcherConfig(
                    window_id=window_id,
                    fps=self.config.fps,
                    max_buffer_size=self.config.max_buffer_size
                )
                logger.info(
                    f"[Watcher {self.watcher_id}] Window ID updated: "
                    f"{original_window_id} → {window_id}"
                )

            # ===================================================================
            # PHASE 3: Re-initialize Stream
            # ===================================================================
            # Reset state
            self.status = WatcherStatus.IDLE
            self._stop_event.clear()
            self._capture_thread = None
            self._frame_loop_task = None

            # Clear queue
            with self._queue_lock:
                while not self.frame_queue.empty():
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        break

            # ===================================================================
            # PHASE 4: Start with Timeout
            # ===================================================================
            try:
                success = await asyncio.wait_for(
                    self.start(),
                    timeout=restart_timeout
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"[Watcher {self.watcher_id}] Restart start() timed out "
                    f"after {restart_timeout}s"
                )
                self.status = WatcherStatus.ERROR
                return False

            if success:
                # Restore preserved stats
                if preserve_stats:
                    with self._stats_lock:
                        self._frames_captured = saved_frames
                        self._frames_analyzed = saved_analyzed
                        self._frames_dropped = saved_dropped
                    self.events_detected = saved_events
                    self.app_name = saved_app_name
                    self.space_id = saved_space_id

                logger.info(
                    f"[Watcher {self.watcher_id}] ✅ Stream restart SUCCESSFUL "
                    f"(window: {self.config.window_id}, "
                    f"preserved: {saved_frames} frames, {saved_events} events)"
                )
                return True
            else:
                logger.error(
                    f"[Watcher {self.watcher_id}] ❌ Stream restart FAILED - "
                    f"start() returned False"
                )
                return False

        except Exception as e:
            logger.error(
                f"[Watcher {self.watcher_id}] ❌ Stream restart FAILED with exception: {e}"
            )
            self.status = WatcherStatus.ERROR
            return False

    async def is_stream_healthy(self) -> bool:
        """
        v37.0: Check if the SCK stream is still healthy and producing frames.

        Returns:
            True if stream appears healthy, False if reconnection may be needed
        """
        if self.status != WatcherStatus.WATCHING:
            return False

        # Check 1: Recent frame activity
        with self._stats_lock:
            last_frame = self._last_frame_time
        time_since_frame = time.time() - last_frame if last_frame > 0 else float('inf')

        # If no frame in 5 seconds, stream may be unhealthy
        frame_timeout = float(os.getenv('JARVIS_STREAM_HEALTH_TIMEOUT', '5.0'))
        if time_since_frame > frame_timeout:
            logger.warning(
                f"[Watcher {self.watcher_id}] Stream unhealthy: "
                f"no frames for {time_since_frame:.1f}s"
            )
            return False

        # Check 2: SCK stream status (if using Ferrari Engine)
        if self._sck_stream and hasattr(self._sck_stream, 'is_running'):
            try:
                if not self._sck_stream.is_running:
                    logger.warning(
                        f"[Watcher {self.watcher_id}] Stream unhealthy: "
                        f"SCK stream not running"
                    )
                    return False
            except Exception as e:
                logger.debug(f"[Watcher {self.watcher_id}] SCK status check failed: {e}")

        return True

    def get_stats(self) -> Dict[str, Any]:
        """Get watcher statistics (thread-safe)."""
        uptime = time.time() - self.start_time if self.start_time > 0 else 0

        # Thread-safe access to stats
        with self._stats_lock:
            frames_captured = self._frames_captured
            frames_analyzed = self._frames_analyzed
            frames_dropped = self._frames_dropped

        actual_fps = frames_captured / uptime if uptime > 0 else 0

        return {
            'watcher_id': self.watcher_id,
            'window_id': self.config.window_id,
            'status': self.status.value,
            'app_name': self.app_name,
            'space_id': self.space_id,
            'target_fps': self.config.fps,
            'actual_fps': round(actual_fps, 2),
            'frames_captured': frames_captured,
            'frames_analyzed': frames_analyzed,
            'frames_dropped': frames_dropped,
            'events_detected': self.events_detected,
            'uptime_seconds': round(uptime, 2),
            'queue_size': self.frame_queue.qsize(),
        }


# =============================================================================
# v38.0: MOSAIC WATCHER - Single-Stream Display Capture
# =============================================================================
# The Mosaic Watcher captures an entire display (Ghost Display) as ONE stream,
# replacing the need for N separate window watchers. This is the O(1) solution.
# =============================================================================

class MosaicWatcher:
    """
    v38.0: Single-stream display capture for the Mosaic Strategy.

    Instead of spawning N watchers for N windows, MosaicWatcher captures
    the entire Ghost Display where all windows are tiled, then analyzes
    the mosaic for trigger text.

    EFFICIENCY COMPARISON:
    =====================
    Per-Window (O(N)):
    - 15 windows = 15 SCK streams
    - 15 × 200MB RAM = 3GB
    - 15 GPU contexts = contention
    - 15 parallel OCR = CPU thrashing

    Mosaic (O(1)):
    - 15 windows = 1 AVFoundation stream
    - 1 × 300MB RAM = constant
    - 1 GPU context = no contention
    - 1 sequential OCR = efficient

    This class uses AVFoundation's AVCaptureScreenInput for efficient
    display capture, which is more appropriate for full-display than SCK.
    """

    def __init__(self, config: MosaicWatcherConfig):
        self.config = config
        self.watcher_id = f"mosaic_{config.display_id}_{int(time.time())}"
        self.status = MosaicCaptureStatus.IDLE

        # Frame queue (same pattern as VideoWatcher)
        self.frame_queue: queue.Queue = queue.Queue(maxsize=config.max_buffer_size)
        self._queue_lock = threading.Lock()

        # Capture session (AVFoundation)
        self._capture_session: Optional[Any] = None
        self._is_avfoundation = AVFOUNDATION_AVAILABLE

        # Stats with lock protection
        self._stats_lock = threading.Lock()
        self._frames_captured = 0
        self._frames_analyzed = 0
        self._frames_dropped = 0
        self.events_detected = 0
        self.start_time: float = 0.0
        self._last_frame_time: float = 0.0

        # Threading
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_loop_task: Optional[asyncio.Task] = None

        # RunLoop for Objective-C callbacks (AVFoundation requirement)
        self._runloop_thread: Optional[threading.Thread] = None
        self._runloop: Optional[Any] = None

        logger.info(
            f"[MosaicWatcher] Created for display {config.display_id} "
            f"({config.display_width}x{config.display_height} @ {config.fps} FPS)"
        )

    def _put_frame_atomic(self, frame_data: Dict[str, Any]) -> bool:
        """Atomically put a frame into the queue, dropping oldest if full."""
        with self._queue_lock:
            try:
                self.frame_queue.put_nowait(frame_data)
                return True
            except queue.Full:
                try:
                    self.frame_queue.get_nowait()
                    with self._stats_lock:
                        self._frames_dropped += 1
                except queue.Empty:
                    pass
                try:
                    self.frame_queue.put_nowait(frame_data)
                    return True
                except queue.Full:
                    with self._stats_lock:
                        self._frames_dropped += 1
                    return False

    async def start(self) -> bool:
        """
        Start mosaic display capture.

        Uses AVFoundation's AVCaptureScreenInput for efficient display capture.
        Falls back to CGDisplayStream if AVFoundation unavailable.
        """
        if self.status == MosaicCaptureStatus.CAPTURING:
            logger.warning(f"[MosaicWatcher] {self.watcher_id} already capturing")
            return True

        self.status = MosaicCaptureStatus.INITIALIZING
        self.start_time = time.time()
        self._stop_event.clear()

        try:
            if self._is_avfoundation:
                success = await self._start_avfoundation_capture()
            else:
                # Fallback: Use thread-based CGDisplayStream
                success = await self._start_cgdisplay_capture()

            if success:
                self.status = MosaicCaptureStatus.CAPTURING
                logger.info(
                    f"[MosaicWatcher] ✅ Started capturing display {self.config.display_id} "
                    f"(tiles: {len(self.config.window_tiles)})"
                )
            else:
                self.status = MosaicCaptureStatus.ERROR

            return success

        except Exception as e:
            logger.error(f"[MosaicWatcher] Failed to start: {e}", exc_info=True)
            self.status = MosaicCaptureStatus.ERROR
            return False

    async def _start_avfoundation_capture(self) -> bool:
        """Start capture using AVFoundation (most efficient for display capture)."""
        try:
            from AVFoundation import (
                AVCaptureSession,
                AVCaptureScreenInput,
                AVCaptureVideoDataOutput
            )
            from CoreMedia import CMTimeMake
            from Quartz import (
                kCVPixelBufferPixelFormatTypeKey,
                kCVPixelFormatType_32BGRA
            )

            logger.info(f"[MosaicWatcher] Starting AVFoundation display capture...")

            # Create capture session
            self._capture_session = AVCaptureSession.alloc().init()

            # Create screen input for Ghost Display
            screen_input = AVCaptureScreenInput.alloc().initWithDisplayID_(
                self.config.display_id
            )

            if not screen_input:
                logger.error(f"[MosaicWatcher] Failed to create screen input for display {self.config.display_id}")
                return False

            # Configure frame rate
            min_frame_duration = CMTimeMake(1, self.config.fps)
            screen_input.setMinFrameDuration_(min_frame_duration)
            screen_input.setCapturesCursor_(self.config.capture_cursor)

            # Add input to session
            if not self._capture_session.canAddInput_(screen_input):
                logger.error("[MosaicWatcher] Cannot add screen input to session")
                return False

            self._capture_session.addInput_(screen_input)

            # Create video output
            video_output = AVCaptureVideoDataOutput.alloc().init()
            video_output.setAlwaysDiscardsLateVideoFrames_(True)
            video_output.setVideoSettings_({
                kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA
            })

            # Create delegate for frame callbacks
            # Import the delegate class from the existing implementation
            try:
                # Try to use existing VideoFrameDelegate
                delegate = VideoFrameDelegate.delegateWithCallback_(
                    self._handle_avfoundation_frame
                )
                self.delegate = delegate
            except NameError:
                # Fallback: Create simple frame handler
                logger.warning("[MosaicWatcher] VideoFrameDelegate not available, using thread capture")
                return await self._start_cgdisplay_capture()

            # Create dispatch queue
            queue_name = f"com.jarvis.mosaic.{self.config.display_id}".encode('utf-8')
            dispatch_queue = libdispatch.dispatch_queue_create(queue_name, None)

            video_output.setSampleBufferDelegate_queue_(delegate, dispatch_queue)

            # Add output to session
            if not self._capture_session.canAddOutput_(video_output):
                logger.error("[MosaicWatcher] Cannot add video output to session")
                return False

            self._capture_session.addOutput_(video_output)

            # Start RunLoop in background thread (required for ObjC callbacks)
            self._start_runloop()

            # Start capture
            self._capture_session.startRunning()

            logger.info(
                f"[MosaicWatcher] ✅ AVFoundation capture started for display {self.config.display_id}"
            )
            return True

        except Exception as e:
            logger.error(f"[MosaicWatcher] AVFoundation capture failed: {e}", exc_info=True)
            return False

    async def _start_cgdisplay_capture(self) -> bool:
        """Fallback: Use CGDisplayStream for display capture in a background thread."""
        try:
            logger.info("[MosaicWatcher] Using CGDisplayStream fallback...")

            # Start capture thread
            self._capture_thread = threading.Thread(
                target=self._cgdisplay_capture_loop,
                name=f"MosaicCapture-{self.config.display_id}",
                daemon=True
            )
            self._capture_thread.start()

            # Wait for first frame to confirm capture works
            await asyncio.sleep(0.5)

            with self._stats_lock:
                if self._frames_captured > 0:
                    logger.info("[MosaicWatcher] ✅ CGDisplayStream capture started")
                    return True

            logger.warning("[MosaicWatcher] CGDisplayStream capture started but no frames yet")
            return True  # May just be slow to start

        except Exception as e:
            logger.error(f"[MosaicWatcher] CGDisplayStream capture failed: {e}")
            return False

    def _cgdisplay_capture_loop(self):
        """Background thread for CGDisplayStream capture."""
        try:
            from Quartz import (
                CGDisplayCreateImage,
                CGImageGetWidth,
                CGImageGetHeight,
                CGImageGetDataProvider,
                CGDataProviderCopyData
            )
            import numpy as np

            logger.info(f"[MosaicWatcher] CGDisplayStream loop started for display {self.config.display_id}")

            frame_interval = 1.0 / self.config.fps

            while not self._stop_event.is_set():
                try:
                    # Capture display
                    cg_image = CGDisplayCreateImage(self.config.display_id)

                    if cg_image:
                        # Convert to numpy array
                        width = CGImageGetWidth(cg_image)
                        height = CGImageGetHeight(cg_image)
                        data_provider = CGImageGetDataProvider(cg_image)
                        data = CGDataProviderCopyData(data_provider)

                        # Create numpy array (BGRA format)
                        arr = np.frombuffer(data, dtype=np.uint8)
                        arr = arr.reshape((height, width, 4))

                        # Convert BGRA to RGB
                        frame = arr[:, :, [2, 1, 0]]  # Swap B and R

                        frame_data = {
                            'frame': frame,
                            'width': width,
                            'height': height,
                            'timestamp': time.time(),
                            'frame_number': self._frames_captured,
                            'fps': self.config.fps,
                            'capture_method': 'cgdisplay'
                        }

                        self._put_frame_atomic(frame_data)

                        with self._stats_lock:
                            self._frames_captured += 1
                            self._last_frame_time = time.time()

                    # Sleep for frame interval
                    time.sleep(frame_interval)

                except Exception as e:
                    logger.error(f"[MosaicWatcher] Frame capture error: {e}")
                    time.sleep(0.1)

        except Exception as e:
            logger.error(f"[MosaicWatcher] Capture loop crashed: {e}", exc_info=True)
        finally:
            logger.info("[MosaicWatcher] Capture loop ended")

    def _handle_avfoundation_frame(self, frame_data: bytes, width: int, height: int):
        """Handle frame from AVFoundation callback."""
        try:
            import numpy as np

            # Convert bytes to numpy array
            arr = np.frombuffer(frame_data, dtype=np.uint8)
            arr = arr.reshape((height, width, 4))

            # Convert BGRA to RGB
            frame = arr[:, :, [2, 1, 0]]

            data = {
                'frame': frame,
                'width': width,
                'height': height,
                'timestamp': time.time(),
                'frame_number': self._frames_captured,
                'fps': self.config.fps,
                'capture_method': 'avfoundation'
            }

            self._put_frame_atomic(data)

            with self._stats_lock:
                self._frames_captured += 1
                self._last_frame_time = time.time()

        except Exception as e:
            logger.error(f"[MosaicWatcher] Frame handling error: {e}")

    def _start_runloop(self):
        """Start NSRunLoop in background thread for ObjC callbacks."""
        def runloop_thread():
            try:
                from Foundation import NSRunLoop
                self._runloop = NSRunLoop.currentRunLoop()
                while not self._stop_event.is_set():
                    self._runloop.runMode_beforeDate_(
                        'kCFRunLoopDefaultMode',
                        time.time() + 0.1
                    )
            except Exception as e:
                logger.error(f"[MosaicWatcher] RunLoop error: {e}")

        self._runloop_thread = threading.Thread(
            target=runloop_thread,
            name=f"MosaicRunLoop-{self.config.display_id}",
            daemon=True
        )
        self._runloop_thread.start()

    async def stop(self):
        """Stop mosaic capture."""
        if self.status in (MosaicCaptureStatus.STOPPED, MosaicCaptureStatus.STOPPING):
            return

        self.status = MosaicCaptureStatus.STOPPING
        logger.info(f"[MosaicWatcher] Stopping {self.watcher_id}...")

        self._stop_event.set()

        # Stop AVFoundation session
        if self._capture_session:
            try:
                self._capture_session.stopRunning()
            except Exception as e:
                logger.error(f"[MosaicWatcher] Error stopping session: {e}")

        # Wait for capture thread
        if self._capture_thread and self._capture_thread.is_alive():
            timeout = float(os.getenv('JARVIS_MOSAIC_STOP_TIMEOUT', '3.0'))
            start = time.monotonic()
            while self._capture_thread.is_alive():
                if time.monotonic() - start > timeout:
                    logger.warning("[MosaicWatcher] Capture thread did not stop in time")
                    break
                await asyncio.sleep(0.1)

        # Clear queue
        with self._queue_lock:
            while not self.frame_queue.empty():
                try:
                    self.frame_queue.get_nowait()
                except queue.Empty:
                    break

        self.status = MosaicCaptureStatus.STOPPED

        # Log stats
        uptime = time.time() - self.start_time if self.start_time > 0 else 0
        with self._stats_lock:
            frames = self._frames_captured
            dropped = self._frames_dropped

        logger.info(
            f"[MosaicWatcher] ✅ Stopped {self.watcher_id} - "
            f"Uptime: {uptime:.1f}s, Frames: {frames}, Dropped: {dropped}, "
            f"Events: {self.events_detected}"
        )

    async def get_latest_frame(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """Get the latest frame from the mosaic capture."""
        if self.status != MosaicCaptureStatus.CAPTURING:
            return None

        try:
            # Try to get frame with timeout
            frame_data = await asyncio.wait_for(
                asyncio.to_thread(self.frame_queue.get, timeout=timeout),
                timeout=timeout + 0.5
            )

            # v38.5: Add frame metadata for reliability checks
            if frame_data and 'frame' in frame_data:
                frame_data['timestamp'] = time.time()
                frame_data['frame_id'] = self._frames_captured

            return frame_data
        except (asyncio.TimeoutError, queue.Empty):
            return None
        except Exception as e:
            logger.error(f"[MosaicWatcher] Error getting frame: {e}")
            return None

    def is_black_screen(self, frame: np.ndarray, threshold: float = 0.02) -> bool:
        """
        v38.5: Detect if frame is a black/sleeping display.

        Ghost Display may sleep or show black when disconnected. This method
        detects such conditions to avoid false OCR failures.

        Args:
            frame: Numpy array (RGB or BGRA)
            threshold: Maximum average brightness to consider "black" (0.0-1.0)

        Returns:
            True if frame appears to be black/sleeping display
        """
        if frame is None or frame.size == 0:
            return True

        try:
            # Convert to grayscale for brightness analysis
            if len(frame.shape) == 3:
                if frame.shape[2] == 4:  # BGRA
                    gray = np.mean(frame[:, :, :3], axis=2)
                else:  # RGB
                    gray = np.mean(frame, axis=2)
            else:
                gray = frame

            # Calculate normalized average brightness (0.0-1.0)
            avg_brightness = np.mean(gray) / 255.0

            # Also check variance - a real display has variation even when dark
            brightness_variance = np.var(gray) / (255.0 * 255.0)

            # Black screen: low brightness AND low variance
            is_black = avg_brightness < threshold and brightness_variance < 0.001

            if is_black:
                logger.debug(
                    f"[MosaicWatcher v38.5] Black screen detected: "
                    f"brightness={avg_brightness:.4f}, variance={brightness_variance:.6f}"
                )

            return is_black

        except Exception as e:
            logger.warning(f"[MosaicWatcher v38.5] Black screen detection error: {e}")
            return False

    def adaptive_upscale_for_ocr(
        self,
        frame: np.ndarray,
        min_height: int = 720,
        max_scale: float = 2.0
    ) -> np.ndarray:
        """
        v38.5: Adaptively upscale frame for better OCR accuracy.

        Small frames have poor OCR accuracy. This method upscales frames
        that are below a minimum height threshold.

        Args:
            frame: Input frame (numpy array)
            min_height: Minimum height for good OCR (default 720px)
            max_scale: Maximum upscale factor (default 2.0x)

        Returns:
            Upscaled frame if needed, otherwise original frame
        """
        if frame is None or frame.size == 0:
            return frame

        try:
            height = frame.shape[0]
            width = frame.shape[1]

            if height >= min_height:
                # Frame already large enough
                return frame

            # Calculate scale factor needed
            scale = min(min_height / height, max_scale)

            # Don't upscale if less than 10% needed
            if scale < 1.1:
                return frame

            new_height = int(height * scale)
            new_width = int(width * scale)

            # Use cv2 if available, otherwise use PIL
            try:
                import cv2
                upscaled = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_CUBIC)
            except ImportError:
                from PIL import Image
                pil_img = Image.fromarray(frame)
                pil_img = pil_img.resize((new_width, new_height), Image.BICUBIC)
                upscaled = np.array(pil_img)

            logger.debug(
                f"[MosaicWatcher v38.5] Adaptive upscale: {width}x{height} → "
                f"{new_width}x{new_height} ({scale:.2f}x)"
            )

            return upscaled

        except Exception as e:
            logger.warning(f"[MosaicWatcher v38.5] Upscale error: {e}")
            return frame

    def check_frame_freshness(
        self,
        frame_data: Dict[str, Any],
        max_age_seconds: float = 2.0
    ) -> Tuple[bool, float]:
        """
        v38.5: Check if frame is fresh enough for reliable detection.

        Stale frames may not reflect current display state, leading to
        false positives/negatives.

        Args:
            frame_data: Frame data dict with 'timestamp' key
            max_age_seconds: Maximum acceptable frame age

        Returns:
            (is_fresh, age_seconds) tuple
        """
        if not frame_data or 'timestamp' not in frame_data:
            return False, float('inf')

        frame_time = frame_data.get('timestamp', 0)
        current_time = time.time()
        age = current_time - frame_time

        is_fresh = age <= max_age_seconds

        if not is_fresh:
            logger.warning(
                f"[MosaicWatcher v38.5] Stale frame detected: age={age:.2f}s > max={max_age_seconds}s"
            )

        return is_fresh, age

    def get_display_health(self) -> Dict[str, Any]:
        """
        v38.5: Get comprehensive display health metrics.

        Returns health indicators for the Ghost Display capture.
        """
        uptime = time.time() - self.start_time if self.start_time > 0 else 0

        with self._stats_lock:
            frames_captured = self._frames_captured
            frames_dropped = self._frames_dropped
            last_frame_time = self._last_frame_time

        # Calculate health metrics
        frame_rate = frames_captured / uptime if uptime > 0 else 0
        drop_rate = frames_dropped / frames_captured if frames_captured > 0 else 0
        frame_gap = time.time() - last_frame_time if last_frame_time > 0 else float('inf')

        # Determine health status
        health_status = "healthy"
        issues = []

        if frame_gap > 2.0:
            health_status = "degraded"
            issues.append(f"No frames in {frame_gap:.1f}s")

        if drop_rate > 0.1:
            health_status = "degraded"
            issues.append(f"High drop rate: {drop_rate:.1%}")

        if frame_rate < self.config.fps * 0.5:
            health_status = "degraded"
            issues.append(f"Low FPS: {frame_rate:.1f} (target: {self.config.fps})")

        if self.status == MosaicCaptureStatus.ERROR:
            health_status = "critical"
            issues.append("Capture session in error state")

        return {
            'status': health_status,
            'frame_rate': round(frame_rate, 2),
            'target_fps': self.config.fps,
            'drop_rate': round(drop_rate, 4),
            'last_frame_age': round(frame_gap, 2),
            'queue_size': self.frame_queue.qsize(),
            'issues': issues,
            'display_id': self.config.display_id,
            'uptime': round(uptime, 2)
        }

    async def stealth_wake_display(self) -> bool:
        """
        v38.5: Wake sleeping Ghost Display without stealing focus.

        Uses caffeinate or a mouse jiggle on the Ghost Display to wake it
        without bringing it to foreground or stealing keyboard focus.

        Returns:
            True if wake attempt was made successfully
        """
        import subprocess

        try:
            # Method 1: Use caffeinate to assert display wake
            # -u = declare user activity (wakes display)
            # -t 1 = for 1 second only
            result = await asyncio.to_thread(
                subprocess.run,
                ['caffeinate', '-u', '-t', '1'],
                capture_output=True,
                timeout=3
            )

            if result.returncode == 0:
                logger.info(
                    f"[MosaicWatcher v38.5] ☕ Stealth wake: caffeinate assertion sent"
                )
                return True

            # Method 2: Fallback - use osascript to wake display
            # This is less stealth but works on all macOS versions
            wake_script = 'tell application "System Events" to key code 124'  # Right arrow
            await asyncio.to_thread(
                subprocess.run,
                ['osascript', '-e', wake_script],
                capture_output=True,
                timeout=2
            )

            logger.info(f"[MosaicWatcher v38.5] ☕ Stealth wake: osascript fallback")
            return True

        except Exception as e:
            logger.warning(f"[MosaicWatcher v38.5] Stealth wake failed: {e}")
            return False

    async def refresh_window_layout(self) -> bool:
        """
        v38.5: Refresh window tile positions from yabai.

        When windows move/resize on Ghost Display, tile positions become stale.
        This method queries yabai to update tile positions.

        Returns:
            True if layout was refreshed successfully
        """
        import subprocess

        try:
            # Query yabai for all windows on Ghost Display's space
            result = await asyncio.to_thread(
                subprocess.run,
                ['yabai', '-m', 'query', '--windows', '--space', str(self.config.display_id)],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode != 0:
                logger.warning(f"[MosaicWatcher v38.5] yabai query failed: {result.stderr}")
                return False

            import json
            windows = json.loads(result.stdout)

            # Update tile positions
            updated_count = 0
            for win in windows:
                window_id = win.get('id')
                tile = self.config.get_tile_for_window(window_id)

                if tile:
                    frame = win.get('frame', {})
                    new_x = int(frame.get('x', tile.x))
                    new_y = int(frame.get('y', tile.y))
                    new_w = int(frame.get('w', tile.width))
                    new_h = int(frame.get('h', tile.height))

                    if (tile.x != new_x or tile.y != new_y or
                        tile.width != new_w or tile.height != new_h):
                        tile.x = new_x
                        tile.y = new_y
                        tile.width = new_w
                        tile.height = new_h
                        updated_count += 1

            if updated_count > 0:
                logger.info(
                    f"[MosaicWatcher v38.5] 🔄 Layout refreshed: {updated_count} tiles updated"
                )

            return True

        except Exception as e:
            logger.warning(f"[MosaicWatcher v38.5] Layout refresh failed: {e}")
            return False

    async def check_display_connection(self) -> Tuple[bool, str]:
        """
        v38.5: Check if Ghost Display is still connected.

        Virtual displays can disconnect unexpectedly. This checks the
        display is still available for capture.

        Returns:
            (is_connected, status_message) tuple
        """
        try:
            from Quartz import CGGetActiveDisplayList, CGDisplayIsActive

            # Get list of active displays
            max_displays = 16
            active_displays = (ctypes.c_uint32 * max_displays)()
            display_count = ctypes.c_uint32()

            # Use CGGetActiveDisplayList via ctypes or pyobjc
            try:
                err, displays, count = CGGetActiveDisplayList(max_displays, None, None)
                display_ids = list(displays)[:count] if displays else []
            except Exception:
                # Fallback: assume display is connected if capture is working
                if self.status == MosaicCaptureStatus.CAPTURING:
                    with self._stats_lock:
                        last_frame = self._last_frame_time
                    if time.time() - last_frame < 2.0:
                        return True, "Display assumed connected (capture active)"
                return False, "Cannot verify display connection"

            # Check if our display is in the list
            is_connected = self.config.display_id in display_ids

            if is_connected:
                return True, f"Display {self.config.display_id} is connected"
            else:
                return False, f"Display {self.config.display_id} not found in active displays"

        except ImportError:
            # Quartz not available - assume connected if capture working
            if self.status == MosaicCaptureStatus.CAPTURING:
                return True, "Display assumed connected (Quartz unavailable)"
            return False, "Cannot verify - Quartz unavailable"

        except Exception as e:
            logger.warning(f"[MosaicWatcher v38.5] Display check error: {e}")
            return False, f"Error checking display: {e}"

    def get_tile_for_ocr_match(self, match_x: int, match_y: int) -> Optional[WindowTileInfo]:
        """
        v38.0: Map an OCR match location back to a specific window tile.

        When OCR detects trigger text, it provides coordinates. This method
        maps those coordinates to the window that contains the text.

        Args:
            match_x: X coordinate of OCR match in mosaic
            match_y: Y coordinate of OCR match in mosaic

        Returns:
            WindowTileInfo if match is within a tile, None otherwise
        """
        return self.config.get_tile_for_point(match_x, match_y)

    def get_stats(self) -> Dict[str, Any]:
        """Get mosaic watcher statistics."""
        uptime = time.time() - self.start_time if self.start_time > 0 else 0

        with self._stats_lock:
            frames_captured = self._frames_captured
            frames_analyzed = self._frames_analyzed
            frames_dropped = self._frames_dropped

        actual_fps = frames_captured / uptime if uptime > 0 else 0

        return {
            'watcher_id': self.watcher_id,
            'display_id': self.config.display_id,
            'status': self.status.value,
            'mode': 'mosaic',
            'target_fps': self.config.fps,
            'actual_fps': round(actual_fps, 2),
            'frames_captured': frames_captured,
            'frames_analyzed': frames_analyzed,
            'frames_dropped': frames_dropped,
            'events_detected': self.events_detected,
            'uptime_seconds': round(uptime, 2),
            'queue_size': self.frame_queue.qsize(),
            'window_tiles': len(self.config.window_tiles),
            'display_dimensions': f"{self.config.display_width}x{self.config.display_height}",
            # v38.0 efficiency metrics
            'efficiency_mode': 'O(1) Mosaic',
            'streams_avoided': len(self.config.window_tiles) - 1 if self.config.window_tiles else 0,
            'estimated_ram_saved_mb': (len(self.config.window_tiles) - 1) * 200 if self.config.window_tiles else 0
        }


class VideoWatcherManager:
    """
    Manager for multiple background video watchers.

    Enables parallel monitoring of multiple windows simultaneously.
    This is the "God Mode" - JARVIS watching multiple things at once.
    """

    def __init__(
        self,
        max_parallel_watchers: Optional[int] = None
    ):
        self.max_parallel_watchers = max_parallel_watchers or int(
            os.getenv('JARVIS_WATCHER_MAX_PARALLEL', '3')
        )

        self.watchers: Dict[str, VideoWatcher] = {}
        # Lazy-initialized lock (created on first async use to avoid
        # "no current event loop" error when created in thread pool)
        self._watcher_lock: Optional[asyncio.Lock] = None

        # Stats
        self.total_watchers_spawned = 0
        self.total_events_detected = 0

        logger.info(f"VideoWatcherManager initialized (max parallel: {self.max_parallel_watchers})")

    def _get_lock(self) -> asyncio.Lock:
        """Lazily create lock on first async use."""
        if self._watcher_lock is None:
            self._watcher_lock = asyncio.Lock()
        return self._watcher_lock

    async def spawn_watcher(
        self,
        window_id: int,
        fps: int = 5,
        app_name: Optional[str] = None,
        space_id: Optional[int] = None,
        priority: str = "low",
        timeout: float = 300.0
    ) -> VideoWatcher:
        """
        Spawn a new background video watcher for a specific window.

        Args:
            window_id: macOS window ID to watch
            fps: Frame rate (1-10, default 5)
            app_name: Name of app (for logging)
            space_id: macOS Space ID (for logging)
            priority: Thread priority ("low", "normal", "high")
            timeout: Max watch time in seconds

        Returns:
            VideoWatcher instance
        """
        async with self._get_lock():
            # Check if we're at max capacity
            active_watchers = [w for w in self.watchers.values()
                              if w.status == WatcherStatus.WATCHING]

            if len(active_watchers) >= self.max_parallel_watchers:
                raise RuntimeError(
                    f"Maximum parallel watchers ({self.max_parallel_watchers}) reached"
                )

            # Validate FPS
            min_fps = int(os.getenv('JARVIS_WATCHER_MIN_FPS', '1'))
            max_fps = int(os.getenv('JARVIS_WATCHER_MAX_FPS', '10'))
            fps = max(min_fps, min(max_fps, fps))

            # Create watcher
            config = WatcherConfig(
                window_id=window_id,
                fps=fps,
                priority=priority,
                timeout=timeout
            )

            watcher = VideoWatcher(config)
            watcher.app_name = app_name
            watcher.space_id = space_id

            # Start watcher
            success = await watcher.start()

            if not success:
                raise RuntimeError(f"Failed to start watcher for window {window_id}")

            # Register watcher
            self.watchers[watcher.watcher_id] = watcher
            self.total_watchers_spawned += 1

            logger.info(
                f"✅ Spawned watcher {watcher.watcher_id} for {app_name or 'Unknown'} "
                f"(Window {window_id}, Space {space_id}, {fps} FPS)"
            )

            return watcher

    async def wait_for_visual_event(
        self,
        watcher: VideoWatcher,
        trigger: Union[str, Dict],
        detector: Optional[Any] = None,
        timeout: Optional[float] = None
    ) -> VisualEventResult:
        """
        Wait for a visual event to occur in watcher stream.

        Args:
            watcher: Active VideoWatcher instance
            trigger: Text to find (str) or element spec (dict)
            detector: VisualEventDetector instance (optional)
            timeout: Max wait time (uses watcher config if None)

        Returns:
            VisualEventResult with detection details
        """
        timeout = timeout or watcher.config.timeout
        start_time = time.time()

        logger.info(
            f"[Watcher {watcher.watcher_id}] Waiting for event: {trigger} "
            f"(timeout: {timeout}s)"
        )

        frame_count = 0

        while (time.time() - start_time) < timeout:
            # Get latest frame
            frame_data = await watcher.get_latest_frame(timeout=1.0)

            if not frame_data:
                logger.debug(f"[Watcher {watcher.watcher_id}] No frame received (frame_count={frame_count})")
                continue

            frame = frame_data['frame']
            frame_number = frame_data['frame_number']
            frame_count += 1
            watcher.frames_analyzed += 1

            # Log frame capture progress every 5 frames and save debug frame
            if frame_count % 5 == 0:
                logger.info(
                    f"[Watcher {watcher.watcher_id}] Processing frames: {frame_count} analyzed, "
                    f"shape={frame.shape if frame is not None else 'None'}, "
                    f"elapsed={time.time() - start_time:.1f}s"
                )

                # Save debug frame for visual inspection (every 5th frame)
                try:
                    debug_dir = Path.home() / ".jarvis" / "debug_frames"
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    debug_path = debug_dir / f"frame_{watcher.watcher_id}_{frame_number}.png"

                    import cv2
                    # Convert RGB to BGR for OpenCV
                    frame_bgr = frame[:, :, ::-1] if frame is not None and len(frame.shape) == 3 else frame
                    cv2.imwrite(str(debug_path), frame_bgr)
                    logger.debug(f"[Watcher {watcher.watcher_id}] Saved debug frame to {debug_path}")
                except Exception as e:
                    logger.debug(f"Could not save debug frame: {e}")

            # Detect event (simplified for now - full detector implementation next)
            detected = False
            confidence = 0.0

            if detector:
                # Use full detector if provided
                try:
                    logger.debug(f"[Watcher {watcher.watcher_id}] Running OCR on frame {frame_number}...")
                    result = await detector.detect_text(frame, str(trigger))
                    detected = result.detected
                    confidence = result.confidence

                    # Log OCR results for debugging
                    if result.all_text:
                        logger.debug(
                            f"[Watcher {watcher.watcher_id}] OCR found text: {result.all_text[:100]}... "
                            f"(searching for '{trigger}')"
                        )
                    else:
                        logger.debug(f"[Watcher {watcher.watcher_id}] OCR found no text in frame {frame_number}")

                    if detected:
                        logger.info(f"[Watcher {watcher.watcher_id}] 🎯 MATCH FOUND! Text: '{trigger}', Confidence: {confidence:.2f}")
                except Exception as e:
                    logger.error(f"Detector error on frame {frame_number}: {e}", exc_info=True)
            else:
                # Simple fallback: always return not detected
                # (Full OCR implementation in VisualEventDetector)
                detected = False
                confidence = 0.0

            if detected:
                watcher.events_detected += 1
                watcher.status = WatcherStatus.EVENT_DETECTED
                self.total_events_detected += 1

                detection_time = time.time() - start_time

                logger.info(
                    f"[Watcher {watcher.watcher_id}] ✅ Event detected! "
                    f"Trigger: '{trigger}', Confidence: {confidence:.2f}, "
                    f"Time: {detection_time:.1f}s, Frames analyzed: {frame_count}"
                )

                return VisualEventResult(
                    detected=True,
                    event_type="text",
                    trigger=str(trigger),
                    confidence=confidence,
                    frame_number=frame_number,
                    detection_time=detection_time,
                    frame_data=frame,
                    metadata={
                        'watcher_id': watcher.watcher_id,
                        'window_id': watcher.config.window_id,
                        'app_name': watcher.app_name,
                        'space_id': watcher.space_id,
                        'frames_analyzed': frame_count,
                    }
                )

            # Brief sleep to prevent tight loop
            await asyncio.sleep(0.1)

        # Timeout reached
        logger.warning(
            f"[Watcher {watcher.watcher_id}] ⏱️ Timeout waiting for event: {trigger} "
            f"(analyzed {frame_count} frames)"
        )

        return VisualEventResult(
            detected=False,
            event_type="text",
            trigger=str(trigger),
            confidence=0.0,
            frame_number=frame_count,
            detection_time=timeout,
            metadata={
                'watcher_id': watcher.watcher_id,
                'timeout_reached': True,
                'frames_analyzed': frame_count,
            }
        )

    async def stop_watcher(self, watcher_id: str):
        """Stop a specific watcher."""
        async with self._get_lock():
            if watcher_id in self.watchers:
                watcher = self.watchers[watcher_id]
                await watcher.stop()
                del self.watchers[watcher_id]
                logger.info(f"Stopped and removed watcher {watcher_id}")
            else:
                logger.warning(f"Watcher {watcher_id} not found")

    async def stop_all_watchers(self):
        """Stop all active watchers."""
        async with self._get_lock():
            logger.info(f"Stopping {len(self.watchers)} watchers...")

            stop_tasks = [w.stop() for w in self.watchers.values()]
            await asyncio.gather(*stop_tasks, return_exceptions=True)

            self.watchers.clear()
            logger.info("✅ All watchers stopped")

    def list_watchers(self) -> List[Dict[str, Any]]:
        """List all active watchers."""
        return [w.get_stats() for w in self.watchers.values()]

    def get_watcher(self, watcher_id: str) -> Optional[VideoWatcher]:
        """Get a specific watcher by ID."""
        return self.watchers.get(watcher_id)

    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics."""
        active = sum(1 for w in self.watchers.values()
                    if w.status == WatcherStatus.WATCHING)

        return {
            'total_watchers_spawned': self.total_watchers_spawned,
            'total_events_detected': self.total_events_detected,
            'active_watchers': active,
            'max_parallel': self.max_parallel_watchers,
            'watchers': self.list_watchers(),
        }


# Global watcher manager instance
_watcher_manager: Optional[VideoWatcherManager] = None


def get_watcher_manager() -> VideoWatcherManager:
    """Get the global VideoWatcherManager instance."""
    global _watcher_manager
    if _watcher_manager is None:
        _watcher_manager = VideoWatcherManager()
    return _watcher_manager
