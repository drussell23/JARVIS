"""
UAE (Unified Autonomous Execution) Context Manager
===================================================

Provides continuous context management for autonomous execution:
- Vision-based state updates during execution
- Screen state change detection
- Element position learning
- Multi-space context maintenance
- Real-time context enrichment

v1.0: Initial implementation with screen state tracking and context updates.

Author: JARVIS AI System
"""

import asyncio
import base64
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class UAEContextConfig:
    """Configuration for the UAE Context Manager."""

    # Update settings
    continuous_update: bool = field(
        default_factory=lambda: os.getenv("JARVIS_UAE_CONTINUOUS", "true").lower() == "true"
    )
    update_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_UAE_UPDATE_INTERVAL", "2.0"))
    )
    max_history_depth: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_UAE_HISTORY_DEPTH", "10"))
    )

    # Screen capture settings
    screen_capture_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_UAE_SCREEN_CAPTURE", "true").lower() == "true"
    )
    capture_on_change_only: bool = field(
        default_factory=lambda: os.getenv("JARVIS_UAE_CHANGE_ONLY", "false").lower() == "true"
    )

    # Change detection settings
    change_detection_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_UAE_CHANGE_DETECT", "true").lower() == "true"
    )
    change_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_UAE_CHANGE_THRESHOLD", "0.05"))
    )

    # Element tracking settings
    element_tracking_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_UAE_ELEMENT_TRACK", "true").lower() == "true"
    )
    max_tracked_elements: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_UAE_MAX_ELEMENTS", "50"))
    )


# =============================================================================
# Context Types
# =============================================================================


class ContextScope(Enum):
    """Scope of context information."""

    GLOBAL = auto()  # System-wide context
    WORKSPACE = auto()  # Current desktop/space
    APPLICATION = auto()  # Active application
    WINDOW = auto()  # Current window
    ELEMENT = auto()  # Specific UI element


class ChangeType(Enum):
    """Types of detected changes."""

    SCREEN = auto()
    WINDOW_FOCUS = auto()
    WINDOW_POSITION = auto()
    ELEMENT_APPEAR = auto()
    ELEMENT_DISAPPEAR = auto()
    ELEMENT_CHANGE = auto()
    TEXT_CHANGE = auto()


@dataclass
class ScreenState:
    """Current screen state snapshot."""

    timestamp: float
    screen_hash: str
    active_app: str
    active_window: str
    window_bounds: Dict[str, int]
    visible_elements: List[Dict[str, Any]]
    cursor_position: Tuple[int, int]
    screenshot_b64: Optional[str] = None


@dataclass
class ContextChange:
    """Detected context change."""

    change_type: ChangeType
    timestamp: float
    before: Dict[str, Any]
    after: Dict[str, Any]
    confidence: float


@dataclass
class TrackedElement:
    """A tracked UI element."""

    element_id: str
    element_type: str
    label: str
    bounds: Dict[str, int]
    last_seen: float
    seen_count: int
    positions: List[Dict[str, int]]  # History of positions


@dataclass
class ContextSnapshot:
    """Complete context snapshot."""

    timestamp: float
    scope: ContextScope
    screen_state: ScreenState
    tracked_elements: Dict[str, TrackedElement]
    recent_changes: List[ContextChange]
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp,
            "scope": self.scope.name,
            "active_app": self.screen_state.active_app,
            "active_window": self.screen_state.active_window,
            "cursor": self.screen_state.cursor_position,
            "tracked_elements_count": len(self.tracked_elements),
            "recent_changes_count": len(self.recent_changes),
        }


# =============================================================================
# UAE Context Manager
# =============================================================================


