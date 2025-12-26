"""
JARVIS Computer Use Cross-Repo Bridge
=====================================

Enables Computer Use capabilities across JARVIS, JARVIS Prime, and Reactor Core.

Features:
- 3D OS Awareness (Proprioception) - knows which Space/Window is active
- Smart App Switching via Yabai - instant teleportation to any window
- Action Chaining optimization (5x speedup via batch processing)
- OmniParser local UI parsing (60% faster, 80% token reduction)
- Cross-repo Computer Use delegation
- Unified action execution tracking
- Dynamic context injection for LLM prompts

Architecture:
    JARVIS (local) ←→ ~/.jarvis/cross_repo/ ←→ JARVIS Prime (inference)
                              ↓
                        Reactor Core (learning)

    Yabai (Window Manager) ←→ Space Detection ←→ Context Injection
                                    ↓
                           Smart App Switching

Author: JARVIS AI System
Version: 6.2.0 - Clinical-Grade 3D OS Awareness
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Constants
# ============================================================================

COMPUTER_USE_STATE_DIR = Path.home() / ".jarvis" / "cross_repo"
COMPUTER_USE_STATE_FILE = COMPUTER_USE_STATE_DIR / "computer_use_state.json"
COMPUTER_USE_EVENTS_FILE = COMPUTER_USE_STATE_DIR / "computer_use_events.json"
ACTION_CACHE_FILE = COMPUTER_USE_STATE_DIR / "action_cache.json"

# v6.1: OmniParser integration
OMNIPARSER_CACHE_DIR = COMPUTER_USE_STATE_DIR / "omniparser_cache"

# v6.2: 3D OS Awareness (Proprioception)
SPATIAL_CONTEXT_FILE = COMPUTER_USE_STATE_DIR / "spatial_context.json"
APP_LOCATION_CACHE_FILE = COMPUTER_USE_STATE_DIR / "app_location_cache.json"

MAX_EVENTS = 500  # Keep last 500 events
MAX_CACHE_SIZE = 100  # Cache last 100 screen analyses
SPACE_SWITCH_ANIMATION_DELAY = 0.4  # Seconds to wait for macOS space animation
WINDOW_FOCUS_DELAY = 0.15  # Seconds to wait for window focus


# ============================================================================
# Enums
# ============================================================================

class ActionType(Enum):
    """Computer action types."""
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE = "type"
    KEY_PRESS = "key_press"
    SCREENSHOT = "screenshot"
    DRAG = "drag"
    SCROLL = "scroll"
    WAIT = "wait"
    # v6.2: Spatial actions
    SWITCH_SPACE = "switch_space"
    FOCUS_WINDOW = "focus_window"
    SWITCH_APP = "switch_app"


class ExecutionStatus(Enum):
    """Action execution status."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CACHED = "cached"  # Used cached result


class InterfaceType(Enum):
    """Interface type for action chaining optimization."""
    STATIC = "static"  # Calculator, forms, dialogs - can batch
    DYNAMIC = "dynamic"  # Web pages, async UI - must step-by-step


class SwitchResult(Enum):
    """Result of a smart switch operation."""
    SUCCESS = "success"
    ALREADY_FOCUSED = "already_focused"
    SWITCHED_SPACE = "switched_space"
    LAUNCHED_APP = "launched_app"
    FAILED = "failed"
    YABAI_UNAVAILABLE = "yabai_unavailable"


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class ComputerAction:
    """A single computer action."""
    action_id: str
    action_type: ActionType
    coordinates: Optional[Tuple[int, int]] = None
    text: Optional[str] = None
    key: Optional[str] = None
    duration: float = 0.5
    reasoning: str = ""
    confidence: float = 0.0
    element_id: Optional[str] = None  # OmniParser element ID

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "coordinates": list(self.coordinates) if self.coordinates else None,
            "text": self.text,
            "key": self.key,
            "duration": self.duration,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "element_id": self.element_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComputerAction":
        """Create from dictionary."""
        coords = data.get("coordinates")
        return cls(
            action_id=data["action_id"],
            action_type=ActionType(data["action_type"]),
            coordinates=tuple(coords) if coords else None,
            text=data.get("text"),
            key=data.get("key"),
            duration=data.get("duration", 0.5),
            reasoning=data.get("reasoning", ""),
            confidence=data.get("confidence", 0.0),
            element_id=data.get("element_id"),
        )


