"""
JARVIS macOS Helper - Permission Manager

Manages macOS TCC (Transparency, Consent, Control) permissions.
Provides a unified interface for checking, requesting, and monitoring
permission status for all required system capabilities.

Features:
- Check all required permissions on startup
- Guide user through permission onboarding
- Monitor permission changes
- Emit events when permissions change
- Graceful degradation when permissions denied

Apple Compliance:
- Uses only public APIs for permission checking
- Respects user choices - never bypasses TCC
- Clear explanations of why each permission is needed
- Supports running with reduced permissions

Required Permissions:
- Accessibility: Window detection, UI automation
- Screen Recording: Vision system, screen capture
- Microphone: Voice commands, speaker verification
- Automation: AppleScript control of apps
- Notifications: Post-delivery notification reading
- Full Disk Access: Optional - for file monitoring
- Calendar: Optional - for meeting awareness
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import subprocess
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)

# =============================================================================
# v264.0: Cached CoreGraphics Library Handle + CGRect Struct
# =============================================================================
# CoreGraphics is loaded once at module level (macOS only) to avoid repeated
# ctypes.util.find_library() filesystem traversal on every permission check.
# The recheck loop calls check_permission(use_cache=False) every 10s — without
# caching, find_library() would run 6x/min doing otool/subprocess calls.
# =============================================================================

_cg_lib = None  # Cached CoreGraphics ctypes handle
_cf_lib = None  # Cached CoreFoundation ctypes handle

if sys.platform == "darwin":
    try:
        _cg_path = ctypes.util.find_library("CoreGraphics")
        if _cg_path:
            _cg_lib = ctypes.cdll.LoadLibrary(_cg_path)
        _cf_path = ctypes.util.find_library("CoreFoundation")
        if _cf_path:
            _cf_lib = ctypes.cdll.LoadLibrary(_cf_path)
    except (OSError, AttributeError) as _e:
        logger.debug(f"CoreGraphics/CoreFoundation ctypes load failed: {_e}")


class _CGPoint(ctypes.Structure):
    """CoreGraphics CGPoint struct for ctypes interop."""
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    """CoreGraphics CGSize struct for ctypes interop."""
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    """CoreGraphics CGRect struct for ctypes interop."""
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


# =============================================================================
# Permission Types and Status
# =============================================================================

class PermissionType(str, Enum):
    """Types of macOS permissions JARVIS may need."""
    ACCESSIBILITY = "accessibility"
    SCREEN_RECORDING = "screen_recording"
    MICROPHONE = "microphone"
    AUTOMATION = "automation"
    NOTIFICATIONS = "notifications"
    FULL_DISK_ACCESS = "full_disk_access"
    CALENDAR = "calendar"
    REMINDERS = "reminders"
    CONTACTS = "contacts"
    LOCATION = "location"
    CAMERA = "camera"


class PermissionStatus(str, Enum):
    """Status of a permission."""
    GRANTED = "granted"
    DENIED = "denied"
    NOT_DETERMINED = "not_determined"
    RESTRICTED = "restricted"  # Managed by MDM/parental controls
    UNKNOWN = "unknown"


class PermissionImportance(str, Enum):
    """How important a permission is for JARVIS operation."""
    REQUIRED = "required"  # JARVIS won't function without this
    RECOMMENDED = "recommended"  # Core features need this
    OPTIONAL = "optional"  # Nice to have


# =============================================================================
# Permission Info and Result
# =============================================================================

@dataclass
class PermissionInfo:
    """Information about a permission."""
    permission_type: PermissionType
    importance: PermissionImportance
    name: str  # Human-readable name
    description: str  # Why JARVIS needs this
    settings_path: str  # Path in System Settings
    features_enabled: List[str]  # Features this permission enables
    features_disabled: List[str]  # Features disabled without this

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.permission_type.value,
            "importance": self.importance.value,
            "name": self.name,
            "description": self.description,
            "settings_path": self.settings_path,
            "features_enabled": self.features_enabled,
            "features_disabled": self.features_disabled,
        }


@dataclass
class PermissionCheckResult:
    """Result of checking a permission."""
    permission_type: PermissionType
    status: PermissionStatus
    checked_at: datetime = field(default_factory=datetime.now)
    error: Optional[str] = None
    needs_restart: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.permission_type.value,
            "status": self.status.value,
            "checked_at": self.checked_at.isoformat(),
            "error": self.error,
            "needs_restart": self.needs_restart,
        }


@dataclass
class PermissionsOverview:
    """Overview of all permission statuses."""
    results: Dict[PermissionType, PermissionCheckResult] = field(default_factory=dict)
    all_required_granted: bool = False
    all_recommended_granted: bool = False
    checked_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permissions": {
                k.value: v.to_dict() for k, v in self.results.items()
            },
            "all_required_granted": self.all_required_granted,
            "all_recommended_granted": self.all_recommended_granted,
            "checked_at": self.checked_at.isoformat(),
        }


# =============================================================================
# Permission Definitions
# =============================================================================

PERMISSION_DEFINITIONS: Dict[PermissionType, PermissionInfo] = {
    PermissionType.ACCESSIBILITY: PermissionInfo(
        permission_type=PermissionType.ACCESSIBILITY,
        importance=PermissionImportance.REQUIRED,
        name="Accessibility",
        description=(
            "Required for detecting windows, reading UI elements, and performing "
            "automated actions on your behalf. Without this, JARVIS cannot see "
            "what's on your screen or control applications."
        ),
        settings_path="Privacy & Security > Accessibility",
        features_enabled=[
            "Window detection",
            "App automation",
            "UI element reading",
            "Keyboard/mouse automation",
        ],
        features_disabled=[
            "Screen awareness",
            "Intelligent suggestions",
            "Automated actions",
        ],
    ),
    PermissionType.SCREEN_RECORDING: PermissionInfo(
        permission_type=PermissionType.SCREEN_RECORDING,
        importance=PermissionImportance.REQUIRED,
        name="Screen Recording",
        description=(
            "Required for capturing screenshots and understanding what's displayed. "
            "JARVIS uses this to see and understand your screen content, enabling "
            "intelligent assistance based on visual context."
        ),
        settings_path="Privacy & Security > Screen Recording",
        features_enabled=[
            "Screen capture",
            "Visual analysis",
            "Context awareness",
            "Error detection",
        ],
        features_disabled=[
            "Vision system",
            "Screen-based suggestions",
            "Visual search",
        ],
    ),
    PermissionType.MICROPHONE: PermissionInfo(
        permission_type=PermissionType.MICROPHONE,
        importance=PermissionImportance.REQUIRED,
        name="Microphone",
        description=(
            "Required for voice commands and speaker verification. JARVIS listens "
            "for your voice to understand commands and verify your identity for "
            "secure operations like screen unlock."
        ),
        settings_path="Privacy & Security > Microphone",
        features_enabled=[
            "Voice commands",
            "Speaker verification",
            "Voice unlock",
            "Always-on listening (when enabled)",
        ],
        features_disabled=[
            "Voice control",
            "Voice authentication",
            "Hands-free operation",
        ],
    ),
    PermissionType.AUTOMATION: PermissionInfo(
        permission_type=PermissionType.AUTOMATION,
        importance=PermissionImportance.RECOMMENDED,
        name="Automation",
        description=(
            "Allows JARVIS to control other applications via AppleScript. This "
            "enables features like opening apps, controlling browsers, and "
            "automating repetitive tasks."
        ),
        settings_path="Privacy & Security > Automation",
        features_enabled=[
            "App control",
            "Browser automation",
            "System control",
            "Workflow automation",
        ],
        features_disabled=[
            "Automatic app launching",
            "Web search automation",
        ],
    ),
    PermissionType.NOTIFICATIONS: PermissionInfo(
        permission_type=PermissionType.NOTIFICATIONS,
        importance=PermissionImportance.RECOMMENDED,
        name="Notifications",
        description=(
            "Allows JARVIS to read notifications after they appear. This enables "
            "intelligent summarization of notifications and proactive assistance."
        ),
        settings_path="Privacy & Security > Notifications (app-specific)",
        features_enabled=[
            "Notification awareness",
            "Smart summaries",
            "Proactive alerts",
        ],
        features_disabled=[
            "Notification reading",
            "Message summaries",
        ],
    ),
    PermissionType.FULL_DISK_ACCESS: PermissionInfo(
        permission_type=PermissionType.FULL_DISK_ACCESS,
        importance=PermissionImportance.OPTIONAL,
        name="Full Disk Access",
        description=(
            "Optional permission for monitoring file changes across your system. "
            "Enables features like detecting when you save files or tracking "
            "project changes."
        ),
        settings_path="Privacy & Security > Full Disk Access",
        features_enabled=[
            "File change monitoring",
            "Project awareness",
            "Document tracking",
        ],
        features_disabled=[
            "Real-time file monitoring",
        ],
    ),
    PermissionType.CALENDAR: PermissionInfo(
        permission_type=PermissionType.CALENDAR,
        importance=PermissionImportance.OPTIONAL,
        name="Calendar",
        description=(
            "Optional permission for reading your calendar. Enables meeting "
            "awareness and proactive reminders about upcoming events."
        ),
        settings_path="Privacy & Security > Calendars",
        features_enabled=[
            "Meeting reminders",
            "Schedule awareness",
            "Calendar integration",
        ],
        features_disabled=[
            "Meeting notifications",
            "Schedule-based suggestions",
        ],
    ),
    PermissionType.REMINDERS: PermissionInfo(
        permission_type=PermissionType.REMINDERS,
        importance=PermissionImportance.OPTIONAL,
        name="Reminders",
        description=(
            "Optional permission for reading and creating reminders. Enables "
            "task management and to-do integration."
        ),
        settings_path="Privacy & Security > Reminders",
        features_enabled=[
            "Task management",
            "Reminder integration",
            "To-do tracking",
        ],
        features_disabled=[
            "Reminder reading",
            "Task creation",
        ],
    ),
}


# =============================================================================
# Permission Manager
# =============================================================================

class PermissionManager:
    """
    Manages macOS TCC permissions for JARVIS.

    Provides:
    - Permission status checking
    - User guidance for granting permissions
    - Event emission on permission changes
    - Graceful degradation support
    """

    def __init__(
        self,
        check_interval_seconds: float = 60.0,
        emit_events: bool = True,
    ):
        """
        Initialize the permission manager.

        Args:
            check_interval_seconds: How often to recheck permissions
            emit_events: Whether to emit events on permission changes
        """
        self._check_interval = check_interval_seconds
        self._emit_events = emit_events

        # Cached results
        self._cached_results: Dict[PermissionType, PermissionCheckResult] = {}
        self._last_full_check: Optional[datetime] = None

        # Event callbacks
        self._on_permission_changed: List[Callable[[PermissionCheckResult], Coroutine]] = []

        # Monitoring state
        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None

        # Event bus (lazy loaded)
        self._event_bus = None

        logger.info("PermissionManager initialized")

    # =========================================================================
    # Permission Checking
    # =========================================================================

    async def check_permission(
        self,
        permission_type: PermissionType,
        use_cache: bool = True,
    ) -> PermissionCheckResult:
        """
        Check the status of a specific permission.

        Args:
            permission_type: The permission to check
            use_cache: Whether to use cached result if available

        Returns:
            PermissionCheckResult with current status
        """
        # Check cache
        if use_cache and permission_type in self._cached_results:
            cached = self._cached_results[permission_type]
            # Cache valid for 60 seconds
            if (datetime.now() - cached.checked_at).seconds < 60:
                return cached

        # Perform actual check
        try:
            status = await self._check_permission_status(permission_type)
            result = PermissionCheckResult(
                permission_type=permission_type,
                status=status,
            )
        except Exception as e:
            logger.error(f"Error checking {permission_type.value}: {e}")
            result = PermissionCheckResult(
                permission_type=permission_type,
                status=PermissionStatus.UNKNOWN,
                error=str(e),
            )

        # Cache result
        self._cached_results[permission_type] = result
        return result

    async def check_all_permissions(self) -> PermissionsOverview:
        """
        Check all defined permissions.

        Returns:
            PermissionsOverview with all permission statuses
        """
        overview = PermissionsOverview()

        # Check all permissions concurrently
        tasks = []
        for perm_type in PERMISSION_DEFINITIONS.keys():
            tasks.append(self.check_permission(perm_type, use_cache=False))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        all_required = True
        all_recommended = True

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Permission check failed: {result}")
                continue

            overview.results[result.permission_type] = result
            info = PERMISSION_DEFINITIONS.get(result.permission_type)

            if info:
                if info.importance == PermissionImportance.REQUIRED:
                    if result.status != PermissionStatus.GRANTED:
                        all_required = False
                elif info.importance == PermissionImportance.RECOMMENDED:
                    if result.status != PermissionStatus.GRANTED:
                        all_recommended = False

        overview.all_required_granted = all_required
        overview.all_recommended_granted = all_recommended
        overview.checked_at = datetime.now()
        self._last_full_check = overview.checked_at

        return overview

    async def _check_permission_status(
        self,
        permission_type: PermissionType
    ) -> PermissionStatus:
        """
        Check the actual status of a permission.

        Uses various methods depending on permission type.
        """
        if permission_type == PermissionType.ACCESSIBILITY:
            return await self._check_accessibility()
        elif permission_type == PermissionType.SCREEN_RECORDING:
            return await self._check_screen_recording()
        elif permission_type == PermissionType.MICROPHONE:
            return await self._check_microphone()
        elif permission_type == PermissionType.AUTOMATION:
            return await self._check_automation()
        elif permission_type == PermissionType.FULL_DISK_ACCESS:
            return await self._check_full_disk_access()
        elif permission_type == PermissionType.CALENDAR:
            return await self._check_calendar()
        elif permission_type == PermissionType.REMINDERS:
            return await self._check_reminders()
        elif permission_type == PermissionType.NOTIFICATIONS:
            return await self._check_notifications()
        else:
            return PermissionStatus.UNKNOWN

    async def _check_accessibility(self) -> PermissionStatus:
        """Check Accessibility permission using AXIsProcessTrusted."""
        try:
            # Use Python subprocess to check via AppleScript
            # AXIsProcessTrusted() returns true/false
            script = """
            use framework "ApplicationServices"
            return current application's AXIsProcessTrusted() as boolean
            """
            result = await asyncio.create_subprocess_exec(
                "osascript", "-l", "AppleScript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()

            if b"true" in stdout.lower():
                return PermissionStatus.GRANTED
            else:
                return PermissionStatus.DENIED

        except Exception as e:
            logger.debug(f"Accessibility check fallback: {e}")
            # Fallback: Try using ApplicationServices via ctypes
            try:
                import ctypes
                lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
                trusted = lib.AXIsProcessTrusted()
                return PermissionStatus.GRANTED if trusted else PermissionStatus.DENIED
            except Exception:
                return PermissionStatus.UNKNOWN

    async def _check_screen_recording(self) -> PermissionStatus:
        """
        Check Screen Recording permission using the most reliable method available.

        v264.0: Replaced screencapture-based check (which can produce blank-but-non-zero
        files giving false positives) with a 3-tier approach:
          1. CGPreflightScreenCaptureAccess (macOS 10.15+, most reliable)
          2. CGWindowListCreateImage probe (older macOS fallback)
          3. screencapture command (last resort)
        """
        # Method 1: CGPreflightScreenCaptureAccess (macOS 10.15+, most reliable)
        # Uses module-level cached _cg_lib handle to avoid repeated find_library() calls.
        try:
            if _cg_lib is not None:
                _cg_lib.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
                granted = _cg_lib.CGPreflightScreenCaptureAccess()
                logger.debug(f"CGPreflightScreenCaptureAccess returned: {granted}")
                return PermissionStatus.GRANTED if granted else PermissionStatus.DENIED
        except (OSError, AttributeError) as e:
            logger.debug(f"CGPreflightScreenCaptureAccess not available: {e}")

        # Method 2: CGWindowListCreateImage probe (older macOS)
        # Uses module-level cached _cg_lib + _cf_lib handles and _CGRect struct.
        try:
            if _cg_lib is not None:
                _cg_lib.CGWindowListCreateImage.restype = ctypes.c_void_p
                _cg_lib.CGWindowListCreateImage.argtypes = [
                    _CGRect,             # screenBounds
                    ctypes.c_uint32,     # listOption
                    ctypes.c_uint32,     # windowID
                    ctypes.c_uint32,     # imageOption
                ]
                rect = _CGRect()
                rect.origin.x = 0.0
                rect.origin.y = 0.0
                rect.size.width = 1.0
                rect.size.height = 1.0
                # kCGWindowListOptionOnScreenOnly=1, kCGNullWindowID=0, kCGWindowImageDefault=0
                img_ref = _cg_lib.CGWindowListCreateImage(rect, 1, 0, 0)
                if img_ref:
                    if _cf_lib is not None:
                        _cf_lib.CFRelease(ctypes.c_void_p(img_ref))
                    logger.debug("CGWindowListCreateImage probe: GRANTED")
                    return PermissionStatus.GRANTED
                else:
                    logger.debug("CGWindowListCreateImage returned NULL: DENIED")
                    return PermissionStatus.DENIED
        except (OSError, AttributeError) as e:
            logger.debug(f"CGWindowListCreateImage probe failed: {e}")

        # Method 3: screencapture command (last resort — can give false positives)
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                temp_path = f.name

            result = await asyncio.create_subprocess_exec(
                "screencapture", "-x", "-t", "png", temp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()

            if os.path.exists(temp_path):
                size = os.path.getsize(temp_path)
                os.unlink(temp_path)
                if size > 0:
                    return PermissionStatus.GRANTED

            return PermissionStatus.DENIED
        except Exception as e:
            logger.debug(f"Screen recording check error: {e}")
            return PermissionStatus.UNKNOWN

    async def request_screen_recording_permission(self) -> bool:
        """
        v264.0: Request Screen Recording permission from the OS.

        Calls CGRequestScreenCaptureAccess() to trigger the native dialog.
        Falls back to opening System Settings if the API is unavailable.

        Returns:
            True if the request was triggered successfully (NOT whether permission was granted).
        """
        # Try CGRequestScreenCaptureAccess (macOS 10.15+) — uses cached handle
        try:
            if _cg_lib is not None:
                _cg_lib.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
                result = _cg_lib.CGRequestScreenCaptureAccess()
                logger.info(f"[Permissions] CGRequestScreenCaptureAccess triggered (returned: {result})")
                return True
        except (OSError, AttributeError) as e:
            logger.debug(f"CGRequestScreenCaptureAccess not available: {e}")

        # Fallback: open System Settings
        return await self.open_permission_settings(PermissionType.SCREEN_RECORDING)

    async def _check_microphone(self) -> PermissionStatus:
        """Check Microphone permission."""
        try:
            # Use AVFoundation to check authorization status
            script = """
            use framework "AVFoundation"
            set authStatus to current application's AVCaptureDevice's authorizationStatusForMediaType:"soun"
            if authStatus = 3 then
                return "granted"
            else if authStatus = 2 then
                return "denied"
            else if authStatus = 0 then
                return "not_determined"
            else
                return "restricted"
            end if
            """
            result = await asyncio.create_subprocess_exec(
                "osascript", "-l", "AppleScript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            output = stdout.decode().strip().lower()

            if "granted" in output:
                return PermissionStatus.GRANTED
            elif "denied" in output:
                return PermissionStatus.DENIED
            elif "not_determined" in output:
                return PermissionStatus.NOT_DETERMINED
            elif "restricted" in output:
                return PermissionStatus.RESTRICTED
            else:
                return PermissionStatus.UNKNOWN

        except Exception as e:
            logger.debug(f"Microphone check error: {e}")
            return PermissionStatus.UNKNOWN

    async def _check_automation(self) -> PermissionStatus:
        """Check Automation permission by testing AppleScript."""
        try:
            # Try a simple AppleScript that requires automation
            script = 'tell application "Finder" to get name of startup disk'
            result = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()

            if result.returncode == 0:
                return PermissionStatus.GRANTED
            elif b"not allowed" in stderr.lower() or b"-1743" in stderr:
                return PermissionStatus.DENIED
            else:
                return PermissionStatus.GRANTED  # Other errors might be unrelated

        except Exception as e:
            logger.debug(f"Automation check error: {e}")
            return PermissionStatus.UNKNOWN

    async def _check_full_disk_access(self) -> PermissionStatus:
        """Check Full Disk Access permission."""
        try:
            # Try to read a protected file
            protected_paths = [
                os.path.expanduser("~/Library/Safari/Bookmarks.plist"),
                os.path.expanduser("~/Library/Mail"),
                "/Library/Application Support/com.apple.TCC/TCC.db",
            ]

            for path in protected_paths:
                if os.path.exists(path):
                    try:
                        with open(path, "rb") as f:
                            f.read(1)
                        return PermissionStatus.GRANTED
                    except PermissionError:
                        return PermissionStatus.DENIED
                    except Exception:
                        continue

            return PermissionStatus.UNKNOWN

        except Exception as e:
            logger.debug(f"Full Disk Access check error: {e}")
            return PermissionStatus.UNKNOWN

    async def _check_calendar(self) -> PermissionStatus:
        """Check Calendar permission using EventKit."""
        try:
            script = """
            use framework "EventKit"
            set eventStore to current application's EKEventStore's alloc()'s init()
            set authStatus to current application's EKEventStore's authorizationStatusForEntityType:0
            if authStatus = 3 then
                return "granted"
            else if authStatus = 2 then
                return "denied"
            else if authStatus = 0 then
                return "not_determined"
            else
                return "restricted"
            end if
            """
            result = await asyncio.create_subprocess_exec(
                "osascript", "-l", "AppleScript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            output = stdout.decode().strip().lower()

            if "granted" in output:
                return PermissionStatus.GRANTED
            elif "denied" in output:
                return PermissionStatus.DENIED
            elif "not_determined" in output:
                return PermissionStatus.NOT_DETERMINED
            else:
                return PermissionStatus.UNKNOWN

        except Exception as e:
            logger.debug(f"Calendar check error: {e}")
            return PermissionStatus.UNKNOWN

    async def _check_reminders(self) -> PermissionStatus:
        """Check Reminders permission using EventKit."""
        try:
            script = """
            use framework "EventKit"
            set eventStore to current application's EKEventStore's alloc()'s init()
            set authStatus to current application's EKEventStore's authorizationStatusForEntityType:1
            if authStatus = 3 then
                return "granted"
            else if authStatus = 2 then
                return "denied"
            else if authStatus = 0 then
                return "not_determined"
            else
                return "restricted"
            end if
            """
            result = await asyncio.create_subprocess_exec(
                "osascript", "-l", "AppleScript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            output = stdout.decode().strip().lower()

            if "granted" in output:
                return PermissionStatus.GRANTED
            elif "denied" in output:
                return PermissionStatus.DENIED
            elif "not_determined" in output:
                return PermissionStatus.NOT_DETERMINED
            else:
                return PermissionStatus.UNKNOWN

        except Exception as e:
            logger.debug(f"Reminders check error: {e}")
            return PermissionStatus.UNKNOWN

    async def _check_notifications(self) -> PermissionStatus:
        """Check Notifications permission."""
        # Notifications are per-app and don't have a global check
        # We'll assume granted if we can create a notification center
        try:
            script = """
            tell application "System Events"
                return "granted"
            end tell
            """
            result = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()

            if result.returncode == 0:
                return PermissionStatus.GRANTED
            else:
                return PermissionStatus.DENIED

        except Exception:
            return PermissionStatus.UNKNOWN

    # =========================================================================
    # Permission Request Guidance
    # =========================================================================

    async def open_permission_settings(
        self,
        permission_type: PermissionType
    ) -> bool:
        """
        Open System Settings to the relevant permission panel.

        Args:
            permission_type: The permission settings to open

        Returns:
            True if settings were opened successfully
        """
        try:
            # System Settings URLs for different permissions
            settings_urls = {
                PermissionType.ACCESSIBILITY: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                PermissionType.SCREEN_RECORDING: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
                PermissionType.MICROPHONE: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
                PermissionType.AUTOMATION: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
                PermissionType.FULL_DISK_ACCESS: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
                PermissionType.CALENDAR: "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars",
                PermissionType.REMINDERS: "x-apple.systempreferences:com.apple.preference.security?Privacy_Reminders",
                PermissionType.NOTIFICATIONS: "x-apple.systempreferences:com.apple.Notifications-Settings.extension",
            }

            url = settings_urls.get(permission_type)
            if not url:
                logger.warning(f"No settings URL for {permission_type.value}")
                return False

            result = await asyncio.create_subprocess_exec(
                "open", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()

            return result.returncode == 0

        except Exception as e:
            logger.error(f"Failed to open settings for {permission_type.value}: {e}")
            return False

    def get_permission_info(
        self,
        permission_type: PermissionType
    ) -> Optional[PermissionInfo]:
        """Get information about a permission."""
        return PERMISSION_DEFINITIONS.get(permission_type)

    def get_required_permissions(self) -> List[PermissionType]:
        """Get list of required permissions."""
        return [
            pt for pt, info in PERMISSION_DEFINITIONS.items()
            if info.importance == PermissionImportance.REQUIRED
        ]

    def get_recommended_permissions(self) -> List[PermissionType]:
        """Get list of recommended permissions."""
        return [
            pt for pt, info in PERMISSION_DEFINITIONS.items()
            if info.importance == PermissionImportance.RECOMMENDED
        ]

    # =========================================================================
    # Permission Monitoring
    # =========================================================================

    async def start_monitoring(self) -> None:
        """Start monitoring permissions for changes."""
        if self._monitoring:
            return

        self._monitoring = True
        self._monitor_task = asyncio.create_task(
            self._monitoring_loop(),
            name="permission_monitor"
        )

        # Initialize event bus
        try:
            from .event_bus import get_macos_event_bus
            self._event_bus = await get_macos_event_bus()
        except Exception as e:
            logger.warning(f"Could not initialize event bus: {e}")

        logger.info("Permission monitoring started")

    async def stop_monitoring(self) -> None:
        """Stop monitoring permissions."""
        if not self._monitoring:
            return

        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        logger.info("Permission monitoring stopped")

    async def _monitoring_loop(self) -> None:
        """Background loop that monitors permissions."""
        while self._monitoring:
            try:
                await asyncio.sleep(self._check_interval)

                # Check all permissions
                for perm_type in PERMISSION_DEFINITIONS.keys():
                    old_result = self._cached_results.get(perm_type)
                    new_result = await self.check_permission(perm_type, use_cache=False)

                    # Check for changes
                    if old_result and old_result.status != new_result.status:
                        await self._on_permission_change(old_result, new_result)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Permission monitoring error: {e}")

    async def _on_permission_change(
        self,
        old_result: PermissionCheckResult,
        new_result: PermissionCheckResult
    ) -> None:
        """Handle permission status change."""
        logger.info(
            f"Permission changed: {new_result.permission_type.value} "
            f"{old_result.status.value} -> {new_result.status.value}"
        )

        # Call registered callbacks
        for callback in self._on_permission_changed:
            try:
                await callback(new_result)
            except Exception as e:
                logger.error(f"Permission change callback error: {e}")

        # Emit event
        if self._event_bus and self._emit_events:
            from .event_types import MacOSEventFactory
            event = MacOSEventFactory.create_permission_changed(
                permission_type=new_result.permission_type.value,
                status=new_result.status.value,
                previous_status=old_result.status.value,
            )
            await self._event_bus.emit(event)

    def on_permission_changed(
        self,
        callback: Callable[[PermissionCheckResult], Coroutine]
    ) -> None:
        """Register a callback for permission changes."""
        self._on_permission_changed.append(callback)

    # =========================================================================
    # Onboarding Support
    # =========================================================================

    async def generate_onboarding_steps(self) -> List[Dict[str, Any]]:
        """
        Generate a list of onboarding steps based on current permissions.

        Returns:
            List of steps with permission info and instructions
        """
        overview = await self.check_all_permissions()
        steps = []

        # First, required permissions
        for perm_type in self.get_required_permissions():
            result = overview.results.get(perm_type)
            info = PERMISSION_DEFINITIONS.get(perm_type)

            if result and result.status != PermissionStatus.GRANTED and info:
                steps.append({
                    "permission_type": perm_type.value,
                    "name": info.name,
                    "description": info.description,
                    "importance": info.importance.value,
                    "settings_path": info.settings_path,
                    "status": result.status.value,
                    "features_enabled": info.features_enabled,
                    "step_number": len(steps) + 1,
                })

        # Then, recommended permissions
        for perm_type in self.get_recommended_permissions():
            result = overview.results.get(perm_type)
            info = PERMISSION_DEFINITIONS.get(perm_type)

            if result and result.status != PermissionStatus.GRANTED and info:
                steps.append({
                    "permission_type": perm_type.value,
                    "name": info.name,
                    "description": info.description,
                    "importance": info.importance.value,
                    "settings_path": info.settings_path,
                    "status": result.status.value,
                    "features_enabled": info.features_enabled,
                    "step_number": len(steps) + 1,
                })

        return steps

    def get_stats(self) -> Dict[str, Any]:
        """Get permission manager statistics."""
        granted = sum(
            1 for r in self._cached_results.values()
            if r.status == PermissionStatus.GRANTED
        )
        denied = sum(
            1 for r in self._cached_results.values()
            if r.status == PermissionStatus.DENIED
        )

        return {
            "total_permissions": len(PERMISSION_DEFINITIONS),
            "granted": granted,
            "denied": denied,
            "monitoring": self._monitoring,
            "last_full_check": self._last_full_check.isoformat() if self._last_full_check else None,
        }


# =============================================================================
# Singleton Pattern
# =============================================================================

_permission_manager: Optional[PermissionManager] = None


async def get_permission_manager(
    auto_start_monitoring: bool = False,
) -> PermissionManager:
    """
    Get the global permission manager instance.

    Args:
        auto_start_monitoring: Automatically start monitoring

    Returns:
        The PermissionManager singleton
    """
    global _permission_manager

    if _permission_manager is None:
        _permission_manager = PermissionManager()

    if auto_start_monitoring and not _permission_manager._monitoring:
        await _permission_manager.start_monitoring()

    return _permission_manager
