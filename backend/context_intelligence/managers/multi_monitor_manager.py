"""
Multi-Monitor Manager
=====================

Overcomes v1.0 limitations:
❌ Assumes single display
❌ Doesn't map spaces to monitors
❌ Can't distinguish "left monitor" vs "right monitor"

New capabilities:
✅ Detects all connected monitors
✅ Maps spaces to specific monitors
✅ Understands spatial relationships (left, right, top, bottom)
✅ Resolves natural language references ("main display", "that screen")
✅ Tracks monitor configurations dynamically

Strategy:
- Query display information from macOS
- Map yabai spaces to physical monitors
- Understand spatial relationships
- Integrate with ImplicitReferenceResolver for context
- Support dynamic monitor changes (connect/disconnect)
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class MonitorPosition(Enum):
    """Relative position of monitor"""

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"
    CENTER = "center"
    MAIN = "main"


@dataclass
class MonitorInfo:
    """Information about a physical monitor"""

    id: int
    uuid: str
    name: str
    resolution: Tuple[int, int]  # (width, height)
    position: Tuple[int, int]  # (x, y) in screen coordinates
    is_main: bool
    spaces: List[int]  # Yabai space IDs on this monitor
    relative_positions: Set[MonitorPosition]  # left, right, etc.
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MonitorLayout:
    """Overall monitor layout"""

    monitors: List[MonitorInfo]
    main_monitor: Optional[MonitorInfo]
    total_resolution: Tuple[int, int]
    layout_type: str  # "single", "horizontal", "vertical", "grid"
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class SpaceMonitorMapping:
    """Mapping of space to monitor"""

    space_id: int
    monitor_id: int
    monitor_uuid: str
    monitor_name: str
    is_visible: bool
    position_on_monitor: str  # "left", "right", "center"


class MonitorDetector:
    """
    Detects connected monitors and their configurations.

    Uses system_profiler and other macOS tools to get monitor information.
    Enhanced with YOLO vision for visual monitor layout detection.
    """

    def __init__(self, enable_vision: bool = True):
        """Initialize monitor detector

        Args:
            enable_vision: Enable YOLO vision for visual monitor detection
        """
        self.cache_ttl = 10.0  # Cache for 10 seconds
        self._cache: Optional[List[MonitorInfo]] = None
        self._cache_time: Optional[float] = None
        self.enable_vision = enable_vision
        self._vision_analyzer = None

    def _get_vision_analyzer(self):
        """Lazy load vision analyzer for monitor detection"""
        if self._vision_analyzer is None and self.enable_vision:
            try:
                import os

                from backend.vision.optimized_claude_vision import OptimizedClaudeVisionAnalyzer

                api_key = os.getenv("ANTHROPIC_API_KEY")
                if api_key:
                    self._vision_analyzer = OptimizedClaudeVisionAnalyzer(
                        api_key=api_key, use_intelligent_selection=True, use_yolo_hybrid=True
                    )
                    logger.info("[MONITOR-DETECTOR] Vision analyzer loaded for monitor detection")
            except Exception as e:
                logger.warning(f"[MONITOR-DETECTOR] Vision analyzer not available: {e}")
        return self._vision_analyzer

    async def detect_monitors(
        self, use_cache: bool = True, use_core_graphics: bool = True
    ) -> List[MonitorInfo]:
        """
        Detect all connected monitors using Core Graphics or system_profiler.

        Args:
            use_cache: Use cached results if available
            use_core_graphics: Try Core Graphics first, fallback to system_profiler

        Returns:
            List of MonitorInfo objects
        """
        # Check cache
        if use_cache and self._cache and self._cache_time:
            import time

            age = time.time() - self._cache_time
            if age < self.cache_ttl:
                return self._cache

        monitors = []

        try:
            # Try Core Graphics first (v2.0 method)
            if use_core_graphics:
                displays = await self._get_displays_via_core_graphics()
                if not displays:
                    # Fallback to system_profiler
                    displays = await self._get_display_info()
            else:
                # Use system_profiler directly
                displays = await self._get_display_info()

            # Get display arrangement from defaults
            arrangement = await self._get_display_arrangement()

            # Combine information
            for i, display in enumerate(displays):
                # Use position from Core Graphics if available, otherwise from arrangement
                position = display.get("position", arrangement.get(i, {}).get("position", (0, 0)))

                monitor = MonitorInfo(
                    id=display.get(
                        "display_id", i + 1
                    ),  # Use Core Graphics display_id if available
                    uuid=display.get("uuid", f"display_{i}"),
                    name=display.get("name", f"Display {i+1}"),
                    resolution=(display.get("width", 1920), display.get("height", 1080)),
                    position=position,
                    is_main=display.get("main", i == 0),
                    spaces=[],
                    relative_positions=set(),
                )
                monitors.append(monitor)

            # Calculate relative positions
            if len(monitors) > 1:
                self._calculate_relative_positions(monitors)

            # Update cache
            import time

            self._cache = monitors
            self._cache_time = time.time()

            return monitors

        except Exception as e:
            logger.error(f"Failed to detect monitors: {e}")
            # Fallback: assume single monitor
            return [
                MonitorInfo(
                    id=1,
                    uuid="default",
                    name="Main Display",
                    resolution=(1920, 1080),
                    position=(0, 0),
                    is_main=True,
                    spaces=[],
                    relative_positions={MonitorPosition.MAIN},
                )
            ]

    async def _get_display_info(self) -> List[Dict[str, Any]]:
        """Get display information from system_profiler"""
        try:
            # Run system_profiler
            result = await asyncio.create_subprocess_exec(
                "system_profiler",
                "SPDisplaysDataType",
                "-json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                logger.warning(f"system_profiler failed: {stderr.decode()}")
                return []

            data = json.loads(stdout.decode())

            displays = []
            for item in data.get("SPDisplaysDataType", []):
                # Extract displays from the structure
                for display_key, display_info in item.items():
                    if isinstance(display_info, dict) and "spdisplays_ndrvs" in display_info:
                        for display in display_info["spdisplays_ndrvs"]:
                            resolution_str = display.get("_spdisplays_resolution", "1920 x 1080")
                            width, height = self._parse_resolution(resolution_str)

                            displays.append(
                                {
                                    "uuid": display.get(
                                        "_spdisplays_display-serial-number",
                                        f"display_{len(displays)}",
                                    ),
                                    "name": display.get("_name", f"Display {len(displays)+1}"),
                                    "width": width,
                                    "height": height,
                                    "main": display.get("spdisplays_main", "spdisplays_no")
                                    == "spdisplays_yes",
                                }
                            )

            return (
                displays
                if displays
                else [
                    {
                        "uuid": "default",
                        "name": "Main Display",
                        "width": 1920,
                        "height": 1080,
                        "main": True,
                    }
                ]
            )

        except Exception as e:
            logger.warning(f"Failed to get display info: {e}")
            return [
                {
                    "uuid": "default",
                    "name": "Main Display",
                    "width": 1920,
                    "height": 1080,
                    "main": True,
                }
            ]

    async def _get_displays_via_core_graphics(self) -> List[Dict[str, Any]]:
        """
        Get display information via Core Graphics (v2.0 method).
        Uses Python script with Quartz bindings (PyObjC).
        """
        try:
            # Try importing Quartz for Core Graphics access
            script = """