@dataclass
class ActionBatch:
    """Batch of actions for chained execution."""
    batch_id: str
    actions: List[ComputerAction]
    interface_type: InterfaceType
    goal: str
    screenshot_b64: Optional[str] = None  # Single screenshot for entire batch
    omniparser_elements: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "batch_id": self.batch_id,
            "actions": [a.to_dict() for a in self.actions],
            "interface_type": self.interface_type.value,
            "goal": self.goal,
            "screenshot_b64": self.screenshot_b64[:100] if self.screenshot_b64 else None,  # Truncate
            "omniparser_elements": self.omniparser_elements,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActionBatch":
        """Create from dictionary."""
        return cls(
            batch_id=data["batch_id"],
            actions=[ComputerAction.from_dict(a) for a in data["actions"]],
            interface_type=InterfaceType(data["interface_type"]),
            goal=data["goal"],
            screenshot_b64=data.get("screenshot_b64"),
            omniparser_elements=data.get("omniparser_elements", []),
        )


@dataclass
class ComputerUseEvent:
    """An event from Computer Use system."""
    event_id: str
    timestamp: str
    event_type: str  # "action_executed", "batch_completed", "vision_analysis", "error"

    # Action data
    action: Optional[ComputerAction] = None
    batch: Optional[ActionBatch] = None

    # Execution results
    status: ExecutionStatus = ExecutionStatus.PENDING
    execution_time_ms: float = 0.0
    error_message: str = ""

    # Vision analysis
    vision_analysis: Optional[Dict[str, Any]] = None
    used_omniparser: bool = False

    # Context
    goal: str = ""
    session_id: str = ""
    repo_source: str = "jarvis"  # jarvis, jarvis-prime, reactor-core

    # Optimization metrics
    token_savings: int = 0  # Tokens saved vs non-optimized approach
    time_savings_ms: float = 0.0  # Time saved vs Stop-and-Look

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "action": self.action.to_dict() if self.action else None,
            "batch": self.batch.to_dict() if self.batch else None,
            "status": self.status.value,
            "execution_time_ms": self.execution_time_ms,
            "error_message": self.error_message,
            "vision_analysis": self.vision_analysis,
            "used_omniparser": self.used_omniparser,
            "goal": self.goal,
            "session_id": self.session_id,
            "repo_source": self.repo_source,
            "token_savings": self.token_savings,
            "time_savings_ms": self.time_savings_ms,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComputerUseEvent":
        """Create from dictionary."""
        action_data = data.get("action")
        batch_data = data.get("batch")

        return cls(
            event_id=data["event_id"],
            timestamp=data["timestamp"],
            event_type=data["event_type"],
            action=ComputerAction.from_dict(action_data) if action_data else None,
            batch=ActionBatch.from_dict(batch_data) if batch_data else None,
            status=ExecutionStatus(data.get("status", "pending")),
            execution_time_ms=data.get("execution_time_ms", 0.0),
            error_message=data.get("error_message", ""),
            vision_analysis=data.get("vision_analysis"),
            used_omniparser=data.get("used_omniparser", False),
            goal=data.get("goal", ""),
            session_id=data.get("session_id", ""),
            repo_source=data.get("repo_source", "jarvis"),
            token_savings=data.get("token_savings", 0),
            time_savings_ms=data.get("time_savings_ms", 0.0),
        )


