#!/usr/bin/env python3
"""
macOS Native Space Detection using Accessibility and Window APIs
Provides accurate detection of spaces, windows, and applications
"""

import os
import json
import subprocess
import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
# v262.0: Gate PyObjC imports behind headless detection (prevents SIGABRT).
def _is_gui_session() -> bool:
    """Check for macOS GUI session without loading PyObjC."""
    _cached = os.environ.get("_JARVIS_GUI_SESSION")
    if _cached is not None:
        return _cached == "1"
    import sys as _sys
    result = False
    if _sys.platform == "darwin":
        if os.environ.get("JARVIS_HEADLESS", "").lower() in ("1", "true", "yes"):
            pass
        elif os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
            pass
        else:
            try:
                import ctypes
                cg = ctypes.cdll.LoadLibrary(
                    "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
                )
                cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
                result = cg.CGSessionCopyCurrentDictionary() is not None
            except Exception:
                pass
    os.environ["_JARVIS_GUI_SESSION"] = "1" if result else "0"
    return result

MACOS_NATIVE_AVAILABLE = False
Quartz = None  # type: ignore[assignment]
AppKit = None  # type: ignore[assignment]
objc = None  # type: ignore[assignment]
NSWorkspace = None  # type: ignore[assignment]
NSScreen = None  # type: ignore[assignment]
NSApplication = None  # type: ignore[assignment]
CGWindowListCopyWindowInfo = None
kCGWindowListOptionAll = None
kCGWindowListExcludeDesktopElements = None
kCGNullWindowID = None
CGWindowListCreateImage = None
CGRectNull = None
kCGWindowImageDefault = None
kCGWindowImageBoundsIgnoreFraming = None
kCGWindowImageNominalResolution = None

if _is_gui_session():
    try:
        import Quartz as _Quartz  # type: ignore[no-redef]
        from Quartz import (
            CGWindowListCopyWindowInfo as _CWLCWI,
            kCGWindowListOptionAll as _kWLOA,
            kCGWindowListExcludeDesktopElements as _kWLEDE,
            kCGNullWindowID as _kNWID,
            CGWindowListCreateImage as _CWLCI,
            CGRectNull as _CRN,
            kCGWindowImageDefault as _kWID,
            kCGWindowImageBoundsIgnoreFraming as _kWIBIF,
            kCGWindowImageNominalResolution as _kWINR,
        )
        import AppKit as _AppKit  # type: ignore[no-redef]
        from AppKit import (
            NSWorkspace as _NSWorkspace,
            NSScreen as _NSScreen,
            NSApplication as _NSApplication,
        )
        import objc as _objc  # type: ignore[no-redef]
        Quartz = _Quartz
        AppKit = _AppKit
        objc = _objc
        NSWorkspace = _NSWorkspace
        NSScreen = _NSScreen
        NSApplication = _NSApplication
        CGWindowListCopyWindowInfo = _CWLCWI
        kCGWindowListOptionAll = _kWLOA
        kCGWindowListExcludeDesktopElements = _kWLEDE
        kCGNullWindowID = _kNWID
        CGWindowListCreateImage = _CWLCI
        CGRectNull = _CRN
        kCGWindowImageDefault = _kWID
        kCGWindowImageBoundsIgnoreFraming = _kWIBIF
        kCGWindowImageNominalResolution = _kWINR
        MACOS_NATIVE_AVAILABLE = True
    except (ImportError, RuntimeError):
        pass

logger = logging.getLogger(__name__)
_PRIVATE_API_WARNING_EMITTED = False


def _warn_private_api_once(message: str) -> None:
    """Log private API bootstrap warnings once per process."""
    global _PRIVATE_API_WARNING_EMITTED
    if _PRIVATE_API_WARNING_EMITTED:
        logger.debug(message)
        return
    _PRIVATE_API_WARNING_EMITTED = True
    logger.warning(message)

@dataclass
class SpaceInfo:
    """Information about a macOS space"""
    space_id: int
    space_uuid: str
    space_name: str  # Actual workspace name
    display_id: int
    is_current: bool
    windows: List[Dict[str, Any]]
    applications: List[str]
    primary_app: Optional[str]
    activity_type: str
    last_active: datetime

@dataclass
class WindowInfo:
    """Detailed window information"""
    window_id: int
    title: str
    app_name: str
    bounds: Dict[str, float]
    space_id: int
    is_visible: bool
    is_focused: bool
    layer: int
    opacity: float

