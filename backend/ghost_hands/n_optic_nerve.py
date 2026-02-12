"""
N-Optic Nerve: Multi-Window Parallel Vision System
===================================================

The "eyes" of JARVIS Ghost Hands - provides N-stream parallel video monitoring
across all macOS Spaces without requiring focus.

Features:
- Simultaneous monitoring of N windows across all Spaces
- Event detection via OCR/pattern matching (trigger-based)
- Integration with Yabai for Space awareness (MSVI)
- 60 FPS capable via ScreenCaptureKit
- Zero focus stealing - purely passive observation
- Real-time event queue for detected triggers
- Environment-driven configuration

Architecture:
    NOpticNerve (Singleton)
    ├── WindowWatcher (per-window monitoring)
    │   ├── ScreenCaptureKit/Quartz capture
    │   ├── OCR detection (pytesseract)
    │   └── Pattern matching (regex/keywords)
    ├── EventDetector (trigger recognition)
    │   ├── Text triggers ("Success", "Error", etc.)
    │   ├── Visual triggers (icon detection)
    │   └── State change triggers (window changes)
    └── VisionEventQueue (async event bus)

Author: JARVIS AI System
Version: 1.0.0 - Ghost Hands Edition
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Pattern,
    Set,
    Tuple,
    Union,
)

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (Environment-Driven)
# =============================================================================

@dataclass
class NOpticConfig:
    """Environment-driven configuration for N-Optic Nerve."""

    # Capture settings
    target_fps: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_OPTIC_FPS", "10"))
    )
    max_concurrent_watchers: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_OPTIC_MAX_WATCHERS", "10"))
    )
    capture_quality: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_OPTIC_QUALITY", "0.8"))
    )

    # OCR settings
    ocr_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_OPTIC_OCR_ENABLED", "true").lower() == "true"
    )
    ocr_interval_ms: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_OPTIC_OCR_INTERVAL_MS", "500"))
    )
    ocr_language: str = field(
        default_factory=lambda: os.getenv("JARVIS_OPTIC_OCR_LANG", "eng")
    )

    # Event detection
    event_debounce_ms: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_OPTIC_DEBOUNCE_MS", "1000"))
    )
    event_queue_size: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_OPTIC_QUEUE_SIZE", "100"))
    )

    # Performance
    low_power_mode: bool = field(
        default_factory=lambda: os.getenv("JARVIS_OPTIC_LOW_POWER", "false").lower() == "true"
    )

    @property
    def capture_interval_ms(self) -> float:
        """Calculate capture interval from target FPS."""
        return 1000.0 / self.target_fps


# =============================================================================
# Event Types and Data Classes
# =============================================================================

class VisionEventType(Enum):
    """Types of vision events that can be detected."""
    TEXT_DETECTED = auto()
    PATTERN_MATCH = auto()
    STATE_CHANGE = auto()
    WINDOW_APPEARED = auto()
    WINDOW_DISAPPEARED = auto()
    ERROR_DETECTED = auto()
    SUCCESS_DETECTED = auto()
    BUTTON_VISIBLE = auto()
    LOADING_COMPLETE = auto()
    CUSTOM = auto()


class WatcherState(Enum):
    """States of a window watcher."""
    IDLE = auto()
    WATCHING = auto()
    PAUSED = auto()
    TRIGGERED = auto()
    ERROR = auto()
    STOPPED = auto()


@dataclass
class VisionEvent:
    """A detected vision event from monitoring."""
    event_type: VisionEventType
    window_id: int
    space_id: int
    app_name: str
    window_title: str
    detected_text: Optional[str] = None
    matched_pattern: Optional[str] = None
    confidence: float = 1.0
    timestamp: datetime = field(default_factory=datetime.now)
    screenshot: Optional[Image.Image] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "event_type": self.event_type.name,
            "window_id": self.window_id,
            "space_id": self.space_id,
            "app_name": self.app_name,
            "window_title": self.window_title,
            "detected_text": self.detected_text,
            "matched_pattern": self.matched_pattern,
            "confidence": self.confidence,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class WatchTarget:
    """Configuration for a window watch target."""
    window_id: int
    app_name: str
    window_title: str
    space_id: int
    triggers: List["WatchTrigger"] = field(default_factory=list)
    callback: Optional[Callable[[VisionEvent], None]] = None
    priority: int = 5
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class WatchTrigger:
    """A trigger condition for event detection."""
    name: str
    trigger_type: VisionEventType
    pattern: Optional[Union[str, Pattern]] = None
    keywords: Optional[List[str]] = None
    callback: Optional[Callable[[VisionEvent], None]] = None
    one_shot: bool = False
    cooldown_ms: int = 1000
    last_triggered: Optional[datetime] = None

    def matches(self, text: str) -> Tuple[bool, Optional[str]]:
        """Check if text matches this trigger."""
        if not text:
            return False, None

        # Pattern match
        if self.pattern:
            if isinstance(self.pattern, str):
                if self.pattern.lower() in text.lower():
                    return True, self.pattern
            else:
                match = self.pattern.search(text)
                if match:
                    return True, match.group()

        # Keyword match
        if self.keywords:
            text_lower = text.lower()
            for keyword in self.keywords:
                if keyword.lower() in text_lower:
                    return True, keyword

        return False, None

    def can_trigger(self) -> bool:
        """Check if trigger is off cooldown."""
        if self.last_triggered is None:
            return True
        elapsed = (datetime.now() - self.last_triggered).total_seconds() * 1000
        return elapsed >= self.cooldown_ms


# =============================================================================
# OCR Engine
# =============================================================================

class OCREngine:
    """OCR engine for text extraction from screenshots."""

    def __init__(self, language: str = "eng"):
        self.language = language
        self._pytesseract = None
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize OCR engine."""
        if self._initialized:
            return True

        try:
            import pytesseract
            self._pytesseract = pytesseract

            # Verify tesseract is installed
            self._pytesseract.get_tesseract_version()
            self._initialized = True
            logger.info("OCR engine initialized (pytesseract)")
            return True

        except ImportError:
            logger.warning("pytesseract not installed, OCR disabled")
            return False
        except Exception as e:
            logger.warning(f"OCR initialization failed: {e}")
            return False

    async def extract_text(self, image: Image.Image) -> str:
        """Extract text from an image."""
        if not self._initialized:
            if not await self.initialize():
                return ""

        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(
                None,
                lambda: self._pytesseract.image_to_string(
                    image,
                    lang=self.language,
                    config=f"--psm {os.getenv('JARVIS_OPTIC_OCR_PSM', '6')}"
                )
            )
            return text.strip()
        except Exception as e:
            logger.error(f"OCR extraction failed: {e}")
            return ""