@dataclass
class ComputerUseBridgeState:
    """State of the Computer Use bridge."""
    session_id: str
    started_at: str
    last_update: str

    # Capabilities
    action_chaining_enabled: bool = True
    omniparser_enabled: bool = False
    omniparser_initialized: bool = False
    spatial_awareness_enabled: bool = True  # v6.2

    # Statistics
    total_actions: int = 0
    total_batches: int = 0
    avg_batch_size: float = 0.0
    total_time_saved_ms: float = 0.0
    total_tokens_saved: int = 0
    total_space_switches: int = 0  # v6.2
    total_window_focuses: int = 0  # v6.2

    # Connected repos
    connected_to_prime: bool = False
    connected_to_reactor: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComputerUseBridgeState":
        """Create from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ============================================================================
# v6.2: Spatial Context Data Models (3D OS Awareness / Proprioception)
# ============================================================================

@dataclass
class WindowInfo:
    """Information about a window."""
    window_id: int
    app_name: str
    title: str
    space_id: int
    display_id: int
    is_focused: bool = False
    is_visible: bool = True
    is_minimized: bool = False
    frame: Optional[Dict[str, float]] = None  # x, y, w, h

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_yabai(cls, data: Dict[str, Any]) -> "WindowInfo":
        """Create from yabai window query."""
        return cls(
            window_id=data.get("id", 0),
            app_name=data.get("app", "Unknown"),
            title=data.get("title", ""),
            space_id=data.get("space", 1),
            display_id=data.get("display", 1),
            is_focused=data.get("has-focus", False),
            is_visible=data.get("is-visible", True),
            is_minimized=data.get("is-minimized", False),
            frame=data.get("frame"),
        )


@dataclass
class SpaceInfo:
    """Information about a Mission Control space."""
    space_id: int
    display_id: int
    is_focused: bool = False
    is_visible: bool = False
    is_fullscreen: bool = False
    window_count: int = 0
    windows: List[WindowInfo] = field(default_factory=list)
    primary_app: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "space_id": self.space_id,
            "display_id": self.display_id,
            "is_focused": self.is_focused,
            "is_visible": self.is_visible,
            "is_fullscreen": self.is_fullscreen,
            "window_count": self.window_count,
            "windows": [w.to_dict() for w in self.windows],
            "primary_app": self.primary_app,
        }


@dataclass
class SpatialContext:
    """
    Complete spatial context for 3D OS Awareness.
    This is the "proprioception" - knowing where JARVIS is in the OS.
    """
    timestamp: str
    current_space_id: int
    current_display_id: int
    focused_window: Optional[WindowInfo] = None
    focused_app: str = ""
    total_spaces: int = 0
    total_windows: int = 0
    spaces: List[SpaceInfo] = field(default_factory=list)
    app_locations: Dict[str, List[int]] = field(default_factory=dict)  # app_name -> [space_ids]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "current_space_id": self.current_space_id,
            "current_display_id": self.current_display_id,
            "focused_window": self.focused_window.to_dict() if self.focused_window else None,
            "focused_app": self.focused_app,
            "total_spaces": self.total_spaces,
            "total_windows": self.total_windows,
            "spaces": [s.to_dict() for s in self.spaces],
            "app_locations": self.app_locations,
        }

    def get_context_prompt(self) -> str:
        """Generate context string for LLM prompt injection."""
        lines = [
            f"Current Space: {self.current_space_id} of {self.total_spaces}",
            f"Active Window: {self.focused_app}" + (f' - "{self.focused_window.title[:50]}"' if self.focused_window and self.focused_window.title else ""),
            f"Total Windows: {self.total_windows}",
        ]

        # Add app locations for relevant apps
        if self.app_locations:
            app_info = []
            for app, spaces in list(self.app_locations.items())[:5]:  # Top 5 apps
                if len(spaces) == 1:
                    app_info.append(f"{app} (Space {spaces[0]})")
                else:
                    app_info.append(f"{app} (Spaces {', '.join(map(str, spaces))})")
            if app_info:
                lines.append(f"App Locations: {'; '.join(app_info)}")

        return " | ".join(lines)

    def find_app(self, app_name: str) -> Optional[Tuple[int, int]]:
        """
        Find an app's location.
        Returns (space_id, window_id) or None if not found.
        """
        app_lower = app_name.lower()
        for space in self.spaces:
            for window in space.windows:
                if app_lower in window.app_name.lower():
                    return (space.space_id, window.window_id)
        return None


@dataclass
class SwitchOperation:
    """Result of a switch_to_app_smart operation."""
    result: SwitchResult
    app_name: str
    from_space: int
    to_space: int
    window_id: Optional[int] = None
    execution_time_ms: float = 0.0
    narration: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result": self.result.value,
            "app_name": self.app_name,
            "from_space": self.from_space,
            "to_space": self.to_space,
            "window_id": self.window_id,
            "execution_time_ms": self.execution_time_ms,
            "narration": self.narration,
        }


# ============================================================================
# v6.2: Spatial Awareness Manager (3D OS Awareness / Proprioception)
# ============================================================================

class SpatialAwarenessManager:
    """
    Manages 3D OS Awareness for JARVIS Computer Use.

    This is the "proprioception" layer - JARVIS always knows:
    - Which Space it's currently on
    - Which Window is focused
    - Where every app is located across all Spaces

    Features:
    - Real-time spatial context via Yabai
    - Smart app switching with teleportation
    - Voice narration of spatial actions
    - Cross-repo context sharing
    - App location caching for instant lookups
    """

    def __init__(self, enable_voice: bool = True):
        """
        Initialize spatial awareness.

        Args:
            enable_voice: Enable voice narration for spatial actions
        """
        self._yabai_detector = None
        self._tts_callback: Optional[Callable[[str], Awaitable[None]]] = None
        self._enable_voice = enable_voice
        self._context_cache: Optional[SpatialContext] = None
        self._cache_timestamp: float = 0.0
        self._cache_ttl: float = 1.0  # Cache valid for 1 second
        self._app_location_cache: Dict[str, Tuple[int, int]] = {}  # app -> (space, window)
        self._initialized = False

        # Ensure state directory exists
        COMPUTER_USE_STATE_DIR.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> bool:
        """Initialize the spatial awareness system."""
        if self._initialized:
            return True

        try:
            # Import Yabai detector
            from vision.yabai_space_detector import get_yabai_detector
            self._yabai_detector = get_yabai_detector()

            if not self._yabai_detector.is_available():
                logger.warning("[SPATIAL] Yabai not available - spatial awareness limited")
                return False

            # Load cached app locations
            await self._load_app_location_cache()

            # Initialize TTS if enabled
            if self._enable_voice:
                await self._init_voice()

            self._initialized = True
            logger.info("[SPATIAL] 3D OS Awareness initialized successfully")
            return True

        except Exception as e:
            logger.error(f"[SPATIAL] Failed to initialize: {e}")
            return False

    async def _init_voice(self) -> None:
        """Initialize voice narration callback."""
        try:
            # Use the existing TTS system
            async def speak(message: str) -> None:
                try:
                    # Use macOS say command directly for instant feedback
                    proc = await asyncio.create_subprocess_exec(
                        "say", "-v", "Daniel", "-r", "180", message,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    # Don't wait - fire and forget for responsiveness
                    asyncio.create_task(proc.wait())
                except Exception as e:
                    logger.debug(f"[SPATIAL] Voice narration error: {e}")

            self._tts_callback = speak
            logger.debug("[SPATIAL] Voice narration initialized (Daniel)")
        except Exception as e:
            logger.warning(f"[SPATIAL] Could not initialize voice: {e}")

    async def _narrate(self, message: str) -> None:
        """Narrate a message if voice is enabled."""
        if self._tts_callback and self._enable_voice:
            await self._tts_callback(message)

    def set_voice_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Set custom voice callback."""
        self._tts_callback = callback

    async def get_current_context(self, force_refresh: bool = False) -> Optional[SpatialContext]:
        """
        Get current spatial context (proprioception).

        Args:
            force_refresh: Force refresh even if cache is valid

        Returns:
            SpatialContext with complete OS spatial awareness
        """
        # Check cache
        if not force_refresh and self._context_cache:
            if time.time() - self._cache_timestamp < self._cache_ttl:
                return self._context_cache

        if not self._yabai_detector or not self._yabai_detector.is_available():
            return None

        try:
            start_time = time.time()

            # Query Yabai for spaces and windows
            spaces_data = await self._run_yabai_query("--spaces")
            windows_data = await self._run_yabai_query("--windows")

            if not spaces_data:
                return None

            # Build spatial context
            spaces: List[SpaceInfo] = []
            app_locations: Dict[str, List[int]] = {}
            current_space_id = 1
            current_display_id = 1
            focused_window: Optional[WindowInfo] = None
            focused_app = ""
            total_windows = len(windows_data) if windows_data else 0

            for space_data in spaces_data:
                space_id = space_data.get("index", 1)
                display_id = space_data.get("display", 1)
                is_focused = space_data.get("has-focus", False)

                if is_focused:
                    current_space_id = space_id
                    current_display_id = display_id

                # Get windows for this space
                space_windows: List[WindowInfo] = []
                if windows_data:
                    for win_data in windows_data:
                        if win_data.get("space") == space_id:
                            window = WindowInfo.from_yabai(win_data)
                            space_windows.append(window)

                            # Track app locations
                            app_name = window.app_name
                            if app_name not in app_locations:
                                app_locations[app_name] = []
                            if space_id not in app_locations[app_name]:
                                app_locations[app_name].append(space_id)

                            # Track focused window
                            if win_data.get("has-focus", False):
                                focused_window = window
                                focused_app = app_name

                            # Update location cache
                            self._app_location_cache[app_name.lower()] = (space_id, window.window_id)

                # Determine primary app for space
                primary_app = ""
                if space_windows:
                    # Most common app in the space
                    app_counts: Dict[str, int] = {}
                    for w in space_windows:
                        app_counts[w.app_name] = app_counts.get(w.app_name, 0) + 1
                    primary_app = max(app_counts.keys(), key=lambda k: app_counts[k])

                spaces.append(SpaceInfo(
                    space_id=space_id,
                    display_id=display_id,
                    is_focused=is_focused,
                    is_visible=space_data.get("is-visible", False),
                    is_fullscreen=space_data.get("is-native-fullscreen", False),
                    window_count=len(space_windows),
                    windows=space_windows,
                    primary_app=primary_app,
                ))

            context = SpatialContext(
                timestamp=datetime.now().isoformat(),
                current_space_id=current_space_id,
                current_display_id=current_display_id,
                focused_window=focused_window,
                focused_app=focused_app,
                total_spaces=len(spaces),
                total_windows=total_windows,
                spaces=spaces,
                app_locations=app_locations,
            )

            # Update cache
            self._context_cache = context
            self._cache_timestamp = time.time()

            # Save to cross-repo state file
            await self._write_spatial_context(context)

            elapsed_ms = (time.time() - start_time) * 1000
            logger.debug(f"[SPATIAL] Context refreshed in {elapsed_ms:.1f}ms")

            return context

        except Exception as e:
            logger.error(f"[SPATIAL] Error getting context: {e}")
            return None

    async def switch_to_app_smart(
        self,
        app_name: str,
        narrate: bool = True,
    ) -> SwitchOperation:
        """
        Smart app switching with Yabai teleportation.

        This is the key function that enables 3D movement:
        1. Find where the app is (which Space, which Window)
        2. Teleport to that Space if needed
        3. Focus the Window
        4. Narrate the action in real-time

        Args:
            app_name: Name of the app to switch to (e.g., "Chrome", "Cursor")
            narrate: Whether to narrate the action

        Returns:
            SwitchOperation with result details
        """
        start_time = time.time()

        # Get current context
        context = await self.get_current_context(force_refresh=True)
        if not context:
            # Yabai not available - fallback to open command
            if narrate:
                await self._narrate(f"Opening {app_name}")
            await self._open_app_fallback(app_name)
            return SwitchOperation(
                result=SwitchResult.YABAI_UNAVAILABLE,
                app_name=app_name,
                from_space=1,
                to_space=1,
                narration=f"Opening {app_name}",
            )

        current_space = context.current_space_id

        # Find the app
        location = context.find_app(app_name)

        if not location:
            # App not running - launch it
            if narrate:
                await self._narrate(f"Launching {app_name}")
            await self._open_app_fallback(app_name)

            elapsed_ms = (time.time() - start_time) * 1000
            return SwitchOperation(
                result=SwitchResult.LAUNCHED_APP,
                app_name=app_name,
                from_space=current_space,
                to_space=current_space,
                execution_time_ms=elapsed_ms,
                narration=f"Launching {app_name}",
            )

        target_space, target_window = location

        # Check if already focused
        if context.focused_window and context.focused_window.window_id == target_window:
            if narrate:
                await self._narrate(f"{app_name} is already active")
            elapsed_ms = (time.time() - start_time) * 1000
            return SwitchOperation(
                result=SwitchResult.ALREADY_FOCUSED,
                app_name=app_name,
                from_space=current_space,
                to_space=current_space,
                window_id=target_window,
                execution_time_ms=elapsed_ms,
                narration=f"{app_name} is already active",
            )

        # Need to switch space?
        if current_space != target_space:
            if narrate:
                await self._narrate(f"Switching to Space {target_space} for {app_name}")

            # Teleport to space
            await self._run_yabai_command(f"-m space --focus {target_space}")
            await asyncio.sleep(SPACE_SWITCH_ANIMATION_DELAY)

        # Focus the window
        await self._run_yabai_command(f"-m window --focus {target_window}")
        await asyncio.sleep(WINDOW_FOCUS_DELAY)

        if narrate and current_space != target_space:
            await self._narrate(f"{app_name} is now active on Space {target_space}")
        elif narrate:
            await self._narrate(f"Focused on {app_name}")

        elapsed_ms = (time.time() - start_time) * 1000

        result = SwitchResult.SWITCHED_SPACE if current_space != target_space else SwitchResult.SUCCESS
        narration = f"Switched to {app_name}" + (f" on Space {target_space}" if current_space != target_space else "")

        return SwitchOperation(
            result=result,
            app_name=app_name,
            from_space=current_space,
            to_space=target_space,
            window_id=target_window,
            execution_time_ms=elapsed_ms,
            narration=narration,
        )

    async def find_window(self, app_name: str) -> Optional[Tuple[int, int]]:
        """
        Find a window by app name.

        Args:
            app_name: Name of the app

        Returns:
            (space_id, window_id) or None if not found
        """
        # Check cache first
        app_lower = app_name.lower()
        if app_lower in self._app_location_cache:
            return self._app_location_cache[app_lower]

        # Refresh context and search
        context = await self.get_current_context(force_refresh=True)
        if context:
            return context.find_app(app_name)
        return None

    async def _run_yabai_query(self, query_type: str) -> Optional[List[Dict[str, Any]]]:
        """Run a yabai query command asynchronously."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "yabai", "-m", "query", query_type,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                return json.loads(stdout.decode())
            return None
        except Exception as e:
            logger.error(f"[SPATIAL] Yabai query error: {e}")
            return None

    async def _run_yabai_command(self, command: str) -> bool:
        """Run a yabai command asynchronously."""
        try:
            args = ["yabai"] + command.split()
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return proc.returncode == 0
        except Exception as e:
            logger.error(f"[SPATIAL] Yabai command error: {e}")
            return False

    async def _open_app_fallback(self, app_name: str) -> None:
        """Fallback to open command when yabai can't find the app."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", app_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as e:
            logger.error(f"[SPATIAL] Failed to open {app_name}: {e}")

    async def _write_spatial_context(self, context: SpatialContext) -> None:
        """Write spatial context to cross-repo state file."""
        try:
            SPATIAL_CONTEXT_FILE.write_text(json.dumps(context.to_dict(), indent=2))
        except Exception as e:
            logger.warning(f"[SPATIAL] Failed to write context file: {e}")

    async def _load_app_location_cache(self) -> None:
        """Load app location cache from file."""
        try:
            if APP_LOCATION_CACHE_FILE.exists():
                data = json.loads(APP_LOCATION_CACHE_FILE.read_text())
                self._app_location_cache = {
                    k: tuple(v) for k, v in data.items()
                }
                logger.debug(f"[SPATIAL] Loaded {len(self._app_location_cache)} cached app locations")
        except Exception as e:
            logger.warning(f"[SPATIAL] Failed to load app location cache: {e}")

    async def _save_app_location_cache(self) -> None:
        """Save app location cache to file."""
        try:
            data = {k: list(v) for k, v in self._app_location_cache.items()}
            APP_LOCATION_CACHE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"[SPATIAL] Failed to save app location cache: {e}")