import sys
try:
    from Quartz import CGGetActiveDisplayList, CGDisplayBounds, CGMainDisplayID
    import json

    # Get all active displays (CGGetActiveDisplayList)
    max_displays = 16
    (err, active_displays, num_displays) = CGGetActiveDisplayList(max_displays, None, None)

    if err != 0:
        print(json.dumps([]))
        sys.exit(0)

    displays = []
    main_display_id = CGMainDisplayID()

    for i, display_id in enumerate(active_displays[:num_displays]):
        bounds = CGDisplayBounds(display_id)

        displays.append({
            "display_id": int(display_id),
            "uuid": f"display_{display_id}",
            "name": f"Display {i+1}",
            "width": int(bounds.size.width),
            "height": int(bounds.size.height),
            "x": int(bounds.origin.x),
            "y": int(bounds.origin.y),
            "main": display_id == main_display_id
        })

    print(json.dumps(displays))
except ImportError:
    # PyObjC not available
    print(json.dumps([]))
except Exception as e:
    print(json.dumps([]), file=sys.stderr)
"""

            # Execute Python script
            result = await asyncio.create_subprocess_exec(
                "python3",
                "-c",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()

            if result.returncode != 0 or not stdout:
                logger.debug("Core Graphics detection not available (PyObjC not installed)")
                return []

            displays_data = json.loads(stdout.decode())

            if not displays_data:
                return []

            # Convert to standard format
            displays = []
            for display in displays_data:
                displays.append(
                    {
                        "uuid": display["uuid"],
                        "name": display["name"],
                        "width": display["width"],
                        "height": display["height"],
                        "position": (display["x"], display["y"]),
                        "main": display["main"],
                        "display_id": display["display_id"],
                    }
                )

            logger.info(f"[MONITOR-DETECTOR] Detected {len(displays)} displays via Core Graphics")
            return displays

        except Exception as e:
            logger.debug(f"Core Graphics detection failed: {e}")
            return []

    def _parse_resolution(self, resolution_str: str) -> Tuple[int, int]:
        """Parse resolution string like '1920 x 1080'"""
        match = re.search(r"(\d+)\s*x\s*(\d+)", resolution_str)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 1920, 1080

    async def _get_display_arrangement(self) -> Dict[int, Dict[str, Any]]:
        """Get display arrangement (positions)"""
        # This would ideally use CGDisplay API, but for now we'll use defaults
        # In a real implementation, you'd use a Swift/ObjC bridge
        try:
            result = await asyncio.create_subprocess_exec(
                "defaults",
                "read",
                "/Library/Preferences/com.apple.windowserver",
                "DisplaySets",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()

            # Parse the arrangement (simplified)
            # Real implementation would parse the plist structure
            return {}

        except Exception as e:
            logger.debug(f"Could not get display arrangement: {e}")
            return {}

    def _calculate_relative_positions(self, monitors: List[MonitorInfo]):
        """Calculate relative positions of monitors"""
        if len(monitors) <= 1:
            if monitors:
                monitors[0].relative_positions.add(MonitorPosition.MAIN)
            return

        # Find main monitor
        main = next((m for m in monitors if m.is_main), monitors[0])
        main.relative_positions.add(MonitorPosition.MAIN)

        # Calculate positions relative to main
        for monitor in monitors:
            if monitor == main:
                continue

            dx = monitor.position[0] - main.position[0]
            dy = monitor.position[1] - main.position[1]

            # Horizontal relationships
            if abs(dx) > abs(dy):
                if dx > 0:
                    monitor.relative_positions.add(MonitorPosition.RIGHT)
                    main.relative_positions.add(MonitorPosition.LEFT)
                else:
                    monitor.relative_positions.add(MonitorPosition.LEFT)
                    main.relative_positions.add(MonitorPosition.RIGHT)

            # Vertical relationships
            if abs(dy) > abs(dx):
                if dy > 0:
                    monitor.relative_positions.add(MonitorPosition.BOTTOM)
                    main.relative_positions.add(MonitorPosition.TOP)
                else:
                    monitor.relative_positions.add(MonitorPosition.TOP)
                    main.relative_positions.add(MonitorPosition.BOTTOM)

    async def detect_monitors_with_vision(self, screenshot=None) -> Optional[Dict[str, Any]]:
        """
        Detect monitors using YOLO vision for visual layout detection

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
            # Use YOLO to detect monitors visually
            result = await vision_analyzer.detect_monitors(screenshot)

            if result:
                logger.info(
                    f"[MONITOR-DETECTOR] Detected monitors via YOLO vision: {result.get('monitor_count', 0)}"
                )
                return result

            return None

        except Exception as e:
            logger.error(f"[MONITOR-DETECTOR] Error detecting monitors with vision: {e}")
            return None

    async def get_enhanced_monitor_info(
        self, use_cache: bool = True, screenshot=None
    ) -> Dict[str, Any]:
        """
        Get monitor information enhanced with vision analysis

        Args:
            use_cache: Use cached monitor info
            screenshot: Optional screenshot for vision analysis

        Returns:
            Enhanced monitor information with vision data
        """
        # Get base monitor info
        monitors = await self.detect_monitors(use_cache=use_cache)

        result = {
            "monitors": monitors,
            "monitor_count": len(monitors),
            "layout_type": self._determine_layout_type(monitors) if len(monitors) > 1 else "single",
        }

        # Add vision enhancements if available
        if self.enable_vision and screenshot is not None:
            vision_analyzer = self._get_vision_analyzer()
            if vision_analyzer:
                try:
                    # Detect monitors visually
                    vision_result = await self.detect_monitors_with_vision(screenshot)
                    if vision_result:
                        result["vision_detection"] = vision_result

                        # Compare Core Graphics vs YOLO detection
                        cg_count = len(monitors)
                        yolo_count = vision_result.get("monitor_count", 0)

                        if cg_count != yolo_count:
                            logger.warning(
                                f"[MONITOR-DETECTOR] Monitor count mismatch: "
                                f"Core Graphics={cg_count}, YOLO={yolo_count}"
                            )
                            result["detection_discrepancy"] = {
                                "core_graphics": cg_count,
                                "yolo": yolo_count,
                            }

                    logger.info("[MONITOR-DETECTOR] Enhanced monitor info with vision analysis")

                except Exception as e:
                    logger.error(
                        f"[MONITOR-DETECTOR] Error enhancing monitor info with vision: {e}"
                    )

        return result


