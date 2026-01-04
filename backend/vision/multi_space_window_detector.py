#!/usr/bin/env python3
"""
Multi-Space Window Detector for JARVIS
Enhanced window detection with space awareness and metadata intelligence
"""

import os
import Quartz
import AppKit
import time
import asyncio
import subprocess
import json
from typing import List, Dict, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from collections import defaultdict
from enum import Enum

logger = logging.getLogger(__name__)


class SpaceWindowVisibility(Enum):
    """Window visibility states across spaces"""

    VISIBLE_CURRENT_SPACE = "visible_current"
    VISIBLE_OTHER_SPACE = "visible_other"
    MINIMIZED = "minimized"
    HIDDEN = "hidden"
    FULLSCREEN = "fullscreen"


@dataclass
class SpaceInfo:
    """Information about a desktop space"""

    space_id: int
    space_uuid: str
    display_id: int
    is_current: bool
    window_count: int
    last_accessed: Optional[datetime] = None
    cached_screenshot: Optional[Any] = None
    screenshot_timestamp: Optional[datetime] = None


@dataclass
class EnhancedWindowInfo:
    """Enhanced window information with space awareness"""

    # Basic info
    window_id: int
    app_name: str
    window_title: str
    process_id: int

    # Space info
    space_id: Optional[int] = None
    space_uuid: Optional[str] = None
    visibility: SpaceWindowVisibility = SpaceWindowVisibility.HIDDEN

    # Position and state
    bounds: Dict[str, int] = field(default_factory=dict)  # x, y, width, height
    is_minimized: bool = False
    is_fullscreen: bool = False
    is_focused: bool = False
    layer: int = 0
    alpha: float = 1.0

    # Enhanced metadata
    document_path: Optional[str] = None
    document_modified: Optional[datetime] = None
    last_activated: Optional[datetime] = None

    # Workspace context
    related_windows: List[int] = field(default_factory=list)
    workspace_role: Optional[str] = None  # "main", "reference", "tool", etc.

    def to_context_string(self) -> str:
        """Generate natural language context about the window"""
        context_parts = [f"{self.app_name}"]

        if self.window_title:
            context_parts.append(f'"{self.window_title}"')

        if self.document_path:
            context_parts.append(f"editing {self.document_path}")

        if self.is_fullscreen:
            context_parts.append("in fullscreen")
        elif self.is_minimized:
            context_parts.append("minimized")

        return " ".join(context_parts)