# =============================================================================
# Window Watcher
# =============================================================================

class WindowWatcher:
    """
    Monitors a single window for trigger events.

    Uses ScreenCaptureKit (via Quartz) to capture window contents
    and OCR to detect text triggers without requiring focus.
    """

    def __init__(
        self,
        target: WatchTarget,
        config: NOpticConfig,
        ocr_engine: OCREngine,
        event_callback: Callable[[VisionEvent], None],
    ):
        self.target = target
        self.config = config
        self.ocr_engine = ocr_engine
        self.event_callback = event_callback

        self.state = WatcherState.IDLE
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # State tracking
        self._last_text: str = ""
        self._last_capture_time: Optional[datetime] = None
        self._capture_count: int = 0
        self._error_count: int = 0

        # Statistics
        self.stats = {
            "captures": 0,
            "ocr_runs": 0,
            "triggers_fired": 0,
            "errors": 0,
            "start_time": None,
        }

    async def start(self) -> bool:
        """Start watching the window."""
        if self.state == WatcherState.WATCHING:
            return True

        self._stop_event.clear()
        self.stats["start_time"] = datetime.now()
        self.state = WatcherState.WATCHING

        self._task = asyncio.create_task(
            self._watch_loop(),
            name=f"Watcher-{self.target.window_id}"
        )

        logger.info(
            f"[N-OPTIC] Started watching window {self.target.window_id} "
            f"({self.target.app_name}) on Space {self.target.space_id}"
        )
        return True

    async def stop(self) -> None:
        """Stop watching."""
        self._stop_event.set()
        self.state = WatcherState.STOPPED

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        logger.info(f"[N-OPTIC] Stopped watching window {self.target.window_id}")

    async def _watch_loop(self) -> None:
        """Main watching loop."""
        interval_s = self.config.capture_interval_ms / 1000.0
        ocr_interval_s = self.config.ocr_interval_ms / 1000.0
        last_ocr_time = 0.0

        while not self._stop_event.is_set():
            try:
                # Capture window
                screenshot = await self._capture_window()

                if screenshot is None:
                    self._error_count += 1
                    if self._error_count > 5:
                        self.state = WatcherState.ERROR
                        logger.warning(
                            f"[N-OPTIC] Window {self.target.window_id} capture failed repeatedly"
                        )
                        await asyncio.sleep(2.0)
                    continue

                self._error_count = 0
                self.stats["captures"] += 1

                # Run OCR if enabled and interval elapsed
                current_time = time.time()
                if (
                    self.config.ocr_enabled and
                    current_time - last_ocr_time >= ocr_interval_s
                ):
                    await self._process_ocr(screenshot)
                    last_ocr_time = current_time

                await asyncio.sleep(interval_s)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[N-OPTIC] Watch loop error: {e}")
                self.stats["errors"] += 1
                await asyncio.sleep(1.0)

    async def _capture_window(self) -> Optional[Image.Image]:
        """Capture the window without focus."""
        try:
            # Use Quartz to capture specific window
            import Quartz
            from Quartz import (
                CGWindowListCreateImage,
                CGRectNull,
                kCGWindowListOptionIncludingWindow,
                kCGWindowImageDefault,
            )

            # Capture the specific window
            cg_image = CGWindowListCreateImage(
                CGRectNull,
                kCGWindowListOptionIncludingWindow,
                self.target.window_id,
                kCGWindowImageDefault
            )

            if cg_image is None:
                return None

            # Convert to PIL Image
            width = Quartz.CGImageGetWidth(cg_image)
            height = Quartz.CGImageGetHeight(cg_image)

            if width == 0 or height == 0:
                return None

            bytes_per_row = Quartz.CGImageGetBytesPerRow(cg_image)
            data_provider = Quartz.CGImageGetDataProvider(cg_image)
            data = Quartz.CGDataProviderCopyData(data_provider)

            # Convert to numpy array
            arr = np.frombuffer(data, dtype=np.uint8)
            arr = arr.reshape((height, bytes_per_row // 4, 4))
            arr = arr[:, :width, :]

            # BGRA to RGB
            image = Image.fromarray(arr[:, :, [2, 1, 0]])

            self._last_capture_time = datetime.now()
            return image

        except Exception as e:
            logger.debug(f"[N-OPTIC] Capture failed for window {self.target.window_id}: {e}")
            return None

    async def _process_ocr(self, screenshot: Image.Image) -> None:
        """Process OCR and check triggers."""
        text = await self.ocr_engine.extract_text(screenshot)
        self.stats["ocr_runs"] += 1

        if not text:
            return

        # Check if text changed significantly
        if text == self._last_text:
            return

        self._last_text = text

        # Check all triggers
        for trigger in self.target.triggers:
            if not trigger.can_trigger():
                continue

            matched, match_text = trigger.matches(text)

            if matched:
                # Create event
                event = VisionEvent(
                    event_type=trigger.trigger_type,
                    window_id=self.target.window_id,
                    space_id=self.target.space_id,
                    app_name=self.target.app_name,
                    window_title=self.target.window_title,
                    detected_text=text[:500],  # Limit text size
                    matched_pattern=match_text,
                    confidence=0.9,
                    screenshot=screenshot if trigger.one_shot else None,
                    metadata={
                        "trigger_name": trigger.name,
                        "one_shot": trigger.one_shot,
                    }
                )

                # Update trigger state
                trigger.last_triggered = datetime.now()
                self.stats["triggers_fired"] += 1

                # Fire callbacks
                if trigger.callback:
                    try:
                        trigger.callback(event)
                    except Exception as e:
                        logger.error(f"[N-OPTIC] Trigger callback error: {e}")

                if self.target.callback:
                    try:
                        self.target.callback(event)
                    except Exception as e:
                        logger.error(f"[N-OPTIC] Target callback error: {e}")

                # Send to main event callback
                self.event_callback(event)

                # Handle one-shot triggers
                if trigger.one_shot:
                    self.target.triggers.remove(trigger)
                    if not self.target.triggers:
                        # All one-shot triggers fired, stop watching
                        self.state = WatcherState.TRIGGERED
                        await self.stop()
                        return

                logger.info(
                    f"[N-OPTIC] Trigger '{trigger.name}' fired on "
                    f"window {self.target.window_id}: {match_text}"
                )


# =============================================================================
# N-Optic Nerve (Main Class)
# =============================================================================

class NOpticNerve:
    """
    N-Optic Nerve: Multi-window parallel vision monitoring system.

    Provides:
    - Simultaneous monitoring of multiple windows across all Spaces
    - Event detection via OCR and pattern matching
    - Integration with Yabai for Space awareness
    - Zero focus stealing - passive observation only
    """

    _instance: Optional["NOpticNerve"] = None

    def __new__(cls, config: Optional[NOpticConfig] = None):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, config: Optional[NOpticConfig] = None):
        if self._initialized:
            return

        self.config = config or NOpticConfig()

        # Components
        self._ocr_engine = OCREngine(self.config.ocr_language)
        self._watchers: Dict[int, WindowWatcher] = {}

        # Event handling
        self._event_queue: asyncio.Queue = asyncio.Queue(
            maxsize=self.config.event_queue_size
        )
        self._event_listeners: List[Callable[[VisionEvent], None]] = []
        self._event_processor_task: Optional[asyncio.Task] = None

        # Yabai integration
        self._yabai_intelligence = None

        # State
        self._is_running = False
        self._shutdown_event = asyncio.Event()

        # Statistics
        self._stats = {
            "total_events": 0,
            "active_watchers": 0,
            "total_captures": 0,
            "start_time": None,
        }

        self._initialized = True
        logger.info("[N-OPTIC] N-Optic Nerve initialized")

    @classmethod
    def get_instance(cls, config: Optional[NOpticConfig] = None) -> "NOpticNerve":
        """Get singleton instance."""
        return cls(config)

    async def start(self) -> bool:
        """Start the N-Optic Nerve system."""
        if self._is_running:
            return True

        logger.info("[N-OPTIC] Starting N-Optic Nerve...")

        # Initialize OCR
        await self._ocr_engine.initialize()

        # Initialize Yabai integration
        await self._init_yabai_integration()

        # Start event processor
        self._shutdown_event.clear()
        self._event_processor_task = asyncio.create_task(
            self._process_events(),
            name="NOpticEventProcessor"
        )

        self._is_running = True
        self._stats["start_time"] = datetime.now()

        logger.info("[N-OPTIC] N-Optic Nerve started")
        return True

    async def stop(self) -> None:
        """Stop the N-Optic Nerve system."""
        logger.info("[N-OPTIC] Stopping N-Optic Nerve...")

        self._shutdown_event.set()

        # Stop all watchers
        for watcher in list(self._watchers.values()):
            await watcher.stop()
        self._watchers.clear()

        # Stop event processor
        if self._event_processor_task and not self._event_processor_task.done():
            self._event_processor_task.cancel()
            try:
                await asyncio.wait_for(self._event_processor_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        self._is_running = False
        logger.info("[N-OPTIC] N-Optic Nerve stopped")

    async def _init_yabai_integration(self) -> None:
        """Initialize Yabai spatial intelligence integration."""
        try:
            from intelligence.yabai_spatial_intelligence import get_yabai_intelligence
            self._yabai_intelligence = await get_yabai_intelligence()

            # Register for space change events
            from intelligence.yabai_spatial_intelligence import YabaiEventType
            self._yabai_intelligence.register_event_listener(
                YabaiEventType.WINDOW_CREATED,
                self._on_window_created
            )
            self._yabai_intelligence.register_event_listener(
                YabaiEventType.WINDOW_DESTROYED,
                self._on_window_destroyed
            )

            logger.info("[N-OPTIC] Yabai integration initialized")
        except Exception as e:
            logger.warning(f"[N-OPTIC] Yabai integration not available: {e}")

    async def _on_window_created(self, event) -> None:
        """Handle window creation event from Yabai."""
        logger.debug(f"[N-OPTIC] Window created: {event.window_id} ({event.app_name})")

    async def _on_window_destroyed(self, event) -> None:
        """Handle window destruction event from Yabai."""
        if event.window_id in self._watchers:
            await self._watchers[event.window_id].stop()
            del self._watchers[event.window_id]
            logger.info(f"[N-OPTIC] Removed watcher for destroyed window {event.window_id}")

    async def watch_window(
        self,
        window_id: int,
        triggers: List[WatchTrigger],
        callback: Optional[Callable[[VisionEvent], None]] = None,
    ) -> bool:
        """
        Start watching a specific window for triggers.

        Args:
            window_id: The window ID to watch
            triggers: List of triggers to monitor for
            callback: Optional callback for when triggers fire

        Returns:
            True if watcher was started successfully
        """
        if window_id in self._watchers:
            logger.warning(f"[N-OPTIC] Already watching window {window_id}")
            return False

        if len(self._watchers) >= self.config.max_concurrent_watchers:
            logger.error("[N-OPTIC] Max concurrent watchers reached")
            return False

        # Get window info from Yabai
        window_info = await self._get_window_info(window_id)
        if window_info is None:
            logger.error(f"[N-OPTIC] Window {window_id} not found")
            return False

        # Create watch target
        target = WatchTarget(
            window_id=window_id,
            app_name=window_info.get("app", "Unknown"),
            window_title=window_info.get("title", ""),
            space_id=window_info.get("space", 1),
            triggers=triggers,
            callback=callback,
        )

        # Create and start watcher
        watcher = WindowWatcher(
            target=target,
            config=self.config,
            ocr_engine=self._ocr_engine,
            event_callback=self._on_event,
        )

        if await watcher.start():
            self._watchers[window_id] = watcher
            self._stats["active_watchers"] = len(self._watchers)
            return True

        return False

    async def watch_app(
        self,
        app_name: str,
        triggers: List[WatchTrigger],
        callback: Optional[Callable[[VisionEvent], None]] = None,
        all_windows: bool = True,
    ) -> int:
        """
        Start watching all windows of a specific application.

        Args:
            app_name: Application name to watch
            triggers: List of triggers to monitor for
            callback: Optional callback for when triggers fire
            all_windows: If True, watch all windows of the app

        Returns:
            Number of windows being watched
        """
        windows = await self._find_windows_by_app(app_name)

        if not windows:
            logger.warning(f"[N-OPTIC] No windows found for app: {app_name}")
            return 0

        count = 0
        for window in windows[:self.config.max_concurrent_watchers]:
            window_id = window.get("id")
            if window_id and await self.watch_window(window_id, triggers.copy(), callback):
                count += 1

            if not all_windows:
                break

        logger.info(f"[N-OPTIC] Watching {count} windows for app: {app_name}")
        return count

    async def watch_for_text(
        self,
        window_id: int,
        text_patterns: List[str],
        callback: Optional[Callable[[VisionEvent], None]] = None,
        one_shot: bool = True,
    ) -> bool:
        """
        Convenience method to watch a window for specific text.

        Args:
            window_id: Window to watch
            text_patterns: List of text patterns to detect
            callback: Callback when text is detected
            one_shot: If True, stop watching after first match
        """
        triggers = []
        for pattern in text_patterns:
            triggers.append(WatchTrigger(
                name=f"text:{pattern[:20]}",
                trigger_type=VisionEventType.TEXT_DETECTED,
                pattern=pattern,
                callback=callback,
                one_shot=one_shot,
            ))

        return await self.watch_window(window_id, triggers, callback)

    async def stop_watching(self, window_id: int) -> bool:
        """Stop watching a specific window."""
        if window_id not in self._watchers:
            return False

        await self._watchers[window_id].stop()
        del self._watchers[window_id]
        self._stats["active_watchers"] = len(self._watchers)
        return True

    async def stop_all_watching(self) -> None:
        """Stop all window watchers."""
        for watcher in list(self._watchers.values()):
            await watcher.stop()
        self._watchers.clear()
        self._stats["active_watchers"] = 0

    def on_event(self, callback: Callable[[VisionEvent], None]) -> None:
        """Register a callback for all vision events."""
        self._event_listeners.append(callback)

    def _on_event(self, event: VisionEvent) -> None:
        """Handle a vision event from a watcher."""
        try:
            self._event_queue.put_nowait(event)
            self._stats["total_events"] += 1
        except asyncio.QueueFull:
            logger.warning("[N-OPTIC] Event queue full, dropping event")

    async def _process_events(self) -> None:
        """Process events from the queue."""
        while not self._shutdown_event.is_set():
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0
                )

                # Notify all listeners
                for listener in self._event_listeners:
                    try:
                        if asyncio.iscoroutinefunction(listener):
                            await listener(event)
                        else:
                            listener(event)
                    except Exception as e:
                        logger.error(f"[N-OPTIC] Event listener error: {e}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[N-OPTIC] Event processing error: {e}")

    async def _get_window_info(self, window_id: int) -> Optional[Dict[str, Any]]:
        """Get window information from Yabai or Quartz."""
        try:
            # Try Yabai first
            if self._yabai_intelligence:
                for space in self._yabai_intelligence.current_spaces.values():
                    for window in space.windows:
                        if window.window_id == window_id:
                            return {
                                "id": window.window_id,
                                "app": window.app_name,
                                "title": window.title,
                                "space": space.space_id,
                                "frame": window.frame,
                            }

            # Fallback to Quartz
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionAll,
                kCGNullWindowID,
            )

            windows = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID)
            for window in windows:
                if window.get("kCGWindowNumber") == window_id:
                    return {
                        "id": window_id,
                        "app": window.get("kCGWindowOwnerName", "Unknown"),
                        "title": window.get("kCGWindowName", ""),
                        "space": 1,  # Unknown without Yabai
                        "frame": window.get("kCGWindowBounds", {}),
                    }

            return None

        except Exception as e:
            logger.error(f"[N-OPTIC] Error getting window info: {e}")
            return None

    async def _find_windows_by_app(self, app_name: str) -> List[Dict[str, Any]]:
        """Find all windows belonging to an application."""
        windows = []

        try:
            # Try Yabai first
            if self._yabai_intelligence:
                for space in self._yabai_intelligence.current_spaces.values():
                    for window in space.windows:
                        if app_name.lower() in window.app_name.lower():
                            windows.append({
                                "id": window.window_id,
                                "app": window.app_name,
                                "title": window.title,
                                "space": space.space_id,
                            })
                return windows

            # Fallback to Quartz
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )

            all_windows = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID
            )

            for window in all_windows:
                owner = window.get("kCGWindowOwnerName", "")
                if app_name.lower() in owner.lower():
                    windows.append({
                        "id": window.get("kCGWindowNumber"),
                        "app": owner,
                        "title": window.get("kCGWindowName", ""),
                        "space": 1,
                    })

        except Exception as e:
            logger.error(f"[N-OPTIC] Error finding windows: {e}")

        return windows

    async def get_all_windows(self) -> List[Dict[str, Any]]:
        """Get all visible windows across all Spaces."""
        windows = []

        try:
            if self._yabai_intelligence and self._yabai_intelligence.current_spaces:
                for space in self._yabai_intelligence.current_spaces.values():
                    for window in space.windows:
                        windows.append({
                            "id": window.window_id,
                            "app": window.app_name,
                            "title": window.title,
                            "space": space.space_id,
                            "focused": window.is_focused,
                        })
            else:
                # Fallback to Quartz
                from Quartz import (
                    CGWindowListCopyWindowInfo,
                    kCGWindowListOptionOnScreenOnly,
                    kCGNullWindowID,
                )

                all_windows = CGWindowListCopyWindowInfo(
                    kCGWindowListOptionOnScreenOnly,
                    kCGNullWindowID
                )

                for window in all_windows:
                    if window.get("kCGWindowLayer") == 0:  # Normal windows only
                        windows.append({
                            "id": window.get("kCGWindowNumber"),
                            "app": window.get("kCGWindowOwnerName", "Unknown"),
                            "title": window.get("kCGWindowName", ""),
                            "space": 1,
                            "focused": False,
                        })

        except Exception as e:
            logger.error(f"[N-OPTIC] Error getting all windows: {e}")

        return windows

    def get_stats(self) -> Dict[str, Any]:
        """Get N-Optic Nerve statistics."""
        watcher_stats = {}
        for window_id, watcher in self._watchers.items():
            watcher_stats[window_id] = watcher.stats

        return {
            **self._stats,
            "is_running": self._is_running,
            "queue_size": self._event_queue.qsize(),
            "listeners": len(self._event_listeners),
            "watchers": watcher_stats,
        }