# Global spatial awareness instance
_spatial_manager: Optional[SpatialAwarenessManager] = None


async def get_spatial_manager(enable_voice: bool = True) -> SpatialAwarenessManager:
    """Get or create the global spatial awareness manager."""
    global _spatial_manager
    if _spatial_manager is None:
        _spatial_manager = SpatialAwarenessManager(enable_voice=enable_voice)
        await _spatial_manager.initialize()
    return _spatial_manager


async def get_current_context(force_refresh: bool = False) -> Optional[SpatialContext]:
    """Convenience function to get current spatial context."""
    manager = await get_spatial_manager()
    return await manager.get_current_context(force_refresh=force_refresh)


async def switch_to_app_smart(app_name: str, narrate: bool = True) -> SwitchOperation:
    """Convenience function for smart app switching."""
    manager = await get_spatial_manager()
    return await manager.switch_to_app_smart(app_name, narrate=narrate)


# ============================================================================
# Computer Use Bridge
# ============================================================================

class ComputerUseBridge:
    """
    Cross-repo bridge for Computer Use capabilities.

    Features:
    - Action Chaining optimization tracking
    - OmniParser integration state sharing
    - Cross-repo action delegation
    - Unified vision analysis caching
    - Performance metrics aggregation
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        enable_action_chaining: bool = True,
        enable_omniparser: bool = False,
    ):
        """
        Initialize Computer Use bridge.

        Args:
            session_id: Unique session ID
            enable_action_chaining: Enable batch action optimization
            enable_omniparser: Enable OmniParser local UI parsing
        """
        self.session_id = session_id or f"cu-{int(time.time())}"

        self.state = ComputerUseBridgeState(
            session_id=self.session_id,
            started_at=datetime.now().isoformat(),
            last_update=datetime.now().isoformat(),
            action_chaining_enabled=enable_action_chaining,
            omniparser_enabled=enable_omniparser,
        )

        self._events: List[ComputerUseEvent] = []
        self._initialized = False

        # Ensure state directory exists
        COMPUTER_USE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        if enable_omniparser:
            OMNIPARSER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        """Initialize the bridge."""
        if self._initialized:
            return

        logger.info(f"Initializing Computer Use bridge (session={self.session_id})")

        # Load existing events
        await self._load_events()

        # Write initial state
        await self._write_state()

        self._initialized = True
        logger.info(
            f"Computer Use bridge initialized "
            f"(action_chaining={self.state.action_chaining_enabled}, "
            f"omniparser={self.state.omniparser_enabled})"
        )

    async def emit_action_event(
        self,
        action: ComputerAction,
        status: ExecutionStatus,
        execution_time_ms: float,
        goal: str = "",
        error_message: str = "",
    ) -> None:
        """Emit an action execution event."""
        event = ComputerUseEvent(
            event_id=f"{self.session_id}-action-{len(self._events)}",
            timestamp=datetime.now().isoformat(),
            event_type="action_executed",
            action=action,
            status=status,
            execution_time_ms=execution_time_ms,
            error_message=error_message,
            goal=goal,
            session_id=self.session_id,
            repo_source="jarvis",
        )

        await self._add_event(event)
        self.state.total_actions += 1
        await self._write_state()

    async def emit_batch_event(
        self,
        batch: ActionBatch,
        status: ExecutionStatus,
        execution_time_ms: float,
        time_saved_ms: float = 0.0,
        tokens_saved: int = 0,
        error_message: str = "",
    ) -> None:
        """Emit a batch execution event."""
        event = ComputerUseEvent(
            event_id=f"{self.session_id}-batch-{len(self._events)}",
            timestamp=datetime.now().isoformat(),
            event_type="batch_completed",
            batch=batch,
            status=status,
            execution_time_ms=execution_time_ms,
            error_message=error_message,
            goal=batch.goal,
            session_id=self.session_id,
            repo_source="jarvis",
            time_savings_ms=time_saved_ms,
            token_savings=tokens_saved,
        )

        await self._add_event(event)
        self.state.total_batches += 1
        self.state.total_time_saved_ms += time_saved_ms
        self.state.total_tokens_saved += tokens_saved

        # Update avg batch size
        if self.state.total_batches > 0:
            self.state.avg_batch_size = self.state.total_actions / self.state.total_batches

        await self._write_state()

    async def emit_vision_event(
        self,
        analysis: Dict[str, Any],
        used_omniparser: bool = False,
        tokens_saved: int = 0,
        goal: str = "",
    ) -> None:
        """Emit a vision analysis event."""
        event = ComputerUseEvent(
            event_id=f"{self.session_id}-vision-{len(self._events)}",
            timestamp=datetime.now().isoformat(),
            event_type="vision_analysis",
            status=ExecutionStatus.COMPLETED,
            vision_analysis=analysis,
            used_omniparser=used_omniparser,
            goal=goal,
            session_id=self.session_id,
            repo_source="jarvis",
            token_savings=tokens_saved,
        )

        await self._add_event(event)

        if used_omniparser:
            self.state.omniparser_initialized = True
            self.state.total_tokens_saved += tokens_saved

        await self._write_state()

    def get_statistics(self) -> Dict[str, Any]:
        """Get optimization statistics."""
        return {
            "session_id": self.session_id,
            "total_actions": self.state.total_actions,
            "total_batches": self.state.total_batches,
            "avg_batch_size": round(self.state.avg_batch_size, 2),
            "time_saved_ms": round(self.state.total_time_saved_ms, 0),
            "time_saved_seconds": round(self.state.total_time_saved_ms / 1000, 2),
            "tokens_saved": self.state.total_tokens_saved,
            "action_chaining_enabled": self.state.action_chaining_enabled,
            "omniparser_enabled": self.state.omniparser_enabled,
            "omniparser_initialized": self.state.omniparser_initialized,
        }

    async def get_recent_events(
        self,
        limit: int = 50,
        event_type: Optional[str] = None,
    ) -> List[ComputerUseEvent]:
        """Get recent Computer Use events."""
        events = self._events[-limit:]

        if event_type:
            events = [e for e in events if e.event_type == event_type]

        return events

    async def _add_event(self, event: ComputerUseEvent) -> None:
        """Add event to history."""
        self._events.append(event)

        # Trim to MAX_EVENTS
        if len(self._events) > MAX_EVENTS:
            self._events = self._events[-MAX_EVENTS:]

        await self._write_events()

    async def _write_state(self) -> None:
        """Write current state to file."""
        try:
            self.state.last_update = datetime.now().isoformat()
            COMPUTER_USE_STATE_FILE.write_text(
                json.dumps(self.state.to_dict(), indent=2)
            )
        except Exception as e:
            logger.warning(f"Failed to write Computer Use state: {e}")

    async def _write_events(self) -> None:
        """Write events to file."""
        try:
            events_data = [e.to_dict() for e in self._events]
            COMPUTER_USE_EVENTS_FILE.write_text(
                json.dumps(events_data, indent=2)
            )
        except Exception as e:
            logger.warning(f"Failed to write Computer Use events: {e}")

    async def _load_events(self) -> None:
        """Load existing events from file."""
        try:
            if COMPUTER_USE_EVENTS_FILE.exists():
                content = COMPUTER_USE_EVENTS_FILE.read_text()
                events_data = json.loads(content)
                self._events = [
                    ComputerUseEvent.from_dict(e) for e in events_data[-MAX_EVENTS:]
                ]
                logger.info(f"Loaded {len(self._events)} Computer Use events")
        except Exception as e:
            logger.warning(f"Failed to load Computer Use events: {e}")
            self._events = []


# ============================================================================
# Global Instance
# ============================================================================

_bridge_instance: Optional[ComputerUseBridge] = None


async def get_computer_use_bridge(
    enable_action_chaining: bool = True,
    enable_omniparser: bool = False,
) -> ComputerUseBridge:
    """Get or create the global Computer Use bridge."""
    global _bridge_instance

    if _bridge_instance is None:
        _bridge_instance = ComputerUseBridge(
            enable_action_chaining=enable_action_chaining,
            enable_omniparser=enable_omniparser,
        )
        await _bridge_instance.initialize()

    return _bridge_instance


def get_bridge() -> Optional[ComputerUseBridge]:
    """Get the bridge instance (sync)."""
    return _bridge_instance


# ============================================================================
# Convenience Functions
# ============================================================================

async def emit_action_event(
    action: ComputerAction,
    status: ExecutionStatus,
    execution_time_ms: float,
    goal: str = "",
    error_message: str = "",
) -> None:
    """Emit action event if bridge is active."""
    bridge = get_bridge()
    if bridge:
        await bridge.emit_action_event(
            action, status, execution_time_ms, goal, error_message
        )


async def emit_batch_event(
    batch: ActionBatch,
    status: ExecutionStatus,
    execution_time_ms: float,
    time_saved_ms: float = 0.0,
    tokens_saved: int = 0,
    error_message: str = "",
) -> None:
    """Emit batch event if bridge is active."""
    bridge = get_bridge()
    if bridge:
        await bridge.emit_batch_event(
            batch, status, execution_time_ms, time_saved_ms, tokens_saved, error_message
        )


async def emit_vision_event(
    analysis: Dict[str, Any],
    used_omniparser: bool = False,
    tokens_saved: int = 0,
    goal: str = "",
) -> None:
    """Emit vision analysis event if bridge is active."""
    bridge = get_bridge()
    if bridge:
        await bridge.emit_vision_event(analysis, used_omniparser, tokens_saved, goal)


def get_statistics() -> Dict[str, Any]:
    """Get optimization statistics if bridge is active."""
    bridge = get_bridge()
    if bridge:
        return bridge.get_statistics()
    return {}