class UAEContextManager:
    """
    Manages context for Unified Autonomous Execution.

    Features:
    - Continuous screen state monitoring
    - Change detection and tracking
    - Element position learning
    - Context history management
    - Multi-scope context queries
    """

    def __init__(
        self,
        config: Optional[UAEContextConfig] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize the context manager."""
        self.config = config or UAEContextConfig()
        self.logger = logger or logging.getLogger(__name__)

        # Current context
        self._current_state: Optional[ScreenState] = None
        self._tracked_elements: Dict[str, TrackedElement] = {}

        # History
        self._state_history: List[ScreenState] = []
        self._change_history: List[ContextChange] = []

        # Callbacks
        self._change_callbacks: List[Callable[[ContextChange], None]] = []

        # Update task
        self._update_task: Optional[asyncio.Task] = None
        self._paused = False

        # Statistics
        self._stats = {
            "updates": 0,
            "changes_detected": 0,
            "elements_tracked": 0,
            "screenshots_taken": 0,
        }

        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize the context manager."""
        if self._initialized:
            return True

        try:
            self.logger.info("[UAEContext] Initializing Context Manager...")

            # Take initial snapshot
            await self._update_context()

            # Start continuous update if enabled
            if self.config.continuous_update:
                self._update_task = asyncio.create_task(self._update_loop())

            self._initialized = True
            self.logger.info("[UAEContext] ✓ Context Manager initialized")
            return True

        except Exception as e:
            self.logger.error(f"[UAEContext] Initialization failed: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the context manager."""
        if not self._initialized:
            return

        self.logger.info("[UAEContext] Shutting down...")

        # Stop update loop
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass

        self._initialized = False
        self.logger.info("[UAEContext] ✓ Shutdown complete")

    # =========================================================================
    # Context Updates
    # =========================================================================

    async def _update_loop(self) -> None:
        """Continuous context update loop."""
        iteration_timeout = float(os.getenv("TIMEOUT_UAE_CONTEXT_ITERATION", "30.0"))
        while True:
            try:
                if not self._paused:
                    await asyncio.wait_for(
                        self._update_context(),
                        timeout=iteration_timeout
                    )

                await asyncio.sleep(self.config.update_interval)

            except asyncio.TimeoutError:
                self.logger.warning("[UAEContext] Update iteration timed out")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"[UAEContext] Update error: {e}")
                await asyncio.sleep(self.config.update_interval)

    async def _update_context(self) -> None:
        """Update the current context."""
        async with self._lock:
            new_state = await self._capture_screen_state()

            if new_state:
                # Detect changes
                if self._current_state and self.config.change_detection_enabled:
                    changes = self._detect_changes(self._current_state, new_state)
                    for change in changes:
                        self._change_history.append(change)
                        self._stats["changes_detected"] += 1

                        # Notify callbacks
                        for callback in self._change_callbacks:
                            try:
                                callback(change)
                            except Exception as e:
                                self.logger.error(f"[UAEContext] Callback error: {e}")

                # Update tracking
                if self.config.element_tracking_enabled:
                    self._update_element_tracking(new_state)

                # Store in history
                if self._current_state:
                    self._state_history.append(self._current_state)
                    if len(self._state_history) > self.config.max_history_depth:
                        self._state_history.pop(0)

                self._current_state = new_state
                self._stats["updates"] += 1

    async def _capture_screen_state(self) -> Optional[ScreenState]:
        """Capture current screen state."""
        try:
            # Get active app and window info
            active_app, active_window, bounds = await self._get_active_window_info()

            # Get cursor position
            cursor_pos = await self._get_cursor_position()

            # Get visible elements (placeholder - would use accessibility APIs)
            visible_elements = await self._get_visible_elements()

            # Take screenshot if enabled
            screenshot_b64 = None
            screen_hash = ""
            if self.config.screen_capture_enabled:
                screenshot_b64, screen_hash = await self._capture_screenshot()
                self._stats["screenshots_taken"] += 1

            return ScreenState(
                timestamp=time.time(),
                screen_hash=screen_hash,
                active_app=active_app,
                active_window=active_window,
                window_bounds=bounds,
                visible_elements=visible_elements,
                cursor_position=cursor_pos,
                screenshot_b64=screenshot_b64,
            )

        except Exception as e:
            self.logger.error(f"[UAEContext] Screen capture error: {e}")
            return None

    async def _get_active_window_info(self) -> Tuple[str, str, Dict[str, int]]:
        """Get active window information."""
        try:
            # Use AppleScript on macOS
            import subprocess

            script = '''
                tell application "System Events"
                    set frontApp to first application process whose frontmost is true
                    set appName to name of frontApp
                    set winName to ""
                    try
                        set winName to name of front window of frontApp
                    end try
                end tell
                return appName & "|" & winName
            '''

            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=2,
            )

            if result.returncode == 0:
                parts = result.stdout.strip().split("|")
                app_name = parts[0] if parts else "Unknown"
                window_name = parts[1] if len(parts) > 1 else ""
                return app_name, window_name, {"x": 0, "y": 0, "width": 0, "height": 0}

        except Exception:
            pass

        return "Unknown", "", {"x": 0, "y": 0, "width": 0, "height": 0}

    async def _get_cursor_position(self) -> Tuple[int, int]:
        """Get current cursor position."""
        try:
            import subprocess

            script = '''
                tell application "System Events"
                    set mousePos to do shell script "python3 -c 'from Quartz.CoreGraphics import CGEventGetLocation, CGEventCreate; e = CGEventCreate(None); loc = CGEventGetLocation(e); print(int(loc.x), int(loc.y))'"
                end tell
                return mousePos
            '''

            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=2,
            )

            if result.returncode == 0:
                parts = result.stdout.strip().split()
                if len(parts) >= 2:
                    return int(parts[0]), int(parts[1])

        except Exception:
            pass

        return 0, 0

    async def _get_visible_elements(self) -> List[Dict[str, Any]]:
        """Get visible UI elements (placeholder)."""
        # In a full implementation, this would use accessibility APIs
        return []

    async def _capture_screenshot(self) -> Tuple[Optional[str], str]:
        """Capture a screenshot and return base64 + hash."""
        try:
            import subprocess
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name

            # Capture screenshot
            subprocess.run(
                ["screencapture", "-x", "-t", "png", tmp_path],
                capture_output=True,
                timeout=5,
            )

            # Read and encode
            with open(tmp_path, "rb") as f:
                data = f.read()

            # Clean up
            os.unlink(tmp_path)

            # Calculate hash
            screen_hash = hashlib.md5(data).hexdigest()

            # Encode to base64
            b64 = base64.b64encode(data).decode("utf-8")

            return b64, screen_hash

        except Exception as e:
            self.logger.debug(f"[UAEContext] Screenshot failed: {e}")
            return None, ""

    def _detect_changes(
        self,
        old_state: ScreenState,
        new_state: ScreenState,
    ) -> List[ContextChange]:
        """Detect changes between two states."""
        changes: List[ContextChange] = []

        # Check app/window change
        if old_state.active_app != new_state.active_app:
            changes.append(ContextChange(
                change_type=ChangeType.WINDOW_FOCUS,
                timestamp=new_state.timestamp,
                before={"app": old_state.active_app, "window": old_state.active_window},
                after={"app": new_state.active_app, "window": new_state.active_window},
                confidence=1.0,
            ))

        # Check screen hash change
        if old_state.screen_hash and new_state.screen_hash:
            if old_state.screen_hash != new_state.screen_hash:
                changes.append(ContextChange(
                    change_type=ChangeType.SCREEN,
                    timestamp=new_state.timestamp,
                    before={"hash": old_state.screen_hash},
                    after={"hash": new_state.screen_hash},
                    confidence=0.9,
                ))

        return changes

    def _update_element_tracking(self, state: ScreenState) -> None:
        """Update element position tracking."""
        now = time.time()

        for element in state.visible_elements:
            elem_id = element.get("id", "")
            if not elem_id:
                continue

            bounds = element.get("bounds", {})

            if elem_id in self._tracked_elements:
                tracked = self._tracked_elements[elem_id]
                tracked.last_seen = now
                tracked.seen_count += 1
                tracked.bounds = bounds
                tracked.positions.append(bounds)

                # Keep only recent positions
                if len(tracked.positions) > 20:
                    tracked.positions.pop(0)
            else:
                # New element
                if len(self._tracked_elements) >= self.config.max_tracked_elements:
                    # Remove oldest
                    oldest = min(
                        self._tracked_elements.items(),
                        key=lambda x: x[1].last_seen,
                    )
                    del self._tracked_elements[oldest[0]]

                self._tracked_elements[elem_id] = TrackedElement(
                    element_id=elem_id,
                    element_type=element.get("type", "unknown"),
                    label=element.get("label", ""),
                    bounds=bounds,
                    last_seen=now,
                    seen_count=1,
                    positions=[bounds],
                )
                self._stats["elements_tracked"] += 1

    # =========================================================================
    # Context Queries
    # =========================================================================

    async def get_current_context(self) -> Optional[ContextSnapshot]:
        """Get current context snapshot."""
        if not self._current_state:
            return None

        return ContextSnapshot(
            timestamp=time.time(),
            scope=ContextScope.GLOBAL,
            screen_state=self._current_state,
            tracked_elements=dict(self._tracked_elements),
            recent_changes=self._change_history[-10:],
            metadata={},
        )

    async def get_active_app(self) -> str:
        """Get the currently active application."""
        if self._current_state:
            return self._current_state.active_app
        return "Unknown"

    async def get_active_window(self) -> str:
        """Get the currently active window."""
        if self._current_state:
            return self._current_state.active_window
        return ""

    async def get_cursor_position(self) -> Tuple[int, int]:
        """Get current cursor position."""
        if self._current_state:
            return self._current_state.cursor_position
        return await self._get_cursor_position()

    async def find_element_position(self, element_id: str) -> Optional[Dict[str, int]]:
        """Find the learned position of an element."""
        tracked = self._tracked_elements.get(element_id)
        if tracked:
            return tracked.bounds
        return None

    async def get_recent_changes(self, limit: int = 10) -> List[ContextChange]:
        """Get recent context changes."""
        return self._change_history[-limit:]

    # =========================================================================
    # Control
    # =========================================================================

    def pause(self) -> None:
        """Pause context updates."""
        self._paused = True

    def resume(self) -> None:
        """Resume context updates."""
        self._paused = False

    def register_change_callback(self, callback: Callable[[ContextChange], None]) -> None:
        """Register a callback for context changes."""
        self._change_callbacks.append(callback)

    async def force_update(self) -> None:
        """Force an immediate context update."""
        await self._update_context()

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get context manager statistics."""
        return {
            **self._stats,
            "current_app": self._current_state.active_app if self._current_state else None,
            "tracked_elements": len(self._tracked_elements),
            "history_depth": len(self._state_history),
            "change_history_depth": len(self._change_history),
            "paused": self._paused,
        }

    @property
    def is_ready(self) -> bool:
        """Check if manager is ready."""
        return self._initialized


# =============================================================================
# Module-level Singleton Access
# =============================================================================

_context_manager_instance: Optional[UAEContextManager] = None


def get_uae_context() -> Optional[UAEContextManager]:
    """Get the global UAE context manager."""
    return _context_manager_instance


def set_uae_context(manager: UAEContextManager) -> None:
    """Set the global UAE context manager."""
    global _context_manager_instance
    _context_manager_instance = manager


async def start_uae_context(
    config: Optional[UAEContextConfig] = None,
) -> UAEContextManager:
    """Start and initialize the UAE context manager."""
    global _context_manager_instance

    if _context_manager_instance is not None:
        return _context_manager_instance

    manager = UAEContextManager(config=config)
    await manager.initialize()
    _context_manager_instance = manager

    return manager


async def stop_uae_context() -> None:
    """Stop the global UAE context manager."""
    global _context_manager_instance

    if _context_manager_instance is not None:
        await _context_manager_instance.shutdown()
        _context_manager_instance = None