class MacOSSpaceDetector:
    """
    Native macOS space detection using system APIs
    """

    def __init__(self):
        self.workspace = NSWorkspace.sharedWorkspace() if NSWorkspace else None
        self.screens = NSScreen.screens() if NSScreen else []
        self._private_api_available = False
        self._cgs_copy_managed_display_spaces = None
        self._cgs_get_active_space = None
        self._init_private_apis()
        self._space_cache = {}
        self._window_cache = {}
        logger.info(
            "MacOS Space Detector initialized (native=%s, private=%s)",
            MACOS_NATIVE_AVAILABLE,
            self._private_api_available,
        )

    def _init_private_apis(self):
        """Initialize private macOS APIs for space management"""
        self.cgs_connection = None

        if objc is None:
            _warn_private_api_once("Could not load private APIs, using fallback: PyObjC bridge unavailable")
            return

        try:
            # Load private framework for space management.
            # Use a dedicated symbol map (not globals) so capability checks are explicit.
            symbol_map: Dict[str, Any] = {}
            bundle = objc.loadBundle(
                "CoreGraphics",
                symbol_map,
                bundle_path="/System/Library/Frameworks/CoreGraphics.framework",
            )

            objc.loadBundleFunctions(
                bundle,
                symbol_map,
                [
                    ("CGSCopySpaces", b"@ii"),
                    ("CGSCopySpacesForWindows", b"@ii@"),
                    ("CGSSpaceGetType", b"iii"),
                    ("CGSGetActiveSpace", b"ii"),
                    ("CGSCopyManagedDisplaySpaces", b"@i"),
                    ("CGSGetWindowCount", b"ii^i"),
                    ("CGSGetOnScreenWindowList", b"iii^i^i"),
                    ("CGSMainConnectionID", b"i"),
                ],
            )

            self._cgs_copy_managed_display_spaces = symbol_map.get("CGSCopyManagedDisplaySpaces")
            self._cgs_get_active_space = symbol_map.get("CGSGetActiveSpace")
            main_connection_fn = symbol_map.get("CGSMainConnectionID")

            if callable(main_connection_fn):
                self.cgs_connection = int(main_connection_fn())
            else:
                # Connection 0 is accepted by many CGS calls and avoids hard coupling
                # to private selector APIs that vary by PyObjC build.
                self.cgs_connection = 0

            self._private_api_available = callable(self._cgs_copy_managed_display_spaces)
            if not self._private_api_available:
                _warn_private_api_once(
                    "Could not load private APIs, using fallback: CGSCopyManagedDisplaySpaces unavailable"
                )
                self.cgs_connection = None

        except Exception as e:
            _warn_private_api_once(f"Could not load private APIs, using fallback: {e}")
            self.cgs_connection = None
            self._private_api_available = False

    def get_all_spaces(self) -> List[SpaceInfo]:
        """
        Get information about all spaces across all displays
        """
        spaces = []

        try:
            if self._private_api_available and self.cgs_connection is not None:
                # Use private APIs for accurate space detection
                spaces_data = self._get_spaces_via_private_api()
                if not spaces_data:
                    spaces_data = self._get_spaces_via_applescript()
            else:
                # Fallback to AppleScript/public APIs
                spaces_data = self._get_spaces_via_applescript()

            # Enrich with window information
            for space_data in spaces_data:
                space_info = self._create_space_info(space_data)
                spaces.append(space_info)

        except Exception as e:
            logger.error(f"Error getting spaces: {e}")
            # Fallback to basic detection
            spaces = self._fallback_space_detection()

        return spaces

    def _get_spaces_via_private_api(self) -> List[Dict]:
        """Use private APIs to get space information"""
        spaces = []

        try:
            if not self._private_api_available or not callable(self._cgs_copy_managed_display_spaces):
                return spaces
            # Get managed display spaces
            display_spaces = self._cgs_copy_managed_display_spaces(self.cgs_connection)

            for display_dict in display_spaces:
                display_id = display_dict.get('Display Identifier')
                current_space = display_dict.get('Current Space')
                space_list = display_dict.get('Spaces', [])

                for space_dict in space_list:
                    space_id = space_dict.get('id64', space_dict.get('ManagedSpaceID'))
                    space_uuid = space_dict.get('uuid', str(space_id))

                    spaces.append({
                        'space_id': space_id,
                        'space_uuid': space_uuid,
                        'display_id': display_id,
                        'is_current': space_id == current_space.get('id64'),
                        'type': space_dict.get('type', 0)
                    })

        except Exception as e:
            logger.error(f"Private API error: {e}")

        return spaces

    def _get_spaces_via_applescript(self) -> List[Dict]:
        """Use AppleScript to get space information including workspace names"""
        spaces = []

        # Enhanced AppleScript to capture actual workspace names
        script = """
        tell application "System Events"
            try
                -- Get Mission Control preferences for space names
                set spacesList to {}

                -- Try to get space information from Mission Control
                tell application "Mission Control" to launch
                delay 0.5

                -- Get spaces from Dock's Mission Control
                tell application process "Dock"
                    tell group 1
                        set spaceElements to every group whose role is "AXGroup"
                        repeat with spaceElement in spaceElements
                            try
                                set spaceName to description of spaceElement
                                if spaceName is missing value then
                                    set spaceName to title of spaceElement
                                end if
                                set end of spacesList to spaceName as string
                            on error
                                set end of spacesList to "Desktop"
                            end try
                        end repeat
                    end tell
                end tell

                -- Close Mission Control
                key code 53 -- ESC key

                return spacesList
            on error errMsg
                return {}
            end try
        end tell
        """

        try:
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0 and result.stdout.strip():
                # Parse space names from output
                space_names = result.stdout.strip().split(',')
                for i, name in enumerate(space_names):
                    space_name = name.strip()
                    if not space_name or space_name == "missing value":
                        space_name = self._get_space_name_from_windows(i + 1)

                    spaces.append({
                        'space_id': i + 1,
                        'space_uuid': f"space_{i + 1}",
                        'space_name': space_name,
                        'display_id': 1,
                        'is_current': False,  # Will be determined separately
                        'type': 0
                    })
            else:
                # Fallback: determine space names from window analysis
                spaces = self._get_spaces_from_window_analysis()

        except Exception as e:
            logger.error(f"AppleScript error: {e}")
            # Fallback to window-based detection
            spaces = self._get_spaces_from_window_analysis()

        return spaces

    def _get_space_name_from_windows(self, space_id: int) -> str:
        """Dynamically determine space name from its windows and applications"""
        windows = self._get_windows_for_space(space_id)

        if not windows:
            return f"Desktop {space_id}"

        # Get the primary application in this space
        primary_app = self._determine_primary_app(windows)

        if primary_app:
            # Clean up app names for display
            app_name_map = {
                'Google Chrome': 'Google Chrome',
                'Safari': 'Safari',
                'Code': 'Code',
                'Cursor': 'Cursor',
                'Terminal': 'Terminal',
                'iTerm2': 'iTerm',
                'Finder': 'Finder',
                'Slack': 'Slack',
                'Discord': 'Discord',
                'Messages': 'Messages',
                'Mail': 'Mail',
                'Notes': 'Notes',
                'Preview': 'Preview',
                'Xcode': 'Xcode',
                'IntelliJ IDEA': 'IntelliJ'
            }

            # Check for JARVIS-specific windows
            for window in windows:
                window_name = window.get('kCGWindowName', '').lower()
                if 'jarvis' in window_name or 'j.a.r.v.i.s' in window_name:
                    return 'J.A.R.V.I.S. interface'

            # Return mapped name or original
            return app_name_map.get(primary_app, primary_app)

        # Check window titles for context
        for window in windows:
            title = window.get('kCGWindowName', '')
            if title:
                # Extract meaningful context from window title
                if 'jarvis' in title.lower():
                    return 'J.A.R.V.I.S. interface'
                elif any(keyword in title.lower() for keyword in ['code', 'editor', 'ide']):
                    return 'Development'
                elif any(keyword in title.lower() for keyword in ['terminal', 'shell', 'console']):
                    return 'Terminal'

        return f"Desktop {space_id}"

    def _get_spaces_from_window_analysis(self) -> List[Dict]:
        """Fallback method to determine spaces from window analysis"""
        spaces = []

        # Get all windows
        window_list = CGWindowListCopyWindowInfo(
            kCGWindowListOptionAll,
            kCGNullWindowID
        )

        # Group windows by visibility and position to estimate spaces
        visible_windows = []
        hidden_windows = []

        for window in window_list:
            if window.get('kCGWindowLayer', 0) == 0:  # Normal windows
                if window.get('kCGWindowIsOnscreen', False):
                    visible_windows.append(window)
                else:
                    hidden_windows.append(window)

        # Create at least one space for visible windows
        if visible_windows:
            space_name = self._determine_space_name_from_windows(visible_windows)
            spaces.append({
                'space_id': 1,
                'space_uuid': 'space_1',
                'space_name': space_name,
                'display_id': 1,
                'is_current': True,
                'type': 0
            })

        # Try to identify additional spaces from application groupings
        app_groups = self._group_windows_by_application(hidden_windows)
        space_id = 2

        for app_name, windows in app_groups.items():
            if windows:  # Only create space if there are windows
                spaces.append({
                    'space_id': space_id,
                    'space_uuid': f'space_{space_id}',
                    'space_name': app_name,
                    'display_id': 1,
                    'is_current': False,
                    'type': 0
                })
                space_id += 1

                if space_id > 9:  # Limit to reasonable number of spaces
                    break

        return spaces if spaces else [{
            'space_id': 1,
            'space_uuid': 'space_1',
            'space_name': 'Desktop',
            'display_id': 1,
            'is_current': True,
            'type': 0
        }]

    def _determine_space_name_from_windows(self, windows: List[Dict]) -> str:
        """Determine space name from a list of windows"""
        if not windows:
            return "Desktop"

        # Get primary app
        primary_app = self._determine_primary_app(windows)

        # Check for special cases
        for window in windows:
            window_name = window.get('kCGWindowName', '').lower()
            if 'jarvis' in window_name or 'j.a.r.v.i.s' in window_name:
                return 'J.A.R.V.I.S. interface'

        return primary_app if primary_app else "Desktop"

    def _group_windows_by_application(self, windows: List[Dict]) -> Dict[str, List[Dict]]:
        """Group windows by their application"""
        app_groups = {}

        for window in windows:
            app_name = window.get('kCGWindowOwnerName', 'Unknown')
            if app_name not in app_groups:
                app_groups[app_name] = []
            app_groups[app_name].append(window)

        return app_groups

    def _fallback_space_detection(self) -> List[SpaceInfo]:
        """Fallback detection using window positions"""
        spaces = []

        # Get all windows
        window_list = CGWindowListCopyWindowInfo(
            kCGWindowListOptionAll,
            kCGNullWindowID
        )

        # Group windows by estimated space
        space_windows = {}

        for window in window_list:
            if window.get('kCGWindowLayer', 0) == 0:  # Normal windows
                # Estimate space based on visibility and position
                is_visible = window.get('kCGWindowIsOnscreen', False)
                bounds = window.get('kCGWindowBounds', {})

                # Simple heuristic: visible windows are on current space
                space_id = 1 if is_visible else 2

                if space_id not in space_windows:
                    space_windows[space_id] = []

                space_windows[space_id].append(window)

        # Create SpaceInfo objects
        for space_id, windows in space_windows.items():
            space_name = self._determine_space_name_from_windows(windows)
            space_info = SpaceInfo(
                space_id=space_id,
                space_uuid=f"space_{space_id}",
                space_name=space_name,
                display_id=1,
                is_current=(space_id == 1),
                windows=windows,
                applications=list(set([w.get('kCGWindowOwnerName', 'Unknown') for w in windows])),
                primary_app=self._determine_primary_app(windows),
                activity_type=self._determine_activity_type(windows),
                last_active=datetime.now()
            )
            spaces.append(space_info)

        return spaces

    def _create_space_info(self, space_data: Dict) -> SpaceInfo:
        """Create a SpaceInfo object with enriched data"""
        space_id = space_data['space_id']

        # Get windows for this space
        windows = self._get_windows_for_space(space_id)

        # Extract application names
        applications = list(set([w.get('kCGWindowOwnerName', 'Unknown') for w in windows]))

        # Get space name dynamically if not provided
        space_name = space_data.get('space_name')
        if not space_name:
            space_name = self._determine_space_name_from_windows(windows)

        return SpaceInfo(
            space_id=space_id,
            space_uuid=space_data['space_uuid'],
            space_name=space_name,
            display_id=space_data['display_id'],
            is_current=space_data['is_current'],
            windows=windows,
            applications=applications,
            primary_app=self._determine_primary_app(windows),
            activity_type=self._determine_activity_type(windows),
            last_active=datetime.now()
        )

    def _get_windows_for_space(self, space_id: int) -> List[Dict]:
        """Get all windows in a specific space"""
        all_windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionAll,
            kCGNullWindowID
        )

        space_windows = []
        for window in all_windows:
            # Filter windows by space (this is an approximation)
            # In reality, we'd need private APIs to get exact space assignment
            if self._is_window_in_space(window, space_id):
                space_windows.append(window)

        return space_windows

    def _is_window_in_space(self, window: Dict, space_id: int) -> bool:
        """Determine if a window belongs to a specific space"""
        # This is a heuristic - accurate detection requires private APIs
        is_visible = window.get('kCGWindowIsOnscreen', False)

        if space_id == 1:  # Assume space 1 is current
            return is_visible
        else:
            return not is_visible and window.get('kCGWindowLayer', 0) == 0

    def _determine_primary_app(self, windows: List[Dict]) -> Optional[str]:
        """Determine the primary application in a space"""
        if not windows:
            return None

        # Count windows per application
        app_counts = {}
        for window in windows:
            app = window.get('kCGWindowOwnerName', 'Unknown')
            app_counts[app] = app_counts.get(app, 0) + 1

        # Return app with most windows
        return max(app_counts, key=app_counts.get)

    def _determine_activity_type(self, windows: List[Dict]) -> str:
        """Determine the type of activity in a space"""
        if not windows:
            return 'idle'

        apps = [w.get('kCGWindowOwnerName', '').lower() for w in windows]

        # Categorize based on applications
        if any('code' in app or 'cursor' in app or 'xcode' in app for app in apps):
            return 'development'
        elif any('chrome' in app or 'safari' in app or 'firefox' in app for app in apps):
            return 'browsing'
        elif any('terminal' in app or 'iterm' in app for app in apps):
            return 'terminal'
        elif any('slack' in app or 'discord' in app or 'messages' in app for app in apps):
            return 'communication'
        elif any('finder' in app for app in apps):
            return 'file_management'
        else:
            return 'general'

    def capture_space_screenshot(self, space_id: int) -> Optional[Any]:
        """Capture a screenshot of a specific space"""
        try:
            # Get windows for the space
            windows = self._get_windows_for_space(space_id)

            if not windows:
                return None

            # Get window IDs
            window_ids = [w.get('kCGWindowID', 0) for w in windows if w.get('kCGWindowID')]

            if window_ids:
                # Capture composite image of all windows
                image = CGWindowListCreateImage(
                    CGRectNull,
                    kCGWindowListOptionAll,
                    window_ids[0],  # Primary window
                    kCGWindowImageDefault | kCGWindowImageBoundsIgnoreFraming
                )
                return image

        except Exception as e:
            logger.error(f"Error capturing space {space_id}: {e}")

        return None

    def get_current_space(self) -> Optional[SpaceInfo]:
        """Get information about the current active space"""
        spaces = self.get_all_spaces()
        for space in spaces:
            if space.is_current:
                return space
        return None

    def monitor_space_changes(self, callback):
        """Monitor for space change events"""
        # Set up notification observer for space changes
        if not NSWorkspace:
            logger.warning("Space change monitoring unavailable: NSWorkspace not available")
            return
        notification_center = NSWorkspace.sharedWorkspace().notificationCenter()

        notification_center.addObserverForName_object_queue_usingBlock_(
            'NSWorkspaceActiveSpaceDidChangeNotification',
            None,
            None,
            lambda notification: callback(self.get_current_space())
        )

        logger.info("Space change monitoring enabled")

# Integration helper for existing code
class SpaceDetectorAdapter:
    """Adapter to integrate with existing multi-space code"""

    def __init__(self):
        self.detector = MacOSSpaceDetector()

    def detect_spaces_and_windows(self) -> Dict[str, Any]:
        """Get spaces in format expected by existing code"""
        spaces = self.detector.get_all_spaces()

        result = {
            'spaces': {},
            'current_space': None,
            'total_spaces': len(spaces)
        }

        for space in spaces:
            result['spaces'][space.space_id] = {
                'space_name': space.space_name,  # Include the actual workspace name
                'windows': space.windows,
                'applications': space.applications,
                'primary_app': space.primary_app,
                'activity_type': space.activity_type,
                'is_current': space.is_current
            }

            if space.is_current:
                result['current_space'] = space.space_id
                result['current_space_name'] = space.space_name

        return result