class MultiSpaceWindowDetector:
    """Enhanced window detector with full multi-space awareness"""

    def __init__(self):
        self.last_update = 0
        self.update_interval = 0.5

        # Caches
        self.windows_cache: Dict[int, EnhancedWindowInfo] = {}
        self.spaces_cache: Dict[int, SpaceInfo] = {}
        self.space_windows_map: Dict[int, Set[int]] = defaultdict(set)

        # State tracking
        self.current_space_id: Optional[int] = None
        self.current_space_uuid: Optional[str] = None

        # Screenshot cache
        self.screenshot_cache: Dict[int, Tuple[Any, datetime]] = {}
        self.cache_ttl = timedelta(minutes=5)

    def get_all_windows_across_spaces(self) -> Dict[str, Any]:
        """Get comprehensive window information across all spaces"""

        # Try Yabai first for most accurate data
        try:
            from .yabai_space_detector import get_yabai_detector

            yabai_detector = get_yabai_detector()
            if yabai_detector.is_available():
                yabai_summary = yabai_detector.get_workspace_summary()

                if yabai_summary and yabai_summary["total_spaces"] > 0:
                    # Convert Yabai data to our format
                    enhanced_spaces = []
                    spaces_dict = {}
                    windows = []

                    for yabai_space in yabai_summary["spaces"]:
                        space_id = yabai_space["space_id"]

                        # Create space info
                        space_info = {
                            "space_id": space_id,
                            "space_name": yabai_space.get(
                                "space_name", f"Desktop {space_id}"
                            ),
                            "primary_app": yabai_space.get("primary_activity", "Empty"),
                            "applications": yabai_space.get("applications", []),
                            "window_count": yabai_space.get("window_count", 0),
                            "is_current": yabai_space.get("is_current", False),
                            "is_fullscreen": yabai_space.get("is_fullscreen", False),
                            "windows": yabai_space.get("windows", []),
                        }

                        enhanced_spaces.append(space_info)
                        spaces_dict[space_id] = space_info

                        # Add windows from this space
                        for window in yabai_space.get("windows", []):
                            window_info = EnhancedWindowInfo(
                                window_id=window.get("id", 0),
                                app_name=window.get("app", "Unknown"),
                                window_title=window.get("title", ""),
                                process_id=0,  # Not available from Yabai
                                space_id=space_id,
                                visibility=(
                                    SpaceWindowVisibility.VISIBLE_CURRENT_SPACE
                                    if yabai_space.get("is_current")
                                    else SpaceWindowVisibility.VISIBLE_OTHER_SPACE
                                ),
                            )
                            windows.append(window_info)

                    # Update caches
                    self.windows_cache = {w.window_id: w for w in windows}
                    current_space = yabai_summary.get("current_space")
                    self.current_space_id = (
                        current_space.get("space_id", 1) if current_space else 1
                    )

                    logger.info(
                        f"[MULTI-SPACE] Using Yabai data: {len(enhanced_spaces)} spaces, {len(windows)} windows"
                    )

                    # Calculate totals for response generation
                    all_applications = set()
                    for space in enhanced_spaces:
                        all_applications.update(space.get("applications", []))

                    return {
                        "current_space": {
                            "id": self.current_space_id,
                            "uuid": self.current_space_uuid or "",
                            "window_count": len(
                                [
                                    w
                                    for w in windows
                                    if w.space_id == self.current_space_id
                                ]
                            ),
                        },
                        "spaces": spaces_dict,
                        "spaces_list": enhanced_spaces,
                        "space_details": enhanced_spaces,  # Alias for response generator
                        "windows": windows,
                        "space_window_map": {
                            space_id: [
                                w.window_id for w in windows if w.space_id == space_id
                            ]
                            for space_id in spaces_dict.keys()
                        },
                        # Add totals for response generation
                        "total_spaces": len(enhanced_spaces),
                        "total_windows": len(windows),
                        "total_applications": len(all_applications),
                        "total_apps": len(all_applications),  # Alias
                        "timestamp": datetime.now().isoformat(),
                    }

        except Exception as e:
            logger.warning(f"[MULTI-SPACE] Yabai integration failed: {e}")

        # Fallback to original detection
        # Get current space info first
        self._update_current_space()

        # Get all windows with enhanced metadata
        windows = self._get_enhanced_windows()

        # Get space information
        spaces = self._get_space_info()

        # Map windows to spaces
        self._map_windows_to_spaces(windows, spaces)

        # Enhance spaces with workspace names and application info
        enhanced_spaces = []
        spaces_dict = {}  # Also keep dict format for compatibility

        for space in spaces:
            space_id = space.space_id

            # Get windows for this space
            space_window_ids = self.space_windows_map.get(space_id, set())
            space_windows = [w for w in windows if w.window_id in space_window_ids]

            # Get applications on this space
            applications = list(
                set(
                    w.app_name
                    for w in space_windows
                    if w.app_name and w.app_name != "Unknown"
                )
            )

            # Determine primary app (most windows or first)
            app_counts = {}
            for w in space_windows:
                if w.app_name:
                    app_counts[w.app_name] = app_counts.get(w.app_name, 0) + 1
            primary_app = (
                max(app_counts.keys(), key=app_counts.get) if app_counts else None
            )

            # Determine workspace name based on applications
            workspace_name = self._determine_workspace_name(
                primary_app, applications, space_windows
            )

            # Create enhanced space info dict
            space_info = {
                "space_id": space_id,
                "space_name": workspace_name,
                "primary_app": primary_app,
                "applications": applications,
                "window_count": len(space_window_ids),
                "is_current": space.is_current,
                "windows": [self._window_to_dict(w) for w in space_windows],
            }

            # Add to both list and dict
            enhanced_spaces.append(space_info)
            spaces_dict[space_id] = space_info

            logger.info(
                f"[MULTI-SPACE] Space {space_id}: '{workspace_name}' with apps {applications[:3] if applications else []}"
            )

        # Calculate totals for response generation
        all_applications = set()
        for space in enhanced_spaces:
            all_applications.update(space.get("applications", []))
        
        # Build comprehensive result with both list (for compatibility) and dict
        result = {
            "current_space": {
                "id": self.current_space_id,
                "uuid": self.current_space_uuid,
                "window_count": len(
                    self.space_windows_map.get(self.current_space_id, set())
                ),
            },
            "spaces": spaces_dict,  # Dict format for workspace name processing
            "spaces_list": enhanced_spaces,  # List format for compatibility
            "space_details": enhanced_spaces,  # Alias for response generator
            "windows": windows,
            "space_window_map": {
                space_id: list(window_ids)
                for space_id, window_ids in self.space_windows_map.items()
            },
            # Add totals for response generation
            "total_spaces": len(enhanced_spaces),
            "total_windows": len(windows),
            "total_applications": len(all_applications),
            "total_apps": len(all_applications),  # Alias
            "timestamp": datetime.now().isoformat(),
        }

        # Update caches
        self.windows_cache = {w.window_id: w for w in windows}
        self.last_update = time.time()

        return result

    async def get_all_windows_across_spaces_async(
        self,
        timeout_seconds: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        v40.0.0: Async version using parallel workspace query.

        This is the RECOMMENDED method for async contexts as it:
        - Runs spaces + windows queries in parallel
        - Has intelligent timeout management
        - Uses circuit breaker for resilience
        - Returns cached data on failure

        Args:
            timeout_seconds: Maximum time to wait (default: from env or 3.0s)

        Returns:
            Comprehensive window information across all spaces
        """
        try:
            from .yabai_space_detector import parallel_workspace_query_async

            # Use the new parallel async query
            yabai_summary = await parallel_workspace_query_async(
                timeout_seconds=timeout_seconds
            )

            if yabai_summary and yabai_summary.get("total_spaces", 0) > 0:
                # Convert Yabai data to our format
                enhanced_spaces = []
                spaces_dict = {}
                windows = []

                for yabai_space in yabai_summary.get("spaces", []):
                    space_id = yabai_space.get("space_id", 1)

                    # Create space info
                    space_info = {
                        "space_id": space_id,
                        "space_name": yabai_space.get(
                            "space_name", f"Desktop {space_id}"
                        ),
                        "primary_app": yabai_space.get("primary_activity", "Empty"),
                        "applications": yabai_space.get("applications", []),
                        "window_count": yabai_space.get("window_count", 0),
                        "is_current": yabai_space.get("is_current", False),
                        "is_fullscreen": yabai_space.get("is_fullscreen", False),
                        "windows": yabai_space.get("windows", []),
                    }

                    enhanced_spaces.append(space_info)
                    spaces_dict[space_id] = space_info

                    # Add windows from this space
                    for window in yabai_space.get("windows", []):
                        window_info = EnhancedWindowInfo(
                            window_id=window.get("id", 0),
                            app_name=window.get("app", "Unknown"),
                            window_title=window.get("title", ""),
                            process_id=0,
                            space_id=space_id,
                            visibility=(
                                SpaceWindowVisibility.VISIBLE_CURRENT_SPACE
                                if yabai_space.get("is_current")
                                else SpaceWindowVisibility.VISIBLE_OTHER_SPACE
                            ),
                        )
                        windows.append(window_info)

                # Update caches
                self.windows_cache = {w.window_id: w for w in windows}
                current_space = yabai_summary.get("current_space")
                self.current_space_id = (
                    current_space.get("space_id", 1) if current_space else 1
                )
                self.last_update = time.time()

                logger.info(
                    f"[MULTI-SPACE] Async parallel query: {len(enhanced_spaces)} spaces, "
                    f"{len(windows)} windows ({yabai_summary.get('query_method', 'unknown')})"
                )

                # Calculate totals for response generation
                all_applications = set()
                for space in enhanced_spaces:
                    all_applications.update(space.get("applications", []))

                return {
                    "current_space": {
                        "id": self.current_space_id,
                        "uuid": self.current_space_uuid or "",
                        "window_count": len(
                            [w for w in windows if w.space_id == self.current_space_id]
                        ),
                    },
                    "spaces": spaces_dict,
                    "spaces_list": enhanced_spaces,
                    "space_details": enhanced_spaces,
                    "windows": windows,
                    "space_window_map": {
                        space_id: [w.window_id for w in windows if w.space_id == space_id]
                        for space_id in spaces_dict.keys()
                    },
                    "total_spaces": len(enhanced_spaces),
                    "total_windows": len(windows),
                    "total_applications": len(all_applications),
                    "total_apps": len(all_applications),
                    "timestamp": datetime.now().isoformat(),
                    "query_method": yabai_summary.get("query_method", "async"),
                }

            # Return error info if available
            error = yabai_summary.get("error", "No spaces found")
            logger.warning(f"[MULTI-SPACE] Async query returned no data: {error}")

            return {
                "current_space": {"id": 1, "uuid": "", "window_count": 0},
                "spaces": {},
                "spaces_list": [],
                "space_details": [],
                "windows": [],
                "space_window_map": {},
                "total_spaces": 0,
                "total_windows": 0,
                "total_applications": 0,
                "total_apps": 0,
                "timestamp": datetime.now().isoformat(),
                "error": error,
                "query_method": "fallback",
            }

        except Exception as e:
            logger.error(f"[MULTI-SPACE] Async parallel query failed: {e}")
            return {
                "current_space": {"id": 1, "uuid": "", "window_count": 0},
                "spaces": {},
                "spaces_list": [],
                "space_details": [],
                "windows": [],
                "space_window_map": {},
                "total_spaces": 0,
                "total_windows": 0,
                "total_applications": 0,
                "total_apps": 0,
                "timestamp": datetime.now().isoformat(),
                "error": str(e),
                "query_method": "error",
            }

    async def get_all_visible_spaces(self) -> List[int]:
        """
        v60.0: PANOPTICON PROTOCOL - Multi-Monitor + Shadow Realm Visibility

        Get ALL visible space IDs across ALL displays, including the Ghost Display
        (Shadow Realm) even if yabai reports it as not visible.

        WHY THIS MATTERS:
        =================
        - Single monitor: Only 1 space is visible at a time
        - Multi-monitor: MULTIPLE spaces can be visible simultaneously (one per display)
        - Virtual monitors (BetterDisplay): Additional visible spaces for background capture
        - v60.0: Ghost Display spaces may report is_visible=False but ARE capturable

        THE PANOPTICON INSIGHT:
        =======================
        Exile ≠ Death. Windows teleported to the Shadow Realm (Display 2+) are still
        ALIVE and CAPTURABLE. Yabai may report these spaces as "not visible" because
        they're on a virtual display, but JARVIS can still see them.

        SOLUTION:
        =========
        Include ALL spaces on Display 2+ (Ghost Display) regardless of visibility flag.
        This ensures windows on the Shadow Realm are not filtered out as "hidden."

        Returns:
            List of space IDs that JARVIS can see (visible OR on Ghost Display)
        """
        visible_space_ids = []
        ghost_display_index = int(os.getenv("JARVIS_SHADOW_DISPLAY", "2"))

        try:
            from .yabai_space_detector import get_yabai_detector

            yabai_detector = get_yabai_detector()
            if yabai_detector.is_available():
                # Use async version if available, fall back to sync
                if hasattr(yabai_detector, 'enumerate_all_spaces_async'):
                    spaces = await yabai_detector.enumerate_all_spaces_async(include_display_info=True)
                else:
                    # Run sync version in executor to avoid blocking
                    import asyncio
                    loop = asyncio.get_event_loop()
                    spaces = await loop.run_in_executor(
                        None,
                        lambda: yabai_detector.enumerate_all_spaces(include_display_info=True)
                    )

                ghost_spaces_found = 0
                for space in spaces:
                    space_id = space.get('space_id')
                    display_id = space.get('display', 1)
                    is_visible = space.get('is_visible', False)

                    if not space_id:
                        continue

                    # ═══════════════════════════════════════════════════════════════
                    # v60.0 PANOPTICON CLAUSE: Include space if:
                    # 1. Standard check: is_visible == True (normal visible space)
                    # 2. PANOPTICON: display >= ghost_display_index (Shadow Realm)
                    # ═══════════════════════════════════════════════════════════════
                    is_on_ghost_display = display_id >= ghost_display_index

                    if is_visible or is_on_ghost_display:
                        visible_space_ids.append(space_id)

                        if is_on_ghost_display and not is_visible:
                            ghost_spaces_found += 1
                            logger.debug(
                                f"[v60.0 PANOPTICON] 👁️ Shadow Realm Space {space_id} on Display {display_id} "
                                f"included despite is_visible=False"
                            )
                        else:
                            logger.debug(
                                f"[MULTI-MONITOR] Space {space_id} visible on Display {display_id}"
                            )

                if ghost_spaces_found > 0:
                    logger.info(
                        f"[v60.0 PANOPTICON] 👁️ Included {ghost_spaces_found} Shadow Realm space(s) in visibility list"
                    )

                logger.info(
                    f"[MULTI-MONITOR] Found {len(visible_space_ids)} capturable spaces: {visible_space_ids}"
                )

        except Exception as e:
            logger.warning(f"[MULTI-MONITOR] Failed to get visible spaces: {e}")
            # Fallback: Return current space only
            if self.current_space_id:
                visible_space_ids = [self.current_space_id]
                logger.info(f"[MULTI-MONITOR] Fallback to current space: {self.current_space_id}")

        return visible_space_ids

    def get_all_visible_spaces_sync(self) -> List[int]:
        """
        v60.0: Synchronous version of get_all_visible_spaces with PANOPTICON PROTOCOL.

        Includes Shadow Realm (Ghost Display) spaces even if yabai reports them
        as not visible, because they ARE capturable.
        """
        visible_space_ids = []
        ghost_display_index = int(os.getenv("JARVIS_SHADOW_DISPLAY", "2"))

        try:
            from .yabai_space_detector import get_yabai_detector

            yabai_detector = get_yabai_detector()
            if yabai_detector.is_available():
                spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)

                ghost_spaces_found = 0
                for space in spaces:
                    space_id = space.get('space_id')
                    display_id = space.get('display', 1)
                    is_visible = space.get('is_visible', False)

                    if not space_id:
                        continue

                    # ═══════════════════════════════════════════════════════════════
                    # v60.0 PANOPTICON CLAUSE: Include space if:
                    # 1. Standard check: is_visible == True (normal visible space)
                    # 2. PANOPTICON: display >= ghost_display_index (Shadow Realm)
                    # ═══════════════════════════════════════════════════════════════
                    is_on_ghost_display = display_id >= ghost_display_index

                    if is_visible or is_on_ghost_display:
                        visible_space_ids.append(space_id)

                        if is_on_ghost_display and not is_visible:
                            ghost_spaces_found += 1
                            logger.debug(
                                f"[v60.0 PANOPTICON SYNC] 👁️ Shadow Realm Space {space_id} on Display {display_id} "
                                f"included despite is_visible=False"
                            )

                if ghost_spaces_found > 0:
                    logger.info(
                        f"[v60.0 PANOPTICON SYNC] 👁️ Included {ghost_spaces_found} Shadow Realm space(s) in visibility list"
                    )

                logger.info(
                    f"[MULTI-MONITOR] Found {len(visible_space_ids)} capturable spaces: {visible_space_ids}"
                )

        except Exception as e:
            logger.warning(f"[MULTI-MONITOR] Failed to get visible spaces: {e}")
            if self.current_space_id:
                visible_space_ids = [self.current_space_id]

        return visible_space_ids

    def get_display_info(self) -> Dict[int, Dict[str, Any]]:
        """
        v22.0.0: Get information about all displays and their visible spaces.

        Returns:
            Dict mapping display_id -> {space_id, is_main, etc.}
        """
        displays = {}

        try:
            from .yabai_space_detector import get_yabai_detector

            yabai_detector = get_yabai_detector()
            if yabai_detector.is_available():
                spaces = yabai_detector.enumerate_all_spaces(include_display_info=True)

                for space in spaces:
                    if space.get('is_visible', False):
                        display_id = space.get('display', 1)
                        displays[display_id] = {
                            'space_id': space.get('space_id'),
                            'space_name': space.get('space_name'),
                            'window_count': space.get('window_count', 0),
                            'is_current': space.get('is_current', False),
                            'is_fullscreen': space.get('is_fullscreen', False),
                        }

                logger.debug(f"[MULTI-MONITOR] Display info: {displays}")

        except Exception as e:
            logger.warning(f"[MULTI-MONITOR] Failed to get display info: {e}")

        return displays

    def _determine_workspace_name(
        self, primary_app: str, applications: List[str], windows: List
    ) -> str:
        """Determine workspace name from applications and windows"""
        # Check for JARVIS first
        for w in windows:
            if (
                "jarvis" in w.window_title.lower()
                or "j.a.r.v.i.s" in w.window_title.lower()
            ):
                return "J.A.R.V.I.S. interface"

        # If Chrome is primary, it might be JARVIS
        if primary_app and "chrome" in primary_app.lower():
            return "J.A.R.V.I.S. interface"

        # Use primary app as workspace name if meaningful
        if primary_app:
            app_mappings = {
                "cursor": "Cursor",
                "code": "Code",
                "visual studio code": "Code",
                "terminal": "Terminal",
                "iterm": "Terminal",
                "messages": "Messages",
                "slack": "Slack",
                "discord": "Discord",
                "safari": "Safari",
                "finder": "Finder",
            }

            for key, value in app_mappings.items():
                if key in primary_app.lower():
                    return value

            return primary_app

        # Default fallback
        return f"Desktop {windows[0].space_id if windows and hasattr(windows[0], 'space_id') else ''}"

    def _window_to_dict(self, window: EnhancedWindowInfo) -> Dict:
        """Convert window object to dictionary"""
        return {
            "kCGWindowNumber": window.window_id,
            "kCGWindowOwnerName": window.app_name,
            "kCGWindowName": window.window_title,
            "kCGWindowOwnerPID": window.process_id,
            "kCGWindowLayer": window.layer,
            "kCGWindowAlpha": window.alpha,
        }

    def _get_enhanced_windows(self) -> List[EnhancedWindowInfo]:
        """Get all windows with enhanced metadata"""
        windows = []

        # Get window list with all options for maximum info
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )

        if not window_list:
            logger.warning("No windows found")
            return windows

        # Get the frontmost application for focus detection
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        frontmost_app = workspace.frontmostApplication()
        frontmost_pid = frontmost_app.processIdentifier() if frontmost_app else None

        # Process each window
        for window_dict in window_list:
            window = self._process_window_dict(window_dict, frontmost_pid)
            if window:
                # Try to get additional metadata
                self._enhance_window_metadata(window)
                windows.append(window)

        return windows

    def _process_window_dict(
        self, window_dict: Dict, frontmost_pid: int
    ) -> Optional[EnhancedWindowInfo]:
        """Process raw window dictionary into EnhancedWindowInfo"""
        # Extract basic info
        window_id = window_dict.get("kCGWindowNumber", 0)
        app_name = window_dict.get("kCGWindowOwnerName", "Unknown")
        window_title = window_dict.get("kCGWindowName", "")
        process_id = window_dict.get("kCGWindowOwnerPID", 0)
        layer = window_dict.get("kCGWindowLayer", 0)
        alpha = window_dict.get("kCGWindowAlpha", 0)

        # Skip system UI elements
        if app_name in [
            "Window Server",
            "SystemUIServer",
            "Dock",
            "Control Center",
            "Notification Center",
            "Wallpaper",
        ]:
            return None

        # Skip invisible windows
        if alpha == 0:
            return None

        # Get window bounds
        bounds_dict = window_dict.get("kCGWindowBounds", {})
        bounds = {
            "x": int(bounds_dict.get("X", 0)),
            "y": int(bounds_dict.get("Y", 0)),
            "width": int(bounds_dict.get("Width", 0)),
            "height": int(bounds_dict.get("Height", 0)),
        }

        # Skip tiny windows
        if bounds["width"] < 100 or bounds["height"] < 100:
            return None

        # Determine visibility and state
        is_minimized = window_dict.get("kCGWindowIsOnscreen", True) == False
        is_focused = process_id == frontmost_pid and layer == 0

        # Check if fullscreen (heuristic based on screen size)
        screen_height = AppKit.NSScreen.mainScreen().frame().size.height
        screen_width = AppKit.NSScreen.mainScreen().frame().size.width
        is_fullscreen = (
            bounds["width"] >= screen_width * 0.95
            and bounds["height"] >= screen_height * 0.95
        )

        # Determine visibility
        if is_minimized:
            visibility = SpaceWindowVisibility.MINIMIZED
        elif is_fullscreen:
            visibility = SpaceWindowVisibility.FULLSCREEN
        elif window_dict.get("kCGWindowIsOnscreen", False):
            visibility = SpaceWindowVisibility.VISIBLE_CURRENT_SPACE
        else:
            visibility = SpaceWindowVisibility.VISIBLE_OTHER_SPACE

        return EnhancedWindowInfo(
            window_id=window_id,
            app_name=app_name,
            window_title=window_title,
            process_id=process_id,
            bounds=bounds,
            is_minimized=is_minimized,
            is_fullscreen=is_fullscreen,
            is_focused=is_focused,
            layer=layer,
            alpha=alpha,
            visibility=visibility,
        )

    def _enhance_window_metadata(self, window: EnhancedWindowInfo):
        """Enhance window with additional metadata"""
        # Try to extract document path from window title
        if "—" in window.window_title:
            # Common pattern: "filename — application"
            parts = window.window_title.split("—")
            potential_path = parts[0].strip()

            # Check if it looks like a file path
            if "/" in potential_path or "." in potential_path:
                window.document_path = potential_path

        # Determine workspace role based on app and position
        if window.app_name in ["Visual Studio Code", "Xcode", "IntelliJ IDEA"]:
            window.workspace_role = "main"
        elif window.app_name in ["Terminal", "iTerm2"]:
            window.workspace_role = "tool"
        elif window.app_name in ["Safari", "Chrome", "Firefox"]:
            window.workspace_role = "reference"

    def _update_current_space(self):
        """Update current space information"""
        # Try to get current space using various methods
        try:
            # Method 1: Use accessibility API to get space info
            # This is a simplified version - in production you'd use more robust methods
            workspace = AppKit.NSWorkspace.sharedWorkspace()

            # Try to determine current space (this is a placeholder)
            # In reality, you'd need to use private APIs or other methods
            if not self.current_space_id:
                self.current_space_id = 1  # Default to space 1

        except Exception as e:
            logger.error(f"Failed to update current space: {e}")
            self.current_space_id = 1

    def _get_space_info(self) -> List[SpaceInfo]:
        """Get information about all spaces"""
        spaces = []

        # This is a simplified implementation
        # In production, you'd use more sophisticated methods to detect spaces

        # For now, infer spaces from window positions and visibility
        # Windows off-screen or not visible are likely on other spaces
        estimated_spaces = self._estimate_spaces_from_windows()

        for space_id in estimated_spaces:
            space = SpaceInfo(
                space_id=space_id,
                space_uuid=f"space-{space_id}",
                display_id=0,  # Main display
                is_current=(space_id == self.current_space_id),
                window_count=len(self.space_windows_map.get(space_id, set())),
            )

            # Check cache for screenshot
            if space_id in self.screenshot_cache:
                screenshot, timestamp = self.screenshot_cache[space_id]
                if datetime.now() - timestamp < self.cache_ttl:
                    space.cached_screenshot = screenshot
                    space.screenshot_timestamp = timestamp

            spaces.append(space)

        return spaces

    def _estimate_spaces_from_windows(self) -> Set[int]:
        """Estimate number of spaces from window visibility"""
        # Start with current space
        spaces = {self.current_space_id}

        # Try Yabai detection first (most accurate)
        try:
            from .yabai_space_detector import get_yabai_detector

            yabai_detector = get_yabai_detector()
            if yabai_detector.is_available():
                yabai_spaces = yabai_detector.enumerate_all_spaces()

                if yabai_spaces:
                    detected_spaces = set()
                    for space_info in yabai_spaces:
                        space_id = space_info.get("space_id", 1)
                        detected_spaces.add(space_id)

                    spaces.update(detected_spaces)
                    logger.info(
                        f"[SPACE_DETECTION] Detected {len(detected_spaces)} spaces via Yabai: {sorted(detected_spaces)}"
                    )
                    return spaces
        except Exception as e:
            logger.warning(f"[SPACE_DETECTION] Yabai detection failed: {e}")

        # NOTE: Objective-C space detection removed due to segfault issues
        # The compiled libspace_detection.dylib had memory management problems
        # causing crashes. Using Yabai or Core Graphics fallback instead.

        # Fallback: Use Core Graphics API to get all windows (including off-screen)
        try:
            import Quartz

            # Get all windows using Core Graphics
            window_list = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
            )

            # Analyze window positions and visibility
            visible_windows = []
            offscreen_windows = []

            for window_info in window_list:
                window_id = window_info.get("kCGWindowNumber")
                bounds = window_info.get("kCGWindowBounds", {})
                layer = window_info.get("kCGWindowLayer", 0)
                owner_name = window_info.get("kCGWindowOwnerName", "Unknown")

                # Skip system windows and very small windows
                if layer != 0 or owner_name in ["Window Server", "Dock", "MenuMeters"]:
                    continue

                x = bounds.get("X", 0)
                y = bounds.get("Y", 0)
                width = bounds.get("Width", 0)
                height = bounds.get("Height", 0)

                # Skip tiny windows
                if width < 100 or height < 100:
                    continue

                # Determine if window is visible on current screen
                screen_bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
                screen_x = screen_bounds.origin.x
                screen_y = screen_bounds.origin.y
                screen_width = screen_bounds.size.width
                screen_height = screen_bounds.size.height

                # Check if window is on current screen
                is_on_current_screen = (
                    x >= screen_x
                    and y >= screen_y
                    and x + width <= screen_x + screen_width
                    and y + height <= screen_y + screen_height
                )

                if is_on_current_screen:
                    visible_windows.append((x, y, width, height, owner_name))
                else:
                    offscreen_windows.append((x, y, width, height, owner_name))

            # If we have offscreen windows, estimate additional spaces
            if offscreen_windows:
                # Group offscreen windows by position
                position_groups = {}
                for x, y, w, h, app in offscreen_windows:
                    # Simple grouping by x-coordinate ranges
                    if x < -1000:  # Far left
                        group = 1
                    elif x < 0:  # Left edge
                        group = 2
                    elif x > screen_width:  # Right edge
                        group = 3
                    elif x > screen_width + 1000:  # Far right
                        group = 4
                    else:
                        group = 5  # Other positions

                    if group not in position_groups:
                        position_groups[group] = []
                    position_groups[group].append((x, y, w, h, app))

                # Add spaces for each group
                for group_id in position_groups.keys():
                    spaces.add(group_id)

                logger.info(
                    f"[SPACE_DETECTION] Detected {len(spaces)} spaces via Core Graphics: {sorted(spaces)}"
                )
                logger.info(
                    f"[SPACE_DETECTION] Visible windows: {len(visible_windows)}, Offscreen windows: {len(offscreen_windows)}"
                )
                return spaces

        except Exception as e:
            logger.warning(f"[SPACE_DETECTION] Core Graphics detection failed: {e}")

        # Fallback: Use window-based detection
        try:
            # Get all windows and analyze their positions
            windows = self._get_all_windows()

            # Group windows by approximate position clusters
            position_clusters = set()
            for window in windows:
                if window.bounds and window.bounds.get("x") is not None:
                    # Simple clustering based on x-coordinate ranges
                    x_pos = window.bounds["x"]
                    if x_pos < 100:  # Left edge
                        position_clusters.add(1)
                    elif x_pos < 2000:  # Middle area
                        position_clusters.add(2)
                    else:  # Right edge
                        position_clusters.add(3)

            if position_clusters:
                spaces.update(position_clusters)
                logger.info(
                    f"[SPACE_DETECTION] Detected spaces via window positions: {sorted(spaces)}"
                )
                return spaces

        except Exception as e:
            logger.warning(f"[SPACE_DETECTION] Window-based detection failed: {e}")

        # Final fallback: Use a reasonable default based on common setups
        logger.warning("[SPACE_DETECTION] Using fallback space detection")
        return {1, 2, 3, 4, 5, 6, 7}  # Allow for more spaces

    def _map_windows_to_spaces(
        self, windows: List[EnhancedWindowInfo], spaces: List[SpaceInfo]
    ):
        """Map windows to their respective spaces"""
        # Clear existing mapping
        self.space_windows_map.clear()

        for window in windows:
            # Determine space based on visibility
            if window.visibility == SpaceWindowVisibility.VISIBLE_CURRENT_SPACE:
                window.space_id = self.current_space_id
                self.space_windows_map[self.current_space_id].add(window.window_id)

            elif window.visibility == SpaceWindowVisibility.VISIBLE_OTHER_SPACE:
                # Try to determine which other space
                # This is a heuristic - in production you'd use actual detection
                window.space_id = self._estimate_window_space(window)
                self.space_windows_map[window.space_id].add(window.window_id)

    def _estimate_window_space(self, window: EnhancedWindowInfo) -> int:
        """Estimate which space a window is on"""
        # Try to use AppleScript to get the actual space for this window
        try:
            import subprocess

            # Use AppleScript to find which space a window is on
            script = f"""
            tell application "System Events"
                set targetApp to "{window.app_name}"
                set targetTitle to "{window.window_title}"
                
                try
                    set appProcess to first process whose name is targetApp
                    set targetWindow to first window of appProcess whose title contains targetTitle
                    
                    -- Try to determine space (this is limited by macOS security)
                    return 1  -- Default to space 1 for now
                on error
                    return 1
                end try
            end tell
            """

            result = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=3
            )

            if result.returncode == 0 and result.stdout.strip().isdigit():
                space_id = int(result.stdout.strip())
                logger.debug(
                    f"[WINDOW_SPACE] Window '{window.window_title}' detected on space {space_id}"
                )
                return space_id

        except Exception as e:
            logger.debug(
                f"[WINDOW_SPACE] AppleScript detection failed for window '{window.window_title}': {e}"
            )

        # Fallback: Use window position and visibility to estimate space
        if window.visibility == SpaceWindowVisibility.VISIBLE_CURRENT_SPACE:
            return self.current_space_id or 1

        elif window.visibility == SpaceWindowVisibility.VISIBLE_OTHER_SPACE:
            # Use window bounds to estimate space
            if window.bounds and window.bounds.get("x") is not None:
                x_pos = window.bounds["x"]
                # Simple heuristic based on position
                if x_pos < 0:  # Off-screen left
                    return 2
                elif x_pos > 2000:  # Off-screen right
                    return 3
                else:
                    return 2  # Default to space 2 for other spaces

        # Final fallback: Distribute based on app type
        if window.app_name in ["Visual Studio Code", "Cursor", "Terminal"]:
            return 2  # Development space
        elif window.app_name in ["Slack", "Messages", "Discord"]:
            return 3  # Communication space
        elif window.app_name in ["Finder"]:
            return 1  # Default space
        else:
            return 2  # Default to space 2 for unknown apps

    def find_window(self, query: str) -> List[EnhancedWindowInfo]:
        """Find windows matching a query"""
        query_lower = query.lower()
        matches = []

        # Update if cache is stale
        if time.time() - self.last_update > self.update_interval:
            self.get_all_windows_across_spaces()

        for window in self.windows_cache.values():
            if (
                query_lower in window.app_name.lower()
                or query_lower in window.window_title.lower()
                or (
                    window.document_path and query_lower in window.document_path.lower()
                )
            ):
                matches.append(window)

        return matches

    def get_space_summary(self, space_id: int) -> Dict[str, Any]:
        """Get a summary of what's on a specific space"""
        # Update cache if needed
        if time.time() - self.last_update > self.update_interval:
            self.get_all_windows_across_spaces()

        windows_on_space = [
            self.windows_cache[wid]
            for wid in self.space_windows_map.get(space_id, set())
            if wid in self.windows_cache
        ]

        # Group by application
        apps = defaultdict(list)
        for window in windows_on_space:
            apps[window.app_name].append(window)

        # Build summary
        summary = {
            "space_id": space_id,
            "is_current": space_id == self.current_space_id,
            "window_count": len(windows_on_space),
            "applications": {
                app: [w.to_context_string() for w in windows]
                for app, windows in apps.items()
            },
            "has_cached_screenshot": space_id in self.screenshot_cache,
        }

        return summary

    async def capture_space_with_switch(self, space_id: int) -> Optional[Any]:
        """Capture a space by switching to it (requires user permission)"""
        if space_id == self.current_space_id:
            # Already on this space, capture normally
            from PIL import ImageGrab

            screenshot = ImageGrab.grab()
            self.screenshot_cache[space_id] = (screenshot, datetime.now())
            return screenshot

        # Switch to space using AppleScript
        script = f"""
        tell application "System Events"
            key code 18 using {{control down}}
        end tell
        """

        try:
            # Execute space switch
            subprocess.run(["osascript", "-e", script], check=True)

            # Wait for switch animation
            await asyncio.sleep(0.5)

            # Capture screenshot
            from PIL import ImageGrab

            screenshot = ImageGrab.grab()

            # Cache it
            self.screenshot_cache[space_id] = (screenshot, datetime.now())

            # Switch back
            subprocess.run(["osascript", "-e", script.replace("18", "19")], check=True)

            return screenshot

        except Exception as e:
            logger.error(f"Failed to capture space {space_id}: {e}")
            return None
