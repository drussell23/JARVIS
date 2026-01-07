#!/usr/bin/env python3
"""
Yabai Spatial Intelligence Engine
==================================

24/7 workspace monitoring and spatial pattern learning using Yabai window manager.

This module provides:
- Real-time Space/Desktop monitoring
- Window position and focus tracking
- App usage pattern detection
- Cross-workspace behavioral analysis
- Spatial intelligence for UAE + SAI integration

Features:
- Monitors all macOS Spaces continuously
- Tracks app locations and movements
- Detects workflow patterns
- Learns Space-specific behaviors
- Feeds spatial data to Learning Database

Author: Derek J. Russell
Date: October 2025
Version: 1.0.0 - 24/7 Spatial Intelligence
"""

import asyncio
import logging
import subprocess
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple, Callable
from dataclasses import dataclass, asdict
from collections import defaultdict, deque
from pathlib import Path
from enum import Enum
import calendar

logger = logging.getLogger(__name__)


# ============================================================================
# Event System
# ============================================================================

class YabaiEventType(Enum):
    """Yabai event types for event-driven monitoring"""
    SPACE_CHANGED = "space_changed"
    WINDOW_FOCUSED = "window_focused"
    WINDOW_CREATED = "window_created"
    WINDOW_DESTROYED = "window_destroyed"
    WINDOW_MOVED = "window_moved"
    WINDOW_RESIZED = "window_resized"
    WINDOW_MINIMIZED = "window_minimized"
    WINDOW_DEMINIMIZED = "window_deminimized"
    APP_LAUNCHED = "app_launched"
    APP_TERMINATED = "app_terminated"
    DISPLAY_CHANGED = "display_changed"
    MISSION_CONTROL_ENTER = "mission_control_enter"
    MISSION_CONTROL_EXIT = "mission_control_exit"


@dataclass
class YabaiEvent:
    """Event data from Yabai"""
    event_type: YabaiEventType
    timestamp: float
    space_id: Optional[int]
    window_id: Optional[int]
    app_name: Optional[str]
    metadata: Dict[str, Any]


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class WindowInfo:
    """Information about a window"""
    window_id: int
    app_name: str
    title: str
    frame: Dict[str, float]  # {x, y, w, h}
    is_focused: bool
    is_fullscreen: bool
    space_id: int
    stack_index: int

@dataclass
class SpaceInfo:
    """Information about a Space/Desktop"""
    space_id: int
    space_index: int
    space_label: Optional[str]
    is_visible: bool
    is_native_fullscreen: bool
    windows: List[WindowInfo]
    focused_window: Optional[WindowInfo]


@dataclass
class AppUsageSession:
    """Tracks an app usage session"""
    app_name: str
    space_id: int
    start_time: float
    end_time: Optional[float]
    duration: Optional[float]
    window_title: str
    focus_count: int


@dataclass
class SpaceTransition:
    """Records a Space transition"""
    from_space_id: int
    to_space_id: int
    trigger_app: Optional[str]
    timestamp: float
    hour_of_day: int
    day_of_week: int


# ============================================================================
# Yabai Spatial Intelligence Engine
# ============================================================================

