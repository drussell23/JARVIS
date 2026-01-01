"""
JARVIS Startup Progress Broadcaster v2.0.0
==========================================

Real-time WebSocket broadcaster for startup progress.
NOW INTEGRATED with UnifiedStartupProgressHub for synchronized state.

This broadcaster NO LONGER maintains its own state - it delegates
to the UnifiedStartupProgressHub as the single source of truth.

Features:
- WebSocket broadcast to all connected clients
- HTTP polling endpoint for fallback
- Dynamic component registration (no hardcoding)
- Synchronized with loading_server and startup_progress_api
- Live log streaming
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

# Python 3.9 compatible lock - lazily initializes asyncio.Lock
try:
    from backend.utils.python39_compat import AsyncLock
except ImportError:
    # Fallback: Define inline if import fails
    class AsyncLock:
        """Python 3.9-safe lock that lazily creates asyncio.Lock."""
        def __init__(self):
            self._thread_lock = threading.RLock()
            self._async_lock: Optional[asyncio.Lock] = None

        def _get_async_lock(self) -> asyncio.Lock:
            if self._async_lock is None:
                try:
                    self._async_lock = asyncio.Lock()
                except RuntimeError:
                    pass
            return self._async_lock

        async def __aenter__(self):
            async_lock = self._get_async_lock()
            if async_lock:
                await async_lock.acquire()
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            async_lock = self._get_async_lock()
            if async_lock and async_lock.locked():
                async_lock.release()
            return False

logger = logging.getLogger(__name__)

# Import the unified hub
try:
    from backend.core.unified_startup_progress import (
        get_progress_hub,
        get_progress_hub_async,
        UnifiedStartupProgressHub,
        StartupPhase
    )
    HUB_AVAILABLE = True
except ImportError:
    HUB_AVAILABLE = False
    logger.warning("[StartupBroadcaster] UnifiedStartupProgressHub not available, running standalone")


@dataclass
class StartupEvent:
    """A single startup event for the progress stream"""
    timestamp: float
    event_type: str
    component: Optional[str] = None
    message: str = ""
    progress: float = 0.0
    phase: str = "initializing"
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "component": self.component,
            "message": self.message,
            "progress": self.progress,
            "phase": self.phase,
            "metadata": self.metadata or {}
        }


class StartupProgressBroadcaster:
    """
    Broadcasts startup progress to WebSocket clients in real-time.

    v2.0 - Now delegates state to UnifiedStartupProgressHub.
    This ensures all progress tracking systems show the same data.

    Usage:
        broadcaster = get_startup_broadcaster()
        await broadcaster.broadcast_component_start("cloud_sql_proxy", "Connecting to Cloud SQL...")
        await broadcaster.broadcast_component_complete("cloud_sql_proxy", "Connected!")
    """

    _instance: Optional["StartupProgressBroadcaster"] = None

    def __init__(self):
        self.websocket_clients: Set[Any] = set()
        self._events: List[StartupEvent] = []
        self._max_events = 500
        self._lock = AsyncLock()  # Python 3.9 compatible
        self._started_at: float = time.time()

        # Get the unified hub (if available)
        self._hub: Optional[UnifiedStartupProgressHub] = None
        if HUB_AVAILABLE:
            self._hub = get_progress_hub()
            # Register this broadcaster as a sync target
            self._hub.register_sync_target(self._on_hub_update)

        # Fallback state (used if hub not available)
        self._fallback_progress = 0.0
        self._fallback_phase = "initializing"
        self._fallback_message = "Starting JARVIS..."
        self._fallback_components: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def get_instance(cls) -> "StartupProgressBroadcaster":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset the singleton - useful for testing"""
        if cls._instance and HUB_AVAILABLE:
            hub = get_progress_hub()
            hub.unregister_sync_target(cls._instance._on_hub_update)
        cls._instance = None

    def _on_hub_update(self, state: Dict[str, Any]):
        """Callback when the unified hub updates state"""
        # This is called automatically when hub state changes
        # We use this to broadcast to our WebSocket clients
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._broadcast_state(state))
        except RuntimeError:
            # No running loop - skip broadcasting
            pass

    async def _broadcast_state(self, state: Dict[str, Any]):
        """Broadcast state to WebSocket clients"""
        message = json.dumps(state)
        dead_clients = set()

        for ws in self.websocket_clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead_clients.add(ws)

        self.websocket_clients -= dead_clients

    # =========================================================================
    # WebSocket Client Management
    # =========================================================================

    def register_websocket(self, websocket):
        """Register a WebSocket client for progress updates"""
        self.websocket_clients.add(websocket)
        if self._hub:
            self._hub.register_websocket(websocket)
        logger.debug(f"[StartupBroadcaster] WebSocket client registered ({len(self.websocket_clients)} total)")

    def unregister_websocket(self, websocket):
        """Unregister a WebSocket client"""
        self.websocket_clients.discard(websocket)
        if self._hub:
            self._hub.unregister_websocket(websocket)
        logger.debug(f"[StartupBroadcaster] WebSocket client unregistered ({len(self.websocket_clients)} total)")

    # =========================================================================
    # Component Broadcasting (Delegates to Hub)
    # =========================================================================

    async def broadcast_component_start(
        self,
        component: str,
        message: str,
        substep: int = 0,
        total_substeps: int = 1
    ):
        """Broadcast that a component is starting"""
        if self._hub:
            await self._hub.component_start(component, message)
        else:
            await self._broadcast_event(
                "component_start", component, message,
                metadata={"substep": substep, "total_substeps": total_substeps}
            )

    async def broadcast_component_progress(
        self,
        component: str,
        message: str,
        substep: int,
        total_substeps: int = 1
    ):
        """Broadcast progress within a component"""
        if self._hub:
            await self._hub.component_progress(component, message, substep, total_substeps)
        else:
            await self._broadcast_event(
                "component_progress", component, message,
                metadata={"substep": substep, "total_substeps": total_substeps}
            )

    async def broadcast_component_complete(
        self,
        component: str,
        message: str = "",
        duration_ms: Optional[float] = None
    ):
        """Broadcast that a component completed successfully"""
        if self._hub:
            await self._hub.component_complete(component, message)
        else:
            await self._broadcast_event(
                "component_complete", component, message or f"{component} ready",
                metadata={"duration_ms": duration_ms}
            )

    async def broadcast_component_failed(
        self,
        component: str,
        error: str,
        is_critical: bool = False
    ):
        """Broadcast that a component failed"""
        if self._hub:
            await self._hub.component_failed(component, error, is_critical)
        else:
            await self._broadcast_event(
                "component_fail", component, f"Failed: {error}",
                metadata={"error": error, "is_critical": is_critical}
            )

    # =========================================================================
    # Other Broadcasting Methods
    # =========================================================================

    async def broadcast_log(self, message: str, level: str = "info"):
        """Broadcast a log message"""
        await self._broadcast_event("log", None, message, metadata={"level": level})

    async def broadcast_memory_info(
        self,
        available_gb: float,
        used_gb: float,
        total_gb: float,
        pressure_percent: float,
        startup_mode: str
    ):
        """Broadcast memory status update"""
        if self._hub:
            await self._hub.update_memory_info(
                available_gb, used_gb, total_gb, pressure_percent, startup_mode
            )
        else:
            await self._broadcast_event(
                "memory_update", None,
                f"Memory: {available_gb:.1f}GB available, Mode: {startup_mode}",
                metadata={
                    "memory_available_gb": available_gb,
                    "memory_used_gb": used_gb,
                    "memory_total_gb": total_gb,
                    "memory_pressure": pressure_percent,
                    "startup_mode": startup_mode
                }
            )

    async def broadcast_phase_change(self, phase: str, message: str):
        """Broadcast a phase change"""
        if self._hub:
            phase_enum = StartupPhase(phase) if isinstance(phase, str) else phase
            await self._hub.set_phase(phase_enum, message)
        else:
            self._fallback_phase = phase
            await self._broadcast_event("phase_change", None, message, metadata={"phase": phase})

    async def broadcast_complete(self, success: bool = True, message: str = ""):
        """Broadcast startup complete"""
        if self._hub:
            await self._hub.mark_complete(success, message)
        else:
            self._fallback_progress = 100.0 if success else self._fallback_progress
            self._fallback_phase = "complete" if success else "failed"
            await self._broadcast_event(
                "complete", None,
                message or ("JARVIS Online" if success else "Startup failed"),
                metadata={"success": success, "redirect_url": "http://localhost:3000"}
            )

    # =========================================================================
    # Internal Broadcasting (Fallback)
    # =========================================================================

    async def _broadcast_event(
        self,
        event_type: str,
        component: Optional[str],
        message: str,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Internal method to broadcast an event"""
        progress = self._hub.get_progress() if self._hub else self._fallback_progress
        phase = self._hub.get_phase().value if self._hub else self._fallback_phase

        event = StartupEvent(
            timestamp=time.time(),
            event_type=event_type,
            component=component,
            message=message,
            progress=progress,
            phase=phase,
            metadata=metadata
        )

        async with self._lock:
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events:]

        # Send to all clients
        msg = json.dumps(event.to_dict())
        dead_clients = set()
        for ws in self.websocket_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead_clients.add(ws)
        self.websocket_clients -= dead_clients

    # =========================================================================
    # State Access
    # =========================================================================

    def get_current_state(self) -> Dict[str, Any]:
        """Get current startup state for HTTP polling"""
        if self._hub:
            return self._hub.get_state()

        # Fallback state
        return {
            "progress": self._fallback_progress,
            "phase": self._fallback_phase,
            "message": self._fallback_message,
            "current_component": None,
            "components": self._fallback_components,
            "memory": None,
            "startup_mode": None,
            "elapsed_seconds": time.time() - self._started_at,
            "events_count": len(self._events)
        }

    def get_recent_events(self, count: int = 50) -> List[Dict[str, Any]]:
        """Get recent events for clients that just connected"""
        if self._hub:
            return self._hub.get_recent_events(count)
        return [e.to_dict() for e in self._events[-count:]]

    def is_ready(self) -> bool:
        """Check if the system is truly ready"""
        if self._hub:
            return self._hub.is_ready()
        return self._fallback_phase == "complete"

    def get_progress_summary(self) -> str:
        """Get a human-readable progress summary"""
        if self._hub:
            return self._hub.get_summary()
        return f"Progress: {self._fallback_progress:.0f}% - {self._fallback_phase}"


# Global singleton getter
def get_startup_broadcaster() -> StartupProgressBroadcaster:
    """Get the global startup progress broadcaster"""
    return StartupProgressBroadcaster.get_instance()