class MonitorSpaceMapper:
    """
    Maps yabai spaces to physical monitors.

    Queries yabai for space and display information.
    """

    def __init__(self):
        """Initialize monitor space mapper"""

    async def map_spaces_to_monitors(self, monitors: List[MonitorInfo]) -> List[MonitorInfo]:
        """
        Map yabai spaces to monitors.

        Args:
            monitors: List of detected monitors

        Returns:
            Updated monitors with space mappings
        """
        try:
            # Get yabai display information
            await self._query_yabai_displays()

            # Get yabai space information
            spaces = await self._query_yabai_spaces()

            # Map spaces to displays
            display_spaces: Dict[int, List[int]] = {}
            for space in spaces:
                display_id = space.get("display", 1)
                space_id = space.get("index", space.get("id", 0))

                if display_id not in display_spaces:
                    display_spaces[display_id] = []
                display_spaces[display_id].append(space_id)

            # Update monitor information
            for i, monitor in enumerate(monitors):
                # Map yabai display ID to monitor (1-indexed)
                yabai_display_id = i + 1
                monitor.spaces = sorted(display_spaces.get(yabai_display_id, []))

            return monitors

        except Exception as e:
            logger.error(f"Failed to map spaces to monitors: {e}")
            # Fallback: assign all spaces to first monitor
            if monitors:
                try:
                    spaces = await self._query_yabai_spaces()
                    monitors[0].spaces = [
                        s.get("index", s.get("id", i + 1)) for i, s in enumerate(spaces)
                    ]
                except Exception:
                    pass
            return monitors

    async def _query_yabai_displays(self) -> List[Dict[str, Any]]:
        """Query yabai for display information"""
        try:
            result = await asyncio.create_subprocess_exec(
                "yabai",
                "-m",
                "query",
                "--displays",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                logger.warning(f"yabai query displays failed: {stderr.decode()}")
                return []

            return json.loads(stdout.decode())

        except Exception as e:
            logger.warning(f"Failed to query yabai displays: {e}")
            return []

    async def _query_yabai_spaces(self) -> List[Dict[str, Any]]:
        """Query yabai for space information"""
        try:
            result = await asyncio.create_subprocess_exec(
                "yabai",
                "-m",
                "query",
                "--spaces",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                logger.warning(f"yabai query spaces failed: {stderr.decode()}")
                return []

            return json.loads(stdout.decode())

        except Exception as e:
            logger.warning(f"Failed to query yabai spaces: {e}")
            return []


class MonitorReferenceResolver:
    """
    Resolves natural language monitor references.

    Integrates with ImplicitReferenceResolver for context-aware resolution.

    Examples:
    - "left monitor" → Monitor 2
    - "main display" → Monitor 1
    - "that screen" → Recently discussed monitor
    - "the other one" → Monitor not currently focused
    """

    def __init__(
        self, implicit_resolver: Optional[Any] = None, conversation_tracker: Optional[Any] = None
    ):
        """
        Initialize monitor reference resolver.

        Args:
            implicit_resolver: ImplicitReferenceResolver instance
            conversation_tracker: ConversationTracker instance
        """
        self.implicit_resolver = implicit_resolver
        self.conversation_tracker = conversation_tracker

        # Common monitor references
        self.reference_patterns = {
            "main": ["main", "primary", "first"],
            "secondary": ["secondary", "second", "other"],
            "left": ["left"],
            "right": ["right"],
            "top": ["top", "upper"],
            "bottom": ["bottom", "lower"],
        }

    async def resolve_monitor_reference(
        self, query: str, monitors: List[MonitorInfo], context: Optional[Dict[str, Any]] = None
    ) -> Optional[MonitorInfo]:
        """
        Resolve monitor reference from query.

        Args:
            query: User query with monitor reference
            monitors: Available monitors
            context: Additional context

        Returns:
            Resolved MonitorInfo or None
        """
        query_lower = query.lower()

        # Check for direct references
        for ref_type, patterns in self.reference_patterns.items():
            for pattern in patterns:
                if pattern in query_lower:
                    monitor = self._resolve_by_pattern(ref_type, monitors)
                    if monitor:
                        return monitor

        # Check for "that monitor", "this display", etc.
        if any(word in query_lower for word in ["that", "this", "the"]):
            # Use conversation context
            if self.conversation_tracker:
                self.conversation_tracker.get_recent_context()
                # Get last mentioned monitor (would need to track monitor entities)
                # For now, return None and let caller handle

        # Check for monitor ID/name
        for monitor in monitors:
            if monitor.name.lower() in query_lower:
                return monitor
            if str(monitor.id) in query_lower:
                return monitor

        return None

    def _resolve_by_pattern(
        self, pattern_type: str, monitors: List[MonitorInfo]
    ) -> Optional[MonitorInfo]:
        """Resolve monitor by pattern type"""
        if pattern_type == "main":
            return next((m for m in monitors if m.is_main), None)

        if pattern_type == "secondary":
            return next((m for m in monitors if not m.is_main), None)

        if pattern_type == "left":
            return next((m for m in monitors if MonitorPosition.LEFT in m.relative_positions), None)

        if pattern_type == "right":
            return next(
                (m for m in monitors if MonitorPosition.RIGHT in m.relative_positions), None
            )

        if pattern_type == "top":
            return next((m for m in monitors if MonitorPosition.TOP in m.relative_positions), None)

        if pattern_type == "bottom":
            return next(
                (m for m in monitors if MonitorPosition.BOTTOM in m.relative_positions), None
            )

        return None


class MultiMonitorManager:
    """
    Main manager for multi-monitor support.

    Detects monitors, maps spaces, resolves references.
    """

    def __init__(
        self,
        implicit_resolver: Optional[Any] = None,
        conversation_tracker: Optional[Any] = None,
        auto_refresh_interval: float = 30.0,
        enable_vision: bool = True,
    ):
        """
        Initialize Multi-Monitor Manager.

        Args:
            implicit_resolver: ImplicitReferenceResolver instance
            conversation_tracker: ConversationTracker instance
            auto_refresh_interval: How often to refresh monitor info (seconds)
            enable_vision: Enable YOLO vision for visual monitor detection
        """
        self.detector = MonitorDetector(enable_vision=enable_vision)
        self.mapper = MonitorSpaceMapper()
        self.resolver = MonitorReferenceResolver(implicit_resolver, conversation_tracker)
        self.auto_refresh_interval = auto_refresh_interval
        self.enable_vision = enable_vision

        self._current_layout: Optional[MonitorLayout] = None
        self._refresh_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """Initialize and detect monitors"""
        await self.refresh_monitor_layout()

        # Start auto-refresh task
        if self.auto_refresh_interval > 0:
            self._refresh_task = asyncio.create_task(self._auto_refresh_loop())

    async def refresh_monitor_layout(self) -> MonitorLayout:
        """
        Refresh monitor layout.

        Returns:
            Updated MonitorLayout
        """
        # Step 1: Detect monitors
        monitors = await self.detector.detect_monitors(use_cache=False)

        # Step 2: Map spaces to monitors
        monitors = await self.mapper.map_spaces_to_monitors(monitors)

        # Step 3: Determine layout type
        layout_type = self._determine_layout_type(monitors)

        # Step 4: Calculate total resolution
        total_resolution = self._calculate_total_resolution(monitors)

        # Step 5: Find main monitor
        main_monitor = next((m for m in monitors if m.is_main), monitors[0] if monitors else None)

        layout = MonitorLayout(
            monitors=monitors,
            main_monitor=main_monitor,
            total_resolution=total_resolution,
            layout_type=layout_type,
        )

        self._current_layout = layout
        logger.info(f"[MULTI-MONITOR] Detected {len(monitors)} monitor(s), layout: {layout_type}")

        return layout

    def _determine_layout_type(self, monitors: List[MonitorInfo]) -> str:
        """Determine layout type from monitor configuration"""
        if len(monitors) == 1:
            return "single"

        # Check if all monitors are horizontally arranged
        positions = [m.position for m in monitors]
        x_coords = [p[0] for p in positions]
        y_coords = [p[1] for p in positions]

        x_range = max(x_coords) - min(x_coords)
        y_range = max(y_coords) - min(y_coords)

        if x_range > y_range * 2:
            return "horizontal"
        elif y_range > x_range * 2:
            return "vertical"
        else:
            return "grid"

    def _calculate_total_resolution(self, monitors: List[MonitorInfo]) -> Tuple[int, int]:
        """Calculate total resolution across all monitors"""
        if not monitors:
            return (0, 0)

        # Calculate bounding box
        min_x = min(m.position[0] for m in monitors)
        min_y = min(m.position[1] for m in monitors)
        max_x = max(m.position[0] + m.resolution[0] for m in monitors)
        max_y = max(m.position[1] + m.resolution[1] for m in monitors)

        return (max_x - min_x, max_y - min_y)

    async def get_monitor_by_reference(
        self, query: str, context: Optional[Dict[str, Any]] = None
    ) -> Optional[MonitorInfo]:
        """
        Get monitor by natural language reference.

        Args:
            query: Query with monitor reference
            context: Additional context

        Returns:
            MonitorInfo or None
        """
        if not self._current_layout:
            await self.refresh_monitor_layout()

        if not self._current_layout or not self._current_layout.monitors:
            return None

        return await self.resolver.resolve_monitor_reference(
            query, self._current_layout.monitors, context
        )

    async def get_spaces_on_monitor(
        self, monitor_id: Optional[int] = None, monitor_reference: Optional[str] = None
    ) -> List[int]:
        """
        Get spaces on a specific monitor.

        Args:
            monitor_id: Direct monitor ID
            monitor_reference: Natural language reference

        Returns:
            List of space IDs
        """
        if not self._current_layout:
            await self.refresh_monitor_layout()

        if not self._current_layout or not self._current_layout.monitors:
            return []

        # Resolve monitor
        monitor = None
        if monitor_id:
            monitor = next((m for m in self._current_layout.monitors if m.id == monitor_id), None)
        elif monitor_reference:
            monitor = await self.get_monitor_by_reference(monitor_reference)

        if monitor:
            return monitor.spaces

        return []

    async def get_monitor_for_space(self, space_id: int) -> Optional[MonitorInfo]:
        """
        Get monitor containing a specific space.

        Args:
            space_id: Space ID

        Returns:
            MonitorInfo or None
        """
        if not self._current_layout:
            await self.refresh_monitor_layout()

        if not self._current_layout:
            return None

        for monitor in self._current_layout.monitors:
            if space_id in monitor.spaces:
                return monitor

        return None

    def get_current_layout(self) -> Optional[MonitorLayout]:
        """Get current monitor layout"""
        return self._current_layout

    async def get_enhanced_monitor_layout(self, screenshot=None) -> Dict[str, Any]:
        """
        Get monitor layout enhanced with vision analysis

        Args:
            screenshot: Optional screenshot for vision analysis

        Returns:
            Enhanced monitor layout with vision data
        """
        if not self._current_layout:
            await self.refresh_monitor_layout()

        # Get enhanced monitor info from detector
        enhanced_info = await self.detector.get_enhanced_monitor_info(
            use_cache=True, screenshot=screenshot
        )

        result = {
            "layout": self._current_layout,
            "enhanced_monitors": enhanced_info,
            "vision_enabled": self.enable_vision,
        }

        return result

    async def _auto_refresh_loop(self):
        """Auto-refresh loop"""
        while True:
            try:
                await asyncio.sleep(self.auto_refresh_interval)
                await self.refresh_monitor_layout()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Auto-refresh failed: {e}")

    async def shutdown(self):
        """Shutdown manager"""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass


# Global instance
_multi_monitor_manager: Optional[MultiMonitorManager] = None


def get_multi_monitor_manager() -> Optional[MultiMonitorManager]:
    """Get the global MultiMonitorManager instance"""
    return _multi_monitor_manager


def initialize_multi_monitor_manager(
    implicit_resolver: Optional[Any] = None,
    conversation_tracker: Optional[Any] = None,
    auto_refresh_interval: float = 30.0,
    enable_vision: bool = True,
) -> MultiMonitorManager:
    """
    Initialize the global MultiMonitorManager instance.

    Note: The manager will be fully initialized on first use (lazy initialization).
    Call await manager.initialize() explicitly if you need immediate initialization.

    Args:
        implicit_resolver: ImplicitReferenceResolver instance
        conversation_tracker: ConversationTracker instance
        auto_refresh_interval: How often to refresh (seconds)
        enable_vision: Enable YOLO vision for visual monitor detection

    Returns:
        MultiMonitorManager instance
    """
    global _multi_monitor_manager

    _multi_monitor_manager = MultiMonitorManager(
        implicit_resolver=implicit_resolver,
        conversation_tracker=conversation_tracker,
        auto_refresh_interval=auto_refresh_interval,
        enable_vision=enable_vision,
    )

    return _multi_monitor_manager