class YabaiSpatialIntelligence:
    """
    24/7 Spatial intelligence engine using Yabai

    Continuously monitors:
    - All Spaces/Desktops
    - Window positions and movements
    - App focus and usage patterns
    - Workspace transitions
    - Behavioral patterns
    """

    def __init__(
        self,
        learning_db=None,
        monitoring_interval: float = 5.0,  # Match SAI interval
        enable_24_7_mode: bool = True
    ):
        """
        Initialize Yabai Spatial Intelligence

        Args:
            learning_db: Learning Database instance
            monitoring_interval: Monitoring interval in seconds
            enable_24_7_mode: Enable continuous 24/7 monitoring
        """
        self.learning_db = learning_db
        self.monitoring_interval = monitoring_interval
        self.enable_24_7_mode = enable_24_7_mode

        # State tracking
        self.is_monitoring = False
        self.monitoring_task: Optional[asyncio.Task] = None

        # Current state
        self.current_spaces: Dict[int, SpaceInfo] = {}
        self.current_focused_space: Optional[int] = None
        self.previous_focused_space: Optional[int] = None

        # App usage tracking
        self.active_sessions: Dict[str, AppUsageSession] = {}
        self.session_history: deque = deque(maxlen=1000)

        # Space transition tracking
        self.space_transition_history: deque = deque(maxlen=500)

        # Event-driven architecture (Phase 2)
        self.event_listeners: Dict[YabaiEventType, List[Callable]] = defaultdict(list)
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.event_processing_task: Optional[asyncio.Task] = None
        self.event_history: deque = deque(maxlen=2000)

        # Window state tracking for event detection
        self.previous_window_state: Dict[int, Dict] = {}
        self.previous_app_list: set = set()

        # Metrics
        self.metrics = {
            'total_space_changes': 0,
            'total_app_switches': 0,
            'total_sessions_tracked': 0,
            'spaces_monitored': 0,
            'windows_tracked': 0,
            'monitoring_cycles': 0,
            'events_processed': 0,
            'events_emitted': 0
        }

        # =====================================================================
        # ROOT CAUSE FIX v12.0.0: Non-Blocking Yabai Check in Constructor
        # =====================================================================
        # PROBLEM: subprocess.run() in __init__ blocks event loop for up to 2s
        # - During startup, this adds to total initialization time
        # - Can cause cascade blocking with other components
        #
        # SOLUTION: Use cached quick check from yabai_space_detector
        # - Returns cached result instantly if available
        # - Only runs subprocess if cache is invalid (with short timeout)
        # =====================================================================
        self.yabai_available = self._check_yabai_fast()

        logger.info("[YABAI-SI] Yabai Spatial Intelligence initialized")
        logger.info(f"[YABAI-SI] Yabai available: {self.yabai_available}")
        logger.info(f"[YABAI-SI] 24/7 mode: {self.enable_24_7_mode}")
        logger.info(f"[YABAI-SI] Monitoring interval: {self.monitoring_interval}s")
        logger.info(f"[YABAI-SI] Event-driven architecture: ENABLED")

    def _check_yabai_fast(self) -> bool:
        """
        Fast check if Yabai is available using cached result.

        ROOT CAUSE FIX v12.0.0: Uses module-level cache to avoid blocking.
        """
        try:
            # Try to use the cached check from yabai_space_detector
            from vision.yabai_space_detector import _quick_yabai_check
            is_available, _ = _quick_yabai_check()
            return is_available
        except ImportError:
            # Fallback to quick filesystem check (no subprocess)
            import shutil
            yabai_path = shutil.which("yabai")
            if not yabai_path:
                return False
            # Just check if executable exists, don't run subprocess
            return True
        except Exception as e:
            logger.debug(f"[YABAI-SI] Fast check failed: {e}")
            return False

    def _check_yabai(self) -> bool:
        """Legacy blocking check - DEPRECATED, use _check_yabai_fast instead."""
        try:
            result = subprocess.run(
                ['yabai', '-m', 'query', '--spaces'],
                capture_output=True,
                text=True,
                timeout=1  # Reduced from 2s
            )
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"[YABAI-SI] Yabai not available: {e}")
            return False

    # ========================================================================
    # Event System Methods (Phase 2)
    # ========================================================================

    def register_event_listener(self, event_type: YabaiEventType, callback: Callable):
        """
        Register a callback for specific event types

        Args:
            event_type: Type of event to listen for
            callback: Async function to call when event occurs
        """
        self.event_listeners[event_type].append(callback)
        logger.info(f"[YABAI-SI] Registered listener for {event_type.value}")

    def unregister_event_listener(self, event_type: YabaiEventType, callback: Callable):
        """Remove an event listener"""
        if callback in self.event_listeners[event_type]:
            self.event_listeners[event_type].remove(callback)
            logger.info(f"[YABAI-SI] Unregistered listener for {event_type.value}")

    async def _emit_event(self, event: YabaiEvent):
        """Emit an event to all registered listeners"""
        try:
            # Add to event queue
            await self.event_queue.put(event)
            self.event_history.append(event)
            self.metrics['events_emitted'] += 1

            logger.debug(f"[YABAI-SI] Event emitted: {event.event_type.value}")
        except asyncio.QueueFull:
            logger.warning(f"[YABAI-SI] Event queue full, dropping event: {event.event_type.value}")

    async def _process_events(self):
        """Process events from the queue"""
        logger.info("[YABAI-SI] Event processing task started")

        while self.is_monitoring:
            try:
                # Get event from queue with timeout
                event = await asyncio.wait_for(self.event_queue.get(), timeout=1.0)

                # Call all registered listeners for this event type
                listeners = self.event_listeners.get(event.event_type, [])
                for listener in listeners:
                    try:
                        if asyncio.iscoroutinefunction(listener):
                            await listener(event)
                        else:
                            listener(event)
                    except Exception as e:
                        logger.error(f"[YABAI-SI] Error in event listener: {e}", exc_info=True)

                self.metrics['events_processed'] += 1

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[YABAI-SI] Error processing event: {e}", exc_info=True)

    async def _detect_and_emit_events(self):
        """Detect changes and emit appropriate events"""
        try:
            # Detect space changes
            if self.current_focused_space != self.previous_focused_space:
                if self.previous_focused_space is not None:
                    event = YabaiEvent(
                        event_type=YabaiEventType.SPACE_CHANGED,
                        timestamp=time.time(),
                        space_id=self.current_focused_space,
                        window_id=None,
                        app_name=None,
                        metadata={
                            'from_space': self.previous_focused_space,
                            'to_space': self.current_focused_space
                        }
                    )
                    await self._emit_event(event)

            # Detect window focus changes
            current_focused = self._get_current_focused_window()
            if current_focused:
                event = YabaiEvent(
                    event_type=YabaiEventType.WINDOW_FOCUSED,
                    timestamp=time.time(),
                    space_id=current_focused.space_id,
                    window_id=current_focused.window_id,
                    app_name=current_focused.app_name,
                    metadata={'title': current_focused.title}
                )
                await self._emit_event(event)

            # Detect app launches/terminations
            await self._detect_app_events()

            # Detect window state changes
            await self._detect_window_events()

        except Exception as e:
            logger.error(f"[YABAI-SI] Error detecting events: {e}", exc_info=True)

    async def _detect_app_events(self):
        """Detect app launch and termination events"""
        current_apps = set(win.app_name for space in self.current_spaces.values() for win in space.windows)

        # Detect launches
        new_apps = current_apps - self.previous_app_list
        for app_name in new_apps:
            event = YabaiEvent(
                event_type=YabaiEventType.APP_LAUNCHED,
                timestamp=time.time(),
                space_id=None,
                window_id=None,
                app_name=app_name,
                metadata={}
            )
            await self._emit_event(event)

        # Detect terminations
        terminated_apps = self.previous_app_list - current_apps
        for app_name in terminated_apps:
            event = YabaiEvent(
                event_type=YabaiEventType.APP_TERMINATED,
                timestamp=time.time(),
                space_id=None,
                window_id=None,
                app_name=app_name,
                metadata={}
            )
            await self._emit_event(event)

        self.previous_app_list = current_apps

    async def _detect_window_events(self):
        """Detect window creation, destruction, moves, and resizes"""
        current_windows = {}
        for space in self.current_spaces.values():
            for win in space.windows:
                current_windows[win.window_id] = {
                    'app': win.app_name,
                    'space': win.space_id,
                    'frame': win.frame,
                    'fullscreen': win.is_fullscreen
                }

        # Detect new windows
        new_window_ids = set(current_windows.keys()) - set(self.previous_window_state.keys())
        for win_id in new_window_ids:
            win_data = current_windows[win_id]
            event = YabaiEvent(
                event_type=YabaiEventType.WINDOW_CREATED,
                timestamp=time.time(),
                space_id=win_data['space'],
                window_id=win_id,
                app_name=win_data['app'],
                metadata={'frame': win_data['frame']}
            )
            await self._emit_event(event)

        # Detect destroyed windows
        destroyed_ids = set(self.previous_window_state.keys()) - set(current_windows.keys())
        for win_id in destroyed_ids:
            prev_data = self.previous_window_state[win_id]
            event = YabaiEvent(
                event_type=YabaiEventType.WINDOW_DESTROYED,
                timestamp=time.time(),
                space_id=prev_data['space'],
                window_id=win_id,
                app_name=prev_data['app'],
                metadata={}
            )
            await self._emit_event(event)

        # Detect moves and resizes
        for win_id in set(current_windows.keys()) & set(self.previous_window_state.keys()):
            curr = current_windows[win_id]
            prev = self.previous_window_state[win_id]

            # Check if moved to different space
            if curr['space'] != prev['space']:
                event = YabaiEvent(
                    event_type=YabaiEventType.WINDOW_MOVED,
                    timestamp=time.time(),
                    space_id=curr['space'],
                    window_id=win_id,
                    app_name=curr['app'],
                    metadata={
                        'from_space': prev['space'],
                        'to_space': curr['space']
                    }
                )
                await self._emit_event(event)

            # Check if resized
            if curr['frame'] != prev['frame']:
                event = YabaiEvent(
                    event_type=YabaiEventType.WINDOW_RESIZED,
                    timestamp=time.time(),
                    space_id=curr['space'],
                    window_id=win_id,
                    app_name=curr['app'],
                    metadata={
                        'old_frame': prev['frame'],
                        'new_frame': curr['frame']
                    }
                )
                await self._emit_event(event)

        self.previous_window_state = current_windows

    def _get_current_focused_window(self) -> Optional[WindowInfo]:
        """Get currently focused window"""
        if self.current_focused_space and self.current_focused_space in self.current_spaces:
            space = self.current_spaces[self.current_focused_space]
            return space.focused_window
        return None

    # ========================================================================
    # Monitoring Methods
    # ========================================================================

    async def start_monitoring(self):
        """Start 24/7 spatial monitoring with event-driven architecture"""
        if self.is_monitoring:
            logger.warning("[YABAI-SI] Already monitoring")
            return

        if not self.yabai_available:
            logger.error("[YABAI-SI] Cannot start monitoring - Yabai not available")
            return

        logger.info("[YABAI-SI] Starting 24/7 spatial monitoring...")
        self.is_monitoring = True

        # Initial scan
        await self._scan_workspace()

        # Start event processing task
        self.event_processing_task = asyncio.create_task(self._process_events())

        # Start continuous monitoring
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())

        logger.info("[YABAI-SI] ✅ 24/7 spatial monitoring active (event-driven)")
        logger.info(f"[YABAI-SI] ✅ Event processing task active")

    async def stop_monitoring(self):
        """Stop spatial monitoring and event processing"""
        if not self.is_monitoring:
            return

        logger.info("[YABAI-SI] Stopping spatial monitoring...")
        self.is_monitoring = False

        # Cancel event processing task
        if self.event_processing_task:
            self.event_processing_task.cancel()
            try:
                await self.event_processing_task
            except asyncio.CancelledError:
                pass

        # Cancel monitoring task
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass

        # Close any active sessions
        await self._close_all_sessions()

        logger.info("[YABAI-SI] ✅ Spatial monitoring stopped")
        logger.info(f"[YABAI-SI] ✅ Event processing stopped (processed {self.metrics['events_processed']} events)")

    async def _monitoring_loop(self):
        """Main 24/7 monitoring loop"""
        logger.info("[YABAI-SI] 24/7 monitoring loop started")

        while self.is_monitoring:
            try:
                # Scan workspace
                await self._scan_workspace()

                # Detect and emit events (Phase 2 event-driven)
                await self._detect_and_emit_events()

                # Track patterns
                await self._track_patterns()

                # Store to Learning DB
                if self.learning_db:
                    await self._store_to_learning_db()

                self.metrics['monitoring_cycles'] += 1

                # Sleep until next cycle
                await asyncio.sleep(self.monitoring_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[YABAI-SI] Error in monitoring loop: {e}", exc_info=True)
                await asyncio.sleep(self.monitoring_interval)

    async def _scan_workspace(self):
        """Scan entire workspace state"""
        try:
            # Query all Spaces
            spaces_data = await self._query_spaces()

            # Query all windows
            windows_data = await self._query_windows()

            # Build SpaceInfo objects
            new_spaces = {}

            for space in spaces_data:
                space_id = space['id']
                space_windows = [
                    w for w in windows_data if w['space'] == space_id
                ]

                # Convert to WindowInfo
                window_infos = []
                focused_window = None

                for w in space_windows:
                    window_info = WindowInfo(
                        window_id=w['id'],
                        app_name=w['app'],
                        title=w.get('title', ''),
                        frame=w['frame'],
                        is_focused=w.get('has-focus', False),
                        is_fullscreen=w.get('is-native-fullscreen', False),
                        space_id=space_id,
                        stack_index=w.get('stack-index', 0)
                    )
                    window_infos.append(window_info)

                    if window_info.is_focused:
                        focused_window = window_info

                space_info = SpaceInfo(
                    space_id=space_id,
                    space_index=space['index'],
                    space_label=space.get('label'),
                    is_visible=space.get('is-visible', False),
                    is_native_fullscreen=space.get('is-native-fullscreen', False),
                    windows=window_infos,
                    focused_window=focused_window
                )

                new_spaces[space_id] = space_info

            # Update state
            self.previous_focused_space = self.current_focused_space
            self.current_spaces = new_spaces

            # Find currently focused Space
            for space_id, space_info in new_spaces.items():
                if space_info.is_visible:
                    self.current_focused_space = space_id
                    break

            # Detect Space transition
            if (self.previous_focused_space is not None and
                self.current_focused_space is not None and
                self.previous_focused_space != self.current_focused_space):
                await self._handle_space_transition(
                    self.previous_focused_space,
                    self.current_focused_space
                )

            # Update metrics
            self.metrics['spaces_monitored'] = len(new_spaces)
            self.metrics['windows_tracked'] = sum(len(s.windows) for s in new_spaces.values())

        except Exception as e:
            logger.error(f"[YABAI-SI] Error scanning workspace: {e}")

    async def _query_spaces(self) -> List[Dict]:
        """
        Query Yabai for all Spaces with robust error handling (v10.6)

        Features:
        - Timeout protection (configurable, default 10s) to prevent hangs
        - Robust JSON parsing with incomplete response handling
        - Strips trailing commas and validates JSON structure
        - Returns empty list on any error (graceful degradation)

        v78.1: Increased timeout from 5s to 10s, made configurable via env var.
        """
        # v78.1: Configurable timeout (Yabai can be slow under memory pressure)
        query_timeout = float(os.getenv('JARVIS_YABAI_QUERY_TIMEOUT', '10.0'))

        try:
            # Execute with timeout protection
            result = await asyncio.create_subprocess_exec(
                'yabai', '-m', 'query', '--spaces',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=query_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"[YABAI-SI] Query spaces timeout ({query_timeout}s), killing process")
                try:
                    result.kill()
                    await result.wait()  # v78.1: Properly wait for process cleanup
                except Exception:
                    pass
                return []

            if result.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.debug(f"[YABAI-SI] Failed to query spaces (code {result.returncode}): {error_msg}")
                return []

            # Robust JSON parsing
            raw_output = stdout.decode().strip()

            if not raw_output:
                logger.debug("[YABAI-SI] Empty response from yabai query --spaces")
                return []

            try:
                # Try direct parsing first
                return json.loads(raw_output)

            except json.JSONDecodeError as json_err:
                # Common issues: incomplete JSON, trailing commas, malformed structure
                logger.debug(f"[YABAI-SI] JSON parse error: {json_err}")

                # Attempt to fix common issues
                fixed_output = raw_output

                # Issue 1: Incomplete JSON (truncated response)
                # Example: '[{...},{...' → '[{...},{...}]'
                if not fixed_output.endswith(']') and fixed_output.startswith('['):
                    # Find last complete JSON object
                    last_brace = fixed_output.rfind('}')
                    if last_brace > 0:
                        fixed_output = fixed_output[:last_brace + 1] + ']'
                        logger.debug("[YABAI-SI] Fixed incomplete JSON (added closing bracket)")

                # Issue 2: Trailing comma before closing bracket
                # Example: '[{...},{...},]' → '[{...},{...}]'
                fixed_output = fixed_output.replace(',]', ']').replace(',}', '}')

                # Try parsing again
                try:
                    spaces = json.loads(fixed_output)
                    logger.debug(f"[YABAI-SI] Successfully parsed after fixes: {len(spaces)} spaces")
                    return spaces

                except json.JSONDecodeError as second_err:
                    # Still can't parse - log for debugging but don't spam errors
                    logger.debug(
                        f"[YABAI-SI] Unable to parse yabai output even after fixes\n"
                        f"  Error: {second_err}\n"
                        f"  Output length: {len(raw_output)} chars\n"
                        f"  First 100 chars: {raw_output[:100] if len(raw_output) > 100 else raw_output}"
                    )
                    return []

        except FileNotFoundError:
            logger.debug("[YABAI-SI] yabai not found in PATH (is it installed?)")
            return []

        except Exception as e:
            # Catch-all for unexpected errors (don't spam logs)
            logger.debug(f"[YABAI-SI] Unexpected error querying spaces: {type(e).__name__}: {e}")
            return []

    async def _query_windows(self) -> List[Dict]:
        """
        Query Yabai for all windows with robust error handling (v10.6)

        Same robust handling as _query_spaces():
        - Timeout protection (configurable, default 10s)
        - JSON parsing with error recovery
        - Graceful degradation

        v78.1: Increased timeout from 5s to 10s, made configurable.
        """
        # v78.1: Configurable timeout (Yabai can be slow with many windows)
        query_timeout = float(os.getenv('JARVIS_YABAI_QUERY_TIMEOUT', '10.0'))

        try:
            # Execute with timeout protection
            result = await asyncio.create_subprocess_exec(
                'yabai', '-m', 'query', '--windows',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=query_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"[YABAI-SI] Query windows timeout ({query_timeout}s), killing process")
                try:
                    result.kill()
                    await result.wait()  # v78.1: Properly wait for process cleanup
                except Exception:
                    pass
                return []

            if result.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.debug(f"[YABAI-SI] Failed to query windows (code {result.returncode}): {error_msg}")
                return []

            # Robust JSON parsing (same logic as _query_spaces)
            raw_output = stdout.decode().strip()

            if not raw_output:
                logger.debug("[YABAI-SI] Empty response from yabai query --windows")
                return []

            try:
                return json.loads(raw_output)

            except json.JSONDecodeError as json_err:
                logger.debug(f"[YABAI-SI] JSON parse error (windows): {json_err}")

                # Attempt to fix common issues
                fixed_output = raw_output

                # Fix incomplete JSON
                if not fixed_output.endswith(']') and fixed_output.startswith('['):
                    last_brace = fixed_output.rfind('}')
                    if last_brace > 0:
                        fixed_output = fixed_output[:last_brace + 1] + ']'
                        logger.debug("[YABAI-SI] Fixed incomplete JSON (windows)")

                # Fix trailing commas
                fixed_output = fixed_output.replace(',]', ']').replace(',}', '}')

                try:
                    windows = json.loads(fixed_output)
                    logger.debug(f"[YABAI-SI] Successfully parsed after fixes: {len(windows)} windows")
                    return windows

                except json.JSONDecodeError:
                    logger.debug("[YABAI-SI] Unable to parse yabai windows output")
                    return []

        except FileNotFoundError:
            logger.debug("[YABAI-SI] yabai not found in PATH")
            return []

        except Exception as e:
            logger.debug(f"[YABAI-SI] Unexpected error querying windows: {type(e).__name__}: {e}")
            return []

    async def _handle_space_transition(self, from_space: int, to_space: int):
        """Handle Space transition"""
        now = time.time()
        dt = datetime.now()

        # Determine trigger app (what was focused before transition)
        trigger_app = None
        if from_space in self.current_spaces:
            from_space_info = self.current_spaces[from_space]
            if from_space_info.focused_window:
                trigger_app = from_space_info.focused_window.app_name

        # Create transition record
        transition = SpaceTransition(
            from_space_id=from_space,
            to_space_id=to_space,
            trigger_app=trigger_app,
            timestamp=now,
            hour_of_day=dt.hour,
            day_of_week=dt.weekday()
        )

        self.space_transition_history.append(transition)
        self.metrics['total_space_changes'] += 1

        logger.info(f"[YABAI-SI] Space transition: {from_space} → {to_space} (trigger: {trigger_app})")

    async def _track_patterns(self):
        """Track usage patterns from current state"""
        now = time.time()
        dt = datetime.now()

        # Track app usage in each Space
        for space_id, space_info in self.current_spaces.items():
            if space_info.focused_window:
                app_name = space_info.focused_window.app_name
                session_key = f"{app_name}_{space_id}"

                if session_key not in self.active_sessions:
                    # Start new session
                    session = AppUsageSession(
                        app_name=app_name,
                        space_id=space_id,
                        start_time=now,
                        end_time=None,
                        duration=None,
                        window_title=space_info.focused_window.title,
                        focus_count=1
                    )
                    self.active_sessions[session_key] = session
                    logger.debug(f"[YABAI-SI] Started session: {app_name} on Space {space_id}")
                else:
                    # Update existing session
                    self.active_sessions[session_key].focus_count += 1

    async def _close_all_sessions(self):
        """Close all active app usage sessions"""
        now = time.time()

        for session_key, session in list(self.active_sessions.items()):
            session.end_time = now
            session.duration = now - session.start_time
            self.session_history.append(session)
            self.metrics['total_sessions_tracked'] += 1

        self.active_sessions.clear()

    async def _store_to_learning_db(self):
        """Store spatial intelligence data to Learning Database"""
        if not self.learning_db:
            return

        try:
            # Store workspace usage
            await self._store_workspace_usage()

            # Store app usage patterns
            await self._store_app_usage_patterns()

            # Store Space transitions
            await self._store_space_transitions()

            # Store temporal patterns (including leap year support!)
            await self._store_temporal_patterns()

        except Exception as e:
            logger.error(f"[YABAI-SI] Error storing to Learning DB: {e}")

    async def _store_workspace_usage(self):
        """Store workspace usage data (Phase 3 enhanced)"""
        for space_id, space_info in self.current_spaces.items():
            if space_info.focused_window:
                try:
                    # Use Phase 3 Learning DB method
                    await self.learning_db.store_workspace_usage(
                        space_id=space_id,
                        app_name=space_info.focused_window.app_name,
                        window_title=space_info.focused_window.title,
                        window_position=space_info.focused_window.frame,
                        focus_duration=self.monitoring_interval,
                        is_fullscreen=space_info.focused_window.is_fullscreen,
                        metadata={'space_index': space_info.space_index}
                    )
                except Exception as e:
                    logger.error(f"[YABAI-SI] Error storing workspace usage: {e}")

    async def _store_app_usage_patterns(self):
        """Store or update app usage patterns (Phase 3 enhanced)"""
        for space_id, space_info in self.current_spaces.items():
            if space_info.focused_window:
                try:
                    # Use Phase 3 Learning DB method
                    await self.learning_db.update_app_usage_pattern(
                        app_name=space_info.focused_window.app_name,
                        space_id=space_id,
                        session_duration=self.monitoring_interval
                    )
                except Exception as e:
                    logger.error(f"[YABAI-SI] Error storing app usage pattern: {e}")

    async def _store_space_transitions(self):
        """Store Space transition data (Phase 3 enhanced)"""
        if not self.space_transition_history:
            return

        # Process recent transitions
        transitions_to_store = list(self.space_transition_history)[-10:]  # Last 10

        for transition in transitions_to_store:
            try:
                # Use Phase 3 Learning DB method
                await self.learning_db.store_space_transition(
                    from_space=transition.from_space_id,
                    to_space=transition.to_space_id,
                    trigger_app=transition.trigger_app,
                    trigger_action='space_change'
                )
            except Exception as e:
                logger.error(f"[YABAI-SI] Error storing space transition: {e}")

    async def _store_temporal_patterns(self):
        """Store temporal patterns including leap year support (Phase 3 enhanced)"""
        now = datetime.now()

        for space_id, space_info in self.current_spaces.items():
            if space_info.focused_window:
                try:
                    # Use Phase 3 Learning DB method (includes leap year support!)
                    await self.learning_db.store_temporal_pattern(
                        pattern_type='app_usage',
                        action_type='focus_app',
                        target=space_info.focused_window.app_name,
                        time_of_day=now.hour,
                        day_of_week=now.weekday(),
                        day_of_month=now.day,
                        month_of_year=now.month,
                        frequency=1,
                        confidence=0.5,
                        metadata={'space_id': space_id}
                    )
                except Exception as e:
                    logger.error(f"[YABAI-SI] Error storing temporal pattern: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get spatial intelligence metrics"""
        return {
            **self.metrics,
            'yabai_available': self.yabai_available,
            'is_monitoring': self.is_monitoring,
            'active_sessions': len(self.active_sessions),
            'current_focused_space': self.current_focused_space
        }


# ============================================================================
# Singleton Instance
# ============================================================================

_yabai_instance: Optional[YabaiSpatialIntelligence] = None


async def get_yabai_intelligence(
    learning_db=None,
    monitoring_interval: float = 5.0,
    enable_24_7_mode: bool = True
) -> YabaiSpatialIntelligence:
    """
    Get singleton Yabai Spatial Intelligence instance

    Args:
        learning_db: Learning Database instance
        monitoring_interval: Monitoring interval
        enable_24_7_mode: Enable 24/7 monitoring

    Returns:
        YabaiSpatialIntelligence instance
    """
    global _yabai_instance

    if _yabai_instance is None:
        _yabai_instance = YabaiSpatialIntelligence(
            learning_db=learning_db,
            monitoring_interval=monitoring_interval,
            enable_24_7_mode=enable_24_7_mode
        )

    return _yabai_instance


# ============================================================================
# Example Usage
# ============================================================================

async def main():
    """Example usage"""
    logging.basicConfig(level=logging.INFO)

    print("\n" + "=" * 80)
    print("Yabai Spatial Intelligence - 24/7 Monitoring")
    print("=" * 80)

    # Initialize
    yabai = await get_yabai_intelligence()

    # Start monitoring
    await yabai.start_monitoring()

    # Monitor for 30 seconds
    print("\nMonitoring workspace for 30 seconds...")
    await asyncio.sleep(30)

    # Get metrics
    metrics = yabai.get_metrics()
    print(f"\n📊 Metrics:")
    print(f"   Spaces monitored: {metrics['spaces_monitored']}")
    print(f"   Windows tracked: {metrics['windows_tracked']}")
    print(f"   Space changes: {metrics['total_space_changes']}")
    print(f"   Monitoring cycles: {metrics['monitoring_cycles']}")

    # Stop
    await yabai.stop_monitoring()

    print("\n" + "=" * 80)
    print("✅ Demo complete!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
