"""
Surveillance Progress Stream - Real-time UI feedback for God Mode operations

v32.0: Provides real-time progress streaming from VisualMonitorAgent to WebSocket/UI.

The Problem:
    When user says "watch all Chrome windows for bouncing ball", they see:
    - "Got it, Sir. I'll scan every Chrome window..."
    - Then SILENCE for 30-60 seconds while JARVIS works
    - User doesn't know: Is it working? Did it crash? What's happening?

The Solution:
    This module provides a global event stream that:
    1. VisualMonitorAgent emits progress events at each stage
    2. WebSocket subscribes and forwards to frontend in real-time
    3. User sees: "Finding windows... Found 6 Chrome windows... Teleporting to Ghost Display..."

Architecture:
    VisualMonitorAgent
        ↓ emit_progress()
    SurveillanceProgressStream (global singleton)
        ↓ async generator
    WebSocket handler
        ↓ send_json()
    Frontend UI

Usage:
    # In VisualMonitorAgent:
    from backend.core.surveillance_progress_stream import emit_surveillance_progress
    await emit_surveillance_progress("discovery", "Found 6 Chrome windows across 4 spaces")
    
    # In WebSocket:
    from backend.core.surveillance_progress_stream import get_progress_stream
    async for event in get_progress_stream():
        await websocket.send_json(event)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Set
from collections import deque

logger = logging.getLogger(__name__)


class SurveillanceStage(Enum):
    """Stages of the surveillance setup process."""
    STARTING = "starting"           # Initial acknowledgment
    PERMISSION_CHECK = "permission_check"  # Checking yabai permissions
    DISCOVERY = "discovery"         # Finding windows across spaces
    TELEPORT_START = "teleport_start"  # Starting window teleportation
    TELEPORT_PROGRESS = "teleport_progress"  # Each window being moved
    TELEPORT_COMPLETE = "teleport_complete"  # All windows moved
    WATCHER_START = "watcher_start"  # Starting to spawn watchers
    WATCHER_PROGRESS = "watcher_progress"  # Each watcher being created
    WATCHER_READY = "watcher_ready"  # Watcher successfully started
    WATCHER_FAILED = "watcher_failed"  # Watcher failed to start
    VALIDATION = "validation"       # Validating capture streams
    MONITORING_ACTIVE = "monitoring_active"  # Surveillance running
    DETECTION = "detection"         # Target detected!
    ERROR = "error"                 # Error occurred
    COMPLETE = "complete"           # Surveillance ended


@dataclass
class SurveillanceProgressEvent:
    """A progress event during surveillance setup."""
    stage: SurveillanceStage
    message: str
    timestamp: float = field(default_factory=time.time)
    
    # Progress tracking
    progress_current: int = 0       # e.g., 3 of 6 windows
    progress_total: int = 0         # e.g., 6 total windows
    progress_percent: float = 0.0   # 0.0 to 1.0
    
    # Context
    app_name: str = ""
    trigger_text: str = ""
    window_id: Optional[int] = None
    space_id: Optional[int] = None
    watcher_id: Optional[str] = None
    
    # Metadata
    details: Dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "type": "surveillance_progress",
            "stage": self.stage.value,
            "message": self.message,
            "timestamp": self.timestamp,
            "progress": {
                "current": self.progress_current,
                "total": self.progress_total,
                "percent": round(self.progress_percent * 100, 1),
            },
            "context": {
                "app_name": self.app_name,
                "trigger_text": self.trigger_text,
                "window_id": self.window_id,
                "space_id": self.space_id,
                "watcher_id": self.watcher_id,
            },
            "details": self.details,
            "correlation_id": self.correlation_id,
        }


class SurveillanceProgressStream:
    """
    Global singleton for streaming surveillance progress events.
    
    Uses asyncio.Queue for efficient async event delivery.
    Supports multiple subscribers (multiple WebSocket connections).
    """
    
    _instance: Optional["SurveillanceProgressStream"] = None
    
    def __init__(self):
        # Event queues for each subscriber
        self._subscribers: Dict[str, asyncio.Queue] = {}
        self._subscriber_count = 0
        
        # Current surveillance state
        self._active_surveillance: Optional[str] = None  # correlation_id
        self._current_stage: SurveillanceStage = SurveillanceStage.COMPLETE
        
        # Event history for late subscribers
        self._event_history: deque = deque(maxlen=50)
        
        # Lock for thread-safe operations
        self._lock = asyncio.Lock()
        
        logger.info("[SurveillanceProgressStream] Initialized")
    
    @classmethod
    def get_instance(cls) -> "SurveillanceProgressStream":
        """Get the global singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    async def subscribe(self, subscriber_id: Optional[str] = None) -> str:
        """
        Subscribe to progress events.
        
        Args:
            subscriber_id: Optional custom ID (auto-generated if not provided)
            
        Returns:
            Subscriber ID for use with get_events() and unsubscribe()
        """
        async with self._lock:
            if subscriber_id is None:
                self._subscriber_count += 1
                subscriber_id = f"sub_{self._subscriber_count}_{int(time.time())}"
            
            self._subscribers[subscriber_id] = asyncio.Queue(maxsize=100)
            
            logger.debug(f"[SurveillanceProgressStream] New subscriber: {subscriber_id}")
            
            return subscriber_id
    
    async def unsubscribe(self, subscriber_id: str) -> None:
        """Unsubscribe from progress events."""
        async with self._lock:
            if subscriber_id in self._subscribers:
                del self._subscribers[subscriber_id]
                logger.debug(f"[SurveillanceProgressStream] Unsubscribed: {subscriber_id}")
    
    async def emit(self, event: SurveillanceProgressEvent) -> None:
        """
        Emit a progress event to all subscribers.
        
        Args:
            event: The progress event to emit
        """
        # Update current state
        self._current_stage = event.stage
        if event.correlation_id:
            self._active_surveillance = event.correlation_id
        
        # Add to history
        self._event_history.append(event)
        
        # Log significant events
        if event.stage in (SurveillanceStage.DISCOVERY, SurveillanceStage.MONITORING_ACTIVE, 
                          SurveillanceStage.DETECTION, SurveillanceStage.ERROR):
            logger.info(f"[SurveillanceProgress] {event.stage.value}: {event.message}")
        else:
            logger.debug(f"[SurveillanceProgress] {event.stage.value}: {event.message}")
        
        # Deliver to all subscribers (non-blocking)
        event_dict = event.to_dict()
        
        for sub_id, queue in list(self._subscribers.items()):
            try:
                # Non-blocking put - drop if queue full
                queue.put_nowait(event_dict)
            except asyncio.QueueFull:
                logger.warning(f"[SurveillanceProgressStream] Queue full for {sub_id}, dropping event")
            except Exception as e:
                logger.error(f"[SurveillanceProgressStream] Error delivering to {sub_id}: {e}")
    
    async def get_events(self, subscriber_id: str, timeout: float = 0.5) -> AsyncIterator[Dict[str, Any]]:
        """
        Async generator that yields progress events for a subscriber.
        
        Args:
            subscriber_id: The subscriber ID from subscribe()
            timeout: Timeout between events (yields None on timeout for heartbeat)
            
        Yields:
            Progress event dicts
        """
        if subscriber_id not in self._subscribers:
            logger.warning(f"[SurveillanceProgressStream] Unknown subscriber: {subscriber_id}")
            return
        
        queue = self._subscribers[subscriber_id]
        
        while subscriber_id in self._subscribers:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
                yield event
            except asyncio.TimeoutError:
                # Yield heartbeat on timeout
                yield {
                    "type": "surveillance_heartbeat",
                    "timestamp": time.time(),
                    "stage": self._current_stage.value,
                    "active": self._active_surveillance is not None,
                }
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SurveillanceProgressStream] Error in get_events: {e}")
                break
    
    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent event history."""
        events = list(self._event_history)[-limit:]
        return [e.to_dict() for e in events]
    
    def get_current_state(self) -> Dict[str, Any]:
        """Get current surveillance state."""
        return {
            "stage": self._current_stage.value,
            "active_surveillance": self._active_surveillance,
            "subscriber_count": len(self._subscribers),
            "history_size": len(self._event_history),
        }
    
    async def clear(self) -> None:
        """Clear surveillance state (called when surveillance ends)."""
        self._active_surveillance = None
        self._current_stage = SurveillanceStage.COMPLETE


# =============================================================================
# Global Access Functions
# =============================================================================

def get_progress_stream() -> SurveillanceProgressStream:
    """Get the global progress stream instance."""
    return SurveillanceProgressStream.get_instance()


async def emit_surveillance_progress(
    stage: SurveillanceStage,
    message: str,
    progress_current: int = 0,
    progress_total: int = 0,
    app_name: str = "",
    trigger_text: str = "",
    window_id: Optional[int] = None,
    space_id: Optional[int] = None,
    watcher_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    correlation_id: str = "",
) -> None:
    """
    Convenience function to emit a surveillance progress event.
    
    Example:
        await emit_surveillance_progress(
            stage=SurveillanceStage.DISCOVERY,
            message="Found 6 Chrome windows across 4 spaces",
            progress_current=6,
            progress_total=6,
            app_name="Chrome",
            trigger_text="bouncing ball"
        )
    """
    progress_percent = progress_current / progress_total if progress_total > 0 else 0.0
    
    event = SurveillanceProgressEvent(
        stage=stage,
        message=message,
        progress_current=progress_current,
        progress_total=progress_total,
        progress_percent=progress_percent,
        app_name=app_name,
        trigger_text=trigger_text,
        window_id=window_id,
        space_id=space_id,
        watcher_id=watcher_id,
        details=details or {},
        correlation_id=correlation_id,
    )
    
    stream = get_progress_stream()
    await stream.emit(event)


# =============================================================================
# Helper Functions for Common Events
# =============================================================================

async def emit_discovery_start(app_name: str, trigger_text: str, correlation_id: str = "") -> None:
    """Emit event when starting to discover windows."""
    await emit_surveillance_progress(
        stage=SurveillanceStage.DISCOVERY,
        message=f"🔍 Searching for {app_name} windows across all spaces...",
        app_name=app_name,
        trigger_text=trigger_text,
        correlation_id=correlation_id,
    )


async def emit_discovery_complete(
    app_name: str, 
    window_count: int, 
    space_count: int,
    trigger_text: str = "",
    correlation_id: str = ""
) -> None:
    """Emit event when window discovery completes."""
    await emit_surveillance_progress(
        stage=SurveillanceStage.DISCOVERY,
        message=f"✅ Found {window_count} {app_name} windows across {space_count} spaces",
        progress_current=window_count,
        progress_total=window_count,
        app_name=app_name,
        trigger_text=trigger_text,
        details={"window_count": window_count, "space_count": space_count},
        correlation_id=correlation_id,
    )


async def emit_teleport_progress(
    current: int, 
    total: int, 
    window_id: int,
    from_space: int,
    to_space: int,
    app_name: str = "",
    correlation_id: str = ""
) -> None:
    """Emit event for each window being teleported."""
    await emit_surveillance_progress(
        stage=SurveillanceStage.TELEPORT_PROGRESS,
        message=f"👻 Moving window {current}/{total} to Ghost Display (Space {from_space} → {to_space})",
        progress_current=current,
        progress_total=total,
        window_id=window_id,
        space_id=to_space,
        app_name=app_name,
        details={"from_space": from_space, "to_space": to_space},
        correlation_id=correlation_id,
    )


async def emit_watcher_spawned(
    current: int,
    total: int,
    watcher_id: str,
    window_id: int,
    space_id: int,
    app_name: str = "",
    correlation_id: str = ""
) -> None:
    """Emit event when a watcher is successfully spawned."""
    await emit_surveillance_progress(
        stage=SurveillanceStage.WATCHER_READY,
        message=f"👁️ Watcher {current}/{total} ready (Window {window_id})",
        progress_current=current,
        progress_total=total,
        window_id=window_id,
        space_id=space_id,
        watcher_id=watcher_id,
        app_name=app_name,
        correlation_id=correlation_id,
    )


async def emit_monitoring_active(
    watcher_count: int,
    app_name: str,
    trigger_text: str,
    correlation_id: str = ""
) -> None:
    """Emit event when surveillance is fully active."""
    await emit_surveillance_progress(
        stage=SurveillanceStage.MONITORING_ACTIVE,
        message=f"🎯 Monitoring {watcher_count} {app_name} windows for '{trigger_text}'",
        progress_current=watcher_count,
        progress_total=watcher_count,
        app_name=app_name,
        trigger_text=trigger_text,
        details={"watcher_count": watcher_count},
        correlation_id=correlation_id,
    )


async def emit_detection(
    trigger_text: str,
    window_id: int,
    space_id: int,
    app_name: str = "",
    confidence: float = 0.0,
    correlation_id: str = ""
) -> None:
    """Emit event when target is detected."""
    await emit_surveillance_progress(
        stage=SurveillanceStage.DETECTION,
        message=f"🎉 DETECTED '{trigger_text}' in {app_name} on Space {space_id}!",
        window_id=window_id,
        space_id=space_id,
        app_name=app_name,
        trigger_text=trigger_text,
        details={"confidence": confidence},
        correlation_id=correlation_id,
    )


async def emit_error(
    message: str,
    app_name: str = "",
    trigger_text: str = "",
    details: Optional[Dict[str, Any]] = None,
    correlation_id: str = ""
) -> None:
    """Emit error event."""
    await emit_surveillance_progress(
        stage=SurveillanceStage.ERROR,
        message=f"❌ {message}",
        app_name=app_name,
        trigger_text=trigger_text,
        details=details or {},
        correlation_id=correlation_id,
    )