# =============================================================================
# Convenience Functions
# =============================================================================

async def get_n_optic_nerve(config: Optional[NOpticConfig] = None) -> NOpticNerve:
    """Get the N-Optic Nerve singleton instance."""
    nerve = NOpticNerve.get_instance(config)
    if not nerve._is_running:
        await nerve.start()
    return nerve


def create_success_trigger(
    callback: Optional[Callable[[VisionEvent], None]] = None,
    one_shot: bool = True,
) -> WatchTrigger:
    """Create a trigger for success messages."""
    return WatchTrigger(
        name="success_detector",
        trigger_type=VisionEventType.SUCCESS_DETECTED,
        keywords=["success", "completed", "done", "finished", "passed", "deployed"],
        callback=callback,
        one_shot=one_shot,
    )


def create_error_trigger(
    callback: Optional[Callable[[VisionEvent], None]] = None,
    one_shot: bool = False,
) -> WatchTrigger:
    """Create a trigger for error messages."""
    return WatchTrigger(
        name="error_detector",
        trigger_type=VisionEventType.ERROR_DETECTED,
        keywords=["error", "failed", "exception", "critical", "fatal", "crash"],
        callback=callback,
        one_shot=one_shot,
    )


def create_build_complete_trigger(
    callback: Optional[Callable[[VisionEvent], None]] = None,
) -> WatchTrigger:
    """Create a trigger for build completion."""
    return WatchTrigger(
        name="build_complete",
        trigger_type=VisionEventType.SUCCESS_DETECTED,
        keywords=["build succeeded", "build complete", "compiled successfully", "build passed"],
        callback=callback,
        one_shot=True,
    )


# =============================================================================
# Testing
# =============================================================================

async def test_n_optic_nerve():
    """Test the N-Optic Nerve system."""
    print("=" * 60)
    print("Testing N-Optic Nerve")
    print("=" * 60)

    # Get instance
    nerve = await get_n_optic_nerve()

    # List all windows
    print("\n1. Listing all windows...")
    windows = await nerve.get_all_windows()
    for w in windows[:10]:
        print(f"   [{w['id']}] {w['app']}: {w.get('title', '')[:50]}")

    # Find Chrome windows
    print("\n2. Finding Chrome windows...")
    chrome_windows = await nerve._find_windows_by_app("Chrome")
    print(f"   Found {len(chrome_windows)} Chrome windows")

    # Show stats
    print("\n3. Statistics:")
    stats = nerve.get_stats()
    for key, value in stats.items():
        if key != "watchers":
            print(f"   {key}: {value}")

    # Stop
    await nerve.stop()
    print("\nTest complete!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_n_optic_nerve())
