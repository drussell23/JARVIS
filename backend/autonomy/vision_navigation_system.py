#!/usr/bin/env python3
"""
Full Screen Vision & Navigation System for JARVIS
Provides complete workspace understanding and autonomous navigation capabilities
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import numpy as np
from collections import defaultdict

# System integration imports
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Vision and workspace imports
from vision.screen_capture_module import ScreenCaptureModule, ScreenCapture
from vision.ocr_processor import OCRProcessor, OCRResult, TextRegion
from vision.window_analysis import WindowAnalyzer, WindowContent, ApplicationCategory
from vision.window_detector import WindowDetector, WindowInfo
from vision.enhanced_monitoring import EnhancedWorkspaceMonitor

# macOS integration
try:
    import Quartz
    import AppKit
    from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionAll, kCGNullWindowID
    from AppKit import NSWorkspace, NSRunningApplication
    MACOS_AVAILABLE = True
except ImportError:
    MACOS_AVAILABLE = False
    
logger = logging.getLogger(__name__)


class NavigationAction(Enum):
    """Types of navigation actions"""
    SWITCH_WINDOW = "switch_window"
    OPEN_APPLICATION = "open_application"
    CLOSE_WINDOW = "close_window"
    MINIMIZE_WINDOW = "minimize_window"
    MAXIMIZE_WINDOW = "maximize_window"
    MOVE_WINDOW = "move_window"
    RESIZE_WINDOW = "resize_window"
    CLICK_ELEMENT = "click_element"
    TYPE_TEXT = "type_text"
    SCROLL = "scroll"
    FOCUS_ELEMENT = "focus_element"
    NAVIGATE_MENU = "navigate_menu"
    SWITCH_DESKTOP = "switch_desktop"
    ARRANGE_WINDOWS = "arrange_windows"


class WorkspaceLayout(Enum):
    """Predefined workspace layouts"""
    FOCUS = "focus"  # Single window maximized
    SPLIT = "split"  # Two windows side by side
    GRID = "grid"   # Four windows in grid
    CASCADE = "cascade"  # Windows cascaded
    CUSTOM = "custom"  # User-defined layout


@dataclass
class WorkspaceElement:
    """Represents an element in the workspace"""
    id: str
    type: str  # window, button, menu, text_field, etc.
    bounds: Tuple[int, int, int, int]  # x, y, width, height
    parent_window: Optional[str] = None
    text: Optional[str] = None
    is_interactive: bool = False
    is_focused: bool = False
    children: List['WorkspaceElement'] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)
    
    def contains_point(self, x: int, y: int) -> bool:
        """Check if point is within element bounds"""
        ex, ey, ew, eh = self.bounds
        return ex <= x <= ex + ew and ey <= y <= ey + eh
    
    def center_point(self) -> Tuple[int, int]:
        """Get center point of element"""
        x, y, w, h = self.bounds
        return (x + w // 2, y + h // 2)


@dataclass
class WorkspaceMap:
    """Complete map of the workspace"""
    windows: List[WindowInfo]
    elements: List[WorkspaceElement]
    active_window: Optional[WindowInfo] = None
    screen_bounds: Tuple[int, int, int, int] = (0, 0, 1920, 1080)
    desktop_number: int = 1
    timestamp: datetime = field(default_factory=datetime.now)
    
    def find_element_at(self, x: int, y: int) -> Optional[WorkspaceElement]:
        """Find element at given coordinates"""
        for element in reversed(self.elements):  # Check from top to bottom
            if element.contains_point(x, y):
                return element
        return None
    
    def get_window_elements(self, window_id: str) -> List[WorkspaceElement]:
        """Get all elements belonging to a window"""
        return [e for e in self.elements if e.parent_window == window_id]


@dataclass
class NavigationPath:
    """Path for navigating between elements"""
    start: WorkspaceElement
    end: WorkspaceElement
    steps: List[NavigationAction]
    confidence: float = 0.0
    estimated_time: float = 0.0
    
    def add_step(self, action: NavigationAction, params: Dict[str, Any] = None):
        """Add a navigation step"""
        self.steps.append({
            'action': action,
            'params': params or {},
            'timestamp': datetime.now()
        })


class VisionNavigationSystem:
    """
    Full screen vision and navigation system that provides complete workspace
    understanding and autonomous navigation capabilities
    """
    
    def __init__(self):
        # Vision components
        self.screen_capture = ScreenCaptureModule(capture_interval=0.5)  # Faster for navigation
        self.ocr_processor = OCRProcessor()
        self.window_analyzer = WindowAnalyzer()
        self.window_detector = WindowDetector()
        self.enhanced_monitor = EnhancedWorkspaceMonitor()
        
        # Workspace mapping
        self.current_map: Optional[WorkspaceMap] = None
        self.element_cache: Dict[str, WorkspaceElement] = {}
        self.window_relationships: Dict[str, List[str]] = defaultdict(list)
        
        # Navigation state
        self.navigation_history: List[NavigationAction] = []
        self.current_focus: Optional[WorkspaceElement] = None
        self.navigation_mode: bool = False
        
        # Learning system
        self.navigation_patterns: Dict[str, List[NavigationPath]] = defaultdict(list)
        self.element_interactions: Dict[str, int] = defaultdict(int)
        self.workflow_sequences: List[List[NavigationAction]] = []
        
        # Performance tracking
        self.navigation_stats = {
            'total_navigations': 0,
            'successful_navigations': 0,
            'failed_navigations': 0,
            'average_time': 0.0
        }
        
    async def start_navigation_mode(self):
        """Enable navigation mode for active control"""
        self.navigation_mode = True
        logger.info("Navigation mode activated - JARVIS has full workspace control")
        
        # Start continuous workspace mapping
        asyncio.create_task(self._continuous_mapping())
        
    async def stop_navigation_mode(self):
        """Disable navigation mode"""
        self.navigation_mode = False
        logger.info("Navigation mode deactivated")
        
    async def _continuous_mapping(self):
        """Continuously map the workspace"""
        while self.navigation_mode:
            try:
                # Update workspace map
                self.current_map = await self.map_full_workspace()
                
                # Analyze relationships
                await self._analyze_window_relationships()
                
                # Update element cache
                self._update_element_cache()
                
                await asyncio.sleep(0.5)  # Update every 500ms
                
            except Exception as e:
                logger.error(f"Error in continuous mapping: {e}")
                await asyncio.sleep(1)
    
    async def map_full_workspace(self) -> WorkspaceMap:
        """Create a complete map of the current workspace"""
        # Capture full screen
        screen_capture = self.screen_capture.capture_screen()
        
        # Get all windows
        windows = self.window_detector.get_all_windows()
        
        # Extract all UI elements
        elements = await self._extract_all_elements(screen_capture, windows)
        
        # Find active window
        active_window = next((w for w in windows if w.is_focused), None)
        
        # Get screen bounds
        screen_bounds = self._get_screen_bounds()
        
        return WorkspaceMap(
            windows=windows,
            elements=elements,
            active_window=active_window,
            screen_bounds=screen_bounds,
            desktop_number=self._get_current_desktop()
        )
    
    async def _extract_all_elements(self, screen_capture: ScreenCapture, 
                                   windows: List[WindowInfo]) -> List[WorkspaceElement]:
        """Extract all UI elements from the screen"""
        all_elements = []
        
        # Process each visible window
        for window in windows:
            if not window.is_visible:
                continue
                
            # Extract elements from window region
            window_elements = await self._extract_window_elements(
                screen_capture, window
            )
            all_elements.extend(window_elements)
            
        # Sort by z-order (approximate by y-coordinate and size)
        all_elements.sort(key=lambda e: (e.bounds[1], -e.bounds[2] * e.bounds[3]))
        
        return all_elements
    
    async def _extract_window_elements(self, screen_capture: ScreenCapture,
                                     window: WindowInfo) -> List[WorkspaceElement]:
        """Extract UI elements from a specific window"""
        elements = []
        
        # Define window region
        region = (window.x, window.y, window.width, window.height)
        
        # Run OCR on window region
        ocr_result = await self.ocr_processor.process_image(
            screen_capture.image, region
        )
        
        # Convert text regions to elements
        for text_region in ocr_result.regions:
            element = WorkspaceElement(
                id=f"text_{window.window_id}_{len(elements)}",
                type=text_region.area_type,
                bounds=text_region.bounding_box,
                parent_window=window.window_id,
                text=text_region.text,
                is_interactive=text_region.area_type in ['button', 'menu', 'label'],
                properties={'confidence': text_region.confidence}
            )
            elements.append(element)
            
        # Detect additional UI elements using vision
        ui_elements = await self._detect_ui_elements(screen_capture, window)
        elements.extend(ui_elements)
        
        # Build element hierarchy
        self._build_element_hierarchy(elements)
        
        return elements
    
    async def _detect_ui_elements(self, screen_capture: ScreenCapture,
                                window: WindowInfo) -> List[WorkspaceElement]:
        """Detect UI elements using computer vision"""
        elements = []
        
        # This would use advanced CV techniques to detect:
        # - Buttons (by shape and shading)
        # - Text fields (by borders and cursors)
        # - Menus (by structure)
        # - Icons (by patterns)
        # - Scrollbars (by position and appearance)
        
        # For now, we'll use heuristics based on the window type
        if window.app_name in ['Finder', 'Files']:
            # File browser elements
            elements.append(WorkspaceElement(
                id=f"sidebar_{window.window_id}",
                type="sidebar",
                bounds=(window.x, window.y + 50, 200, window.height - 50),
                parent_window=window.window_id,
                is_interactive=True,
                properties={'role': 'navigation'}
            ))
            
        return elements
    
    def _build_element_hierarchy(self, elements: List[WorkspaceElement]):
        """Build parent-child relationships between elements"""
        # Sort by area (larger elements are likely parents)
        sorted_elements = sorted(
            elements, 
            key=lambda e: e.bounds[2] * e.bounds[3], 
            reverse=True
        )
        
        # Check containment
        for i, parent in enumerate(sorted_elements):
            for child in sorted_elements[i+1:]:
                if self._contains(parent.bounds, child.bounds):
                    parent.children.append(child)
                    
    def _contains(self, parent_bounds: Tuple[int, int, int, int],
                  child_bounds: Tuple[int, int, int, int]) -> bool:
        """Check if parent bounds contain child bounds"""
        px, py, pw, ph = parent_bounds
        cx, cy, cw, ch = child_bounds
        
        return (cx >= px and cy >= py and 
                cx + cw <= px + pw and cy + ch <= py + ph)
    
    async def navigate_to_element(self, target: WorkspaceElement) -> bool:
        """Navigate to a specific UI element"""
        if not self.navigation_mode:
            logger.warning("Navigation mode not active")
            return False
            
        try:
            # Find optimal path to element
            path = await self._find_navigation_path(target)
            
            if not path:
                logger.error(f"No path found to element {target.id}")
                return False
                
            # Execute navigation steps
            for step in path.steps:
                success = await self._execute_navigation_step(step)
                if not success:
                    logger.error(f"Navigation step failed: {step}")
                    return False
                    
                # Small delay between steps
                await asyncio.sleep(0.1)
                
            # Update focus
            self.current_focus = target
            self.navigation_stats['successful_navigations'] += 1
            
            # Record pattern for learning
            self._record_navigation_pattern(path)
            
            return True
            
        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            self.navigation_stats['failed_navigations'] += 1
            return False
    
    async def _find_navigation_path(self, target: WorkspaceElement) -> Optional[NavigationPath]:
        """Find optimal path to navigate to target element"""
        if not self.current_map:
            return None
            
        # Start from current focus or active window
        start = self.current_focus or self._get_active_element()
        
        if not start:
            return None
            
        path = NavigationPath(start=start, end=target, steps=[])
        
        # Check if target is in same window
        if start.parent_window == target.parent_window:
            # Direct navigation within window
            path.add_step(NavigationAction.CLICK_ELEMENT, {
                'element_id': target.id,
                'coordinates': target.center_point()
            })
        else:
            # Need to switch windows first
            target_window = self._find_window_by_id(target.parent_window)
            if target_window:
                path.add_step(NavigationAction.SWITCH_WINDOW, {
                    'window_id': target.parent_window,
                    'window_title': target_window.window_title
                })
                
                # Then click element
                path.add_step(NavigationAction.CLICK_ELEMENT, {
                    'element_id': target.id,
                    'coordinates': target.center_point()
                })
                
        path.confidence = 0.9  # High confidence for direct paths
        path.estimated_time = len(path.steps) * 0.5  # 0.5s per step estimate
        
        return path
    
    async def _execute_navigation_step(self, step: Dict[str, Any]) -> bool:
        """Execute a single navigation step"""
        action = step['action']
        params = step['params']
        
        try:
            if action == NavigationAction.SWITCH_WINDOW:
                return await self._switch_to_window(params['window_id'])
                
            elif action == NavigationAction.CLICK_ELEMENT:
                return await self._click_at_coordinates(params['coordinates'])
                
            elif action == NavigationAction.TYPE_TEXT:
                return await self._type_text(params['text'])
                
            elif action == NavigationAction.OPEN_APPLICATION:
                return await self._open_application(params['app_name'])
                
            elif action == NavigationAction.CLOSE_WINDOW:
                return await self._close_window(params['window_id'])
                
            elif action == NavigationAction.ARRANGE_WINDOWS:
                return await self._arrange_windows(params['layout'])
                
            else:
                logger.warning(f"Unknown navigation action: {action}")
                return False
                
        except Exception as e:
            logger.error(f"Error executing navigation step: {e}")
            return False
    
    async def _switch_to_window(self, window_id: str) -> bool:
        """Switch focus to a specific window"""
        if not MACOS_AVAILABLE:
            logger.error("macOS integration not available")
            return False
            
        try:
            # Use macOS APIs to bring window to front
            workspace = NSWorkspace.sharedWorkspace()
            
            # Find the window
            window_info = self._find_window_by_id(window_id)
            if not window_info:
                return False
                
            # Find the app
            apps = workspace.runningApplications()
            for app in apps:
                if app.localizedName() == window_info.app_name:
                    # Activate the app
                    app.activateWithOptions_(NSRunningApplication.ActivationOptions.ActivateIgnoringOtherApps)
                    
                    # Record action
                    self.navigation_history.append(NavigationAction.SWITCH_WINDOW)
                    return True
                    
            return False
            
        except Exception as e:
            logger.error(f"Error switching window: {e}")
            return False
    
    async def _click_at_coordinates(self, coordinates: Tuple[int, int]) -> bool:
        """Click at specific screen coordinates"""
        if not MACOS_AVAILABLE:
            logger.error("macOS integration not available")
            return False
            
        try:
            x, y = coordinates
            
            # Create mouse events
            from Quartz import (
                CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap,
                kCGEventLeftMouseDown, kCGEventLeftMouseUp, CGPointMake
            )
            
            point = CGPointMake(x, y)
            
            # Mouse down
            mouse_down = CGEventCreateMouseEvent(
                None, kCGEventLeftMouseDown, point, 0
            )
            CGEventPost(kCGHIDEventTap, mouse_down)
            
            # Small delay
            await asyncio.sleep(0.05)
            
            # Mouse up
            mouse_up = CGEventCreateMouseEvent(
                None, kCGEventLeftMouseUp, point, 0
            )
            CGEventPost(kCGHIDEventTap, mouse_up)
            
            # Record action
            self.navigation_history.append(NavigationAction.CLICK_ELEMENT)
            return True
            
        except Exception as e:
            logger.error(f"Error clicking: {e}")
            return False
    
    async def _type_text(self, text: str) -> bool:
        """Type text at current focus"""
        if not MACOS_AVAILABLE:
            logger.error("macOS integration not available")
            return False
            
        try:
            from Quartz import (
                CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap,
                CGEventSetUnicodeString
            )
            
            # Create keyboard event
            event = CGEventCreateKeyboardEvent(None, 0, True)
            CGEventSetUnicodeString(event, len(text), text)
            CGEventPost(kCGHIDEventTap, event)
            
            # Record action
            self.navigation_history.append(NavigationAction.TYPE_TEXT)
            return True
            
        except Exception as e:
            logger.error(f"Error typing text: {e}")
            return False
    
    async def navigate_to_application(self, app_name: str) -> bool:
        """Navigate to a specific application"""
        # First check if app is already open
        window = self._find_window_by_app(app_name)
        
        if window:
            # Switch to existing window
            element = WorkspaceElement(
                id=f"window_{window.window_id}",
                type="window",
                bounds=(window.x, window.y, window.width, window.height),
                parent_window=window.window_id
            )
            return await self.navigate_to_element(element)
        else:
            # Open the application
            return await self._open_application(app_name)
    
    async def _open_application(self, app_name: str) -> bool:
        """Open an application"""
        if not MACOS_AVAILABLE:
            logger.error("macOS integration not available")
            return False
            
        try:
            workspace = NSWorkspace.sharedWorkspace()
            
            # Try to launch app
            app_url = workspace.URLForApplicationWithBundleIdentifier_(
                f"com.{app_name.lower()}.{app_name}"
            )
            
            if not app_url:
                # Try alternative bundle IDs
                bundle_ids = [
                    f"com.apple.{app_name.lower()}",
                    f"com.{app_name.lower()}",
                    app_name
                ]
                
                for bundle_id in bundle_ids:
                    app_url = workspace.URLForApplicationWithBundleIdentifier_(bundle_id)
                    if app_url:
                        break
                        
            if app_url:
                workspace.openURL_(app_url)
                self.navigation_history.append(NavigationAction.OPEN_APPLICATION)
                return True
                
            return False
            
        except Exception as e:
            logger.error(f"Error opening application: {e}")
            return False
    
    async def arrange_workspace(self, layout: WorkspaceLayout,
                              windows: Optional[List[str]] = None) -> bool:
        """Arrange windows in a specific layout"""
        if not self.current_map:
            return False
            
        try:
            if layout == WorkspaceLayout.FOCUS:
                # Maximize current window
                if self.current_map.active_window:
                    return await self._maximize_window(
                        self.current_map.active_window.window_id
                    )
                    
            elif layout == WorkspaceLayout.SPLIT:
                # Split two windows side by side
                target_windows = windows or [
                    w.window_id for w in self.current_map.windows[:2]
                ]
                
                if len(target_windows) >= 2:
                    screen_width = self.current_map.screen_bounds[2]
                    screen_height = self.current_map.screen_bounds[3]
                    
                    # Position first window on left
                    await self._position_window(
                        target_windows[0],
                        0, 0, screen_width // 2, screen_height
                    )
                    
                    # Position second window on right
                    await self._position_window(
                        target_windows[1],
                        screen_width // 2, 0, screen_width // 2, screen_height
                    )
                    
                    return True
                    
            elif layout == WorkspaceLayout.GRID:
                # Arrange four windows in grid
                target_windows = windows or [
                    w.window_id for w in self.current_map.windows[:4]
                ]
                
                if len(target_windows) >= 4:
                    screen_width = self.current_map.screen_bounds[2]
                    screen_height = self.current_map.screen_bounds[3]
                    half_width = screen_width // 2
                    half_height = screen_height // 2
                    
                    positions = [
                        (0, 0, half_width, half_height),  # Top left
                        (half_width, 0, half_width, half_height),  # Top right
                        (0, half_height, half_width, half_height),  # Bottom left
                        (half_width, half_height, half_width, half_height)  # Bottom right
                    ]
                    
                    for window_id, pos in zip(target_windows, positions):
                        await self._position_window(window_id, *pos)
                        
                    return True
                    
            return False
            
        except Exception as e:
            logger.error(f"Error arranging workspace: {e}")
            return False
    
    async def search_workspace(self, query: str) -> List[WorkspaceElement]:
        """Search for elements in the workspace"""
        if not self.current_map:
            return []
            
        results = []
        query_lower = query.lower()
        
        for element in self.current_map.elements:
            # Search in text
            if element.text and query_lower in element.text.lower():
                results.append(element)
                
            # Search in properties
            for key, value in element.properties.items():
                if isinstance(value, str) and query_lower in value.lower():
                    results.append(element)
                    break
                    
        # Sort by relevance (exact matches first)
        results.sort(key=lambda e: (
            e.text and e.text.lower() == query_lower,
            e.text and e.text.lower().startswith(query_lower)
        ), reverse=True)
        
        return results
    
    async def execute_workflow(self, workflow_name: str) -> bool:
        """Execute a predefined workflow"""
        # This would load and execute saved workflows
        # For example: "prepare for meeting", "focus mode", "research setup"
        
        workflows = {
            "prepare_meeting": [
                ("close_all_except", ["Zoom", "Calendar"]),
                ("open_application", "Zoom"),
                ("arrange_workspace", WorkspaceLayout.SPLIT),
                ("mute_notifications", True)
            ],
            "focus_mode": [
                ("close_all_except", ["current"]),
                ("maximize_current", None),
                ("hide_dock", True),
                ("mute_notifications", True)
            ],
            "research_setup": [
                ("open_application", "Safari"),
                ("open_application", "Notes"),
                ("arrange_workspace", WorkspaceLayout.SPLIT),
                ("focus_element", "notes_editor")
            ]
        }
        
        if workflow_name not in workflows:
            logger.error(f"Unknown workflow: {workflow_name}")
            return False
            
        # Execute workflow steps
        for step_name, params in workflows[workflow_name]:
            # Execute each workflow step
            # This would call appropriate methods
            pass
            
        return True
    
    def _analyze_window_relationships(self):
        """Analyze relationships between windows"""
        if not self.current_map:
            return
            
        # Clear old relationships
        self.window_relationships.clear()
        
        for window in self.current_map.windows:
            # Find related windows (same app, similar titles, etc.)
            related = []
            
            for other in self.current_map.windows:
                if other.window_id == window.window_id:
                    continue
                    
                # Same application
                if other.app_name == window.app_name:
                    related.append(other.window_id)
                    
                # Similar titles (might be related documents)
                elif (window.window_title and other.window_title and
                      self._title_similarity(window.window_title, other.window_title) > 0.7):
                    related.append(other.window_id)
                    
            self.window_relationships[window.window_id] = related
    
    def _title_similarity(self, title1: str, title2: str) -> float:
        """Calculate similarity between window titles"""
        # Simple word overlap for now
        words1 = set(title1.lower().split())
        words2 = set(title2.lower().split())
        
        if not words1 or not words2:
            return 0.0
            
        intersection = words1 & words2
        union = words1 | words2
        
        return len(intersection) / len(union)
    
    def _update_element_cache(self):
        """Update cache of UI elements for faster lookup"""
        if not self.current_map:
            return
            
        self.element_cache.clear()
        
        for element in self.current_map.elements:
            self.element_cache[element.id] = element
    
    def _record_navigation_pattern(self, path: NavigationPath):
        """Record navigation pattern for learning"""
        # Group patterns by start-end pairs
        pattern_key = f"{path.start.type}_{path.end.type}"
        self.navigation_patterns[pattern_key].append(path)
        
        # Keep only recent patterns
        if len(self.navigation_patterns[pattern_key]) > 100:
            self.navigation_patterns[pattern_key].pop(0)
            
        # Update interaction counts
        self.element_interactions[path.end.id] += 1
    
    def _get_active_element(self) -> Optional[WorkspaceElement]:
        """Get currently active/focused element"""
        if not self.current_map or not self.current_map.active_window:
            return None
            
        # Find focused element in active window
        window_elements = self.current_map.get_window_elements(
            self.current_map.active_window.window_id
        )
        
        for element in window_elements:
            if element.is_focused:
                return element
                
        # Return first interactive element as fallback
        for element in window_elements:
            if element.is_interactive:
                return element
                
        return None
    
    def _find_window_by_id(self, window_id: str) -> Optional[WindowInfo]:
        """Find window by ID"""
        if not self.current_map:
            return None
            
        for window in self.current_map.windows:
            if window.window_id == window_id:
                return window
                
        return None
    
    def _find_window_by_app(self, app_name: str) -> Optional[WindowInfo]:
        """Find window by application name"""
        if not self.current_map:
            return None
            
        for window in self.current_map.windows:
            if window.app_name.lower() == app_name.lower():
                return window
                
        return None
    
    def _get_screen_bounds(self) -> Tuple[int, int, int, int]:
        """Get screen boundaries"""
        if MACOS_AVAILABLE:
            try:
                from AppKit import NSScreen
                main_screen = NSScreen.mainScreen()
                frame = main_screen.frame()
                return (0, 0, int(frame.size.width), int(frame.size.height))
            except:
                pass
                
        # Default fallback
        return (0, 0, 1920, 1080)
    
    def _get_current_desktop(self) -> int:
        """Get current desktop/space number"""
        # This would use macOS APIs to get current space
        # For now, return 1
        return 1
    
    async def _position_window(self, window_id: str, x: int, y: int, 
                             width: int, height: int) -> bool:
        """Position and resize a window"""
        # This would use macOS accessibility APIs
        # to move and resize windows
        logger.info(f"Positioning window {window_id} to {x},{y} {width}x{height}")
        return True
    
    async def _maximize_window(self, window_id: str) -> bool:
        """Maximize a window"""
        screen_bounds = self._get_screen_bounds()
        return await self._position_window(
            window_id, 0, 0, screen_bounds[2], screen_bounds[3]
        )
    
    async def _close_window(self, window_id: str) -> bool:
        """Close a window"""
        # This would use macOS APIs to close the window
        logger.info(f"Closing window {window_id}")
        return True
    
    def get_navigation_suggestions(self) -> List[Dict[str, Any]]:
        """Get navigation suggestions based on patterns"""
        suggestions = []
        
        # Most frequently accessed elements
        frequent_elements = sorted(
            self.element_interactions.items(),
            key=lambda x: x[1],
            reverse=True
        )[:5]
        
        for element_id, count in frequent_elements:
            if element_id in self.element_cache:
                element = self.element_cache[element_id]
                suggestions.append({
                    'type': 'frequent_element',
                    'element': element,
                    'usage_count': count,
                    'description': f"Frequently used {element.type}"
                })
                
        # Common navigation patterns
        for pattern_key, paths in self.navigation_patterns.items():
            if len(paths) >= 3:  # At least 3 occurrences
                avg_time = sum(p.estimated_time for p in paths) / len(paths)
                suggestions.append({
                    'type': 'navigation_pattern',
                    'pattern': pattern_key,
                    'frequency': len(paths),
                    'average_time': avg_time,
                    'description': f"Common navigation: {pattern_key}"
                })
                
        return suggestions
    
    def get_workspace_summary(self) -> Dict[str, Any]:
        """Get summary of current workspace state"""
        if not self.current_map:
            return {'error': 'No workspace map available'}
            
        return {
            'window_count': len(self.current_map.windows),
            'element_count': len(self.current_map.elements),
            'active_window': {
                'app': self.current_map.active_window.app_name,
                'title': self.current_map.active_window.window_title
            } if self.current_map.active_window else None,
            'interactive_elements': len([
                e for e in self.current_map.elements if e.is_interactive
            ]),
            'navigation_stats': self.navigation_stats,
            'current_focus': {
                'type': self.current_focus.type,
                'text': self.current_focus.text
            } if self.current_focus else None
        }


async def test_vision_navigation():
    """Test the vision navigation system"""
    print("🗺️ Testing Vision Navigation System")
    print("=" * 50)
    
    nav_system = VisionNavigationSystem()
    
    # Start navigation mode
    print("\n🚀 Starting navigation mode...")
    await nav_system.start_navigation_mode()
    
    # Wait for initial mapping
    await asyncio.sleep(2)
    
    # Get workspace summary
    summary = nav_system.get_workspace_summary()
    print(f"\n📊 Workspace Summary:")
    print(f"   Windows: {summary['window_count']}")
    print(f"   Elements: {summary['element_count']}")
    print(f"   Interactive: {summary['interactive_elements']}")
    
    if summary.get('active_window'):
        print(f"   Active: {summary['active_window']['app']} - {summary['active_window']['title']}")
    
    # Search for elements
    print("\n🔍 Searching workspace...")
    results = await nav_system.search_workspace("button")
    print(f"   Found {len(results)} button elements")
    
    # Get navigation suggestions
    suggestions = nav_system.get_navigation_suggestions()
    print(f"\n💡 Navigation Suggestions: {len(suggestions)}")
    
    # Stop navigation mode
    await nav_system.stop_navigation_mode()
    
    print("\n✅ Vision navigation test complete!")


if __name__ == "__main__":
    asyncio.run(test_vision_navigation())