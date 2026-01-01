#!/usr/bin/env python3
"""
Startup Progress WebSocket API v2.0
===================================

Real-time broadcast of JARVIS startup/restart progress to loading page.
NOW INTEGRATED with UnifiedStartupProgressHub for synchronized state.

This API no longer maintains its own progress state - it delegates
to the UnifiedStartupProgressHub as the single source of truth.
"""

import asyncio
import logging
import threading
from typing import Dict, List, Optional, Set
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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
        UnifiedStartupProgressHub
    )
    HUB_AVAILABLE = True
except ImportError:
    HUB_AVAILABLE = False
    logger.warning("[StartupProgressAPI] UnifiedStartupProgressHub not available, running standalone")


router = APIRouter()


class StartupProgressManager:
    """
    Manages WebSocket connections for startup progress updates.

    v2.0 - Now delegates state to UnifiedStartupProgressHub.
    This ensures all progress tracking systems show the same data.
    """

    def __init__(self):
        self.connections: Set[WebSocket] = set()
        self._lock = AsyncLock()  # Python 3.9 compatible

        # Get the unified hub (if available)
        self._hub: UnifiedStartupProgressHub = None
        if HUB_AVAILABLE:
            self._hub = get_progress_hub()
            # Register for state updates
            self._hub.register_sync_target(self._on_hub_update)

        # Fallback state (used if hub not available)
        self._fallback_status: Dict = {
            "stage": "idle",
            "message": "System idle",
            "progress": 0,
            "timestamp": datetime.now().isoformat(),
        }

    @property
    def current_status(self) -> Dict:
        """Get current status from hub or fallback"""
        if self._hub:
            state = self._hub.get_state()
            # Add timestamp for API compatibility
            state["timestamp"] = datetime.now().isoformat()
            return state
        return self._fallback_status

    def _on_hub_update(self, state: Dict):
        """Callback when hub state changes - broadcast to clients"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._broadcast_to_clients(state))
        except RuntimeError:
            # No running loop - skip broadcasting
            pass

    async def _broadcast_to_clients(self, status: Dict):
        """Broadcast status to all connected WebSocket clients"""
        status["timestamp"] = datetime.now().isoformat()
        disconnected = []

        async with self._lock:
            for websocket in self.connections:
                try:
                    await websocket.send_json(status)
                except Exception as e:
                    logger.warning(f"Failed to send to client: {e}")
                    disconnected.append(websocket)

        if disconnected:
            async with self._lock:
                for ws in disconnected:
                    self.connections.discard(ws)

    async def connect(self, websocket: WebSocket):
        """Add a new WebSocket connection"""
        await websocket.accept()
        async with self._lock:
            self.connections.add(websocket)

        # Register with hub if available
        if self._hub:
            self._hub.register_websocket(websocket)

        logger.info(f"Startup progress client connected. Total: {len(self.connections)}")

        # Send current status immediately
        try:
            await websocket.send_json(self.current_status)
        except Exception as e:
            logger.error(f"Failed to send initial status: {e}")

    async def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection"""
        async with self._lock:
            self.connections.discard(websocket)

        # Unregister from hub if available
        if self._hub:
            self._hub.unregister_websocket(websocket)

        logger.info(f"Startup progress client disconnected. Remaining: {len(self.connections)}")

    async def broadcast_progress(
        self,
        stage: str,
        message: str,
        progress: int,
        details: Dict = None,
        metadata: Dict = None,
    ):
        """
        Broadcast progress update to all connected clients.

        If hub is available, this also updates the hub which will
        sync to all other systems (loading_server, broadcaster, etc.)

        Args:
            stage: Current stage (e.g., "detecting", "killing", "starting")
            message: Human-readable message
            progress: Progress percentage (0-100)
            details: Optional additional data
            metadata: Optional stage metadata (icon, label, sublabel for dynamic UI)
        """
        # If hub available, use component-based tracking
        if self._hub:
            # Map stage to component operations
            if stage == "complete":
                await self._hub.mark_complete(True, message)
            elif stage == "failed":
                await self._hub.mark_complete(False, message)
            else:
                # For other stages, update as a component
                component_name = stage.replace("-", "_").replace(" ", "_")
                if progress < 100:
                    await self._hub.component_start(component_name, message)
                else:
                    await self._hub.component_complete(component_name, message)
            return

        # Fallback: Update local state and broadcast directly
        status = {
            "stage": stage,
            "message": message,
            "progress": progress,
            "timestamp": datetime.now().isoformat(),
        }

        if details:
            status["details"] = details

        if metadata:
            status["metadata"] = metadata

        self._fallback_status = status

        # Broadcast to all connected clients
        await self._broadcast_to_clients(status)

    async def broadcast_complete(self, success: bool = True, redirect_url: str = None):
        """Broadcast completion status"""
        if self._hub:
            await self._hub.mark_complete(success, "System ready!" if success else "Startup failed")
            return

        # Fallback
        status = {
            "stage": "complete" if success else "failed",
            "message": "System ready!" if success else "Startup failed",
            "progress": 100 if success else 0,
            "timestamp": datetime.now().isoformat(),
            "success": success,
        }

        if redirect_url:
            status["redirect_url"] = redirect_url

        self._fallback_status = status

        # Broadcast to all clients
        await self._broadcast_to_clients(status)

    def is_ready(self) -> bool:
        """Check if the system is truly ready"""
        if self._hub:
            return self._hub.is_ready()
        return self._fallback_status.get("stage") == "complete"


# Global instance
startup_progress_manager = StartupProgressManager()


@router.websocket("/ws/startup-progress")
async def startup_progress_websocket(websocket: WebSocket):
    """WebSocket endpoint for real-time startup progress updates"""
    await startup_progress_manager.connect(websocket)

    try:
        # Keep connection alive and listen for pings
        while True:
            try:
                data = await websocket.receive_text()
                # Client can send "ping" to keep connection alive
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
            except WebSocketDisconnect:
                break
    except Exception as e:
        logger.error(f"Startup progress WebSocket error: {e}")
    finally:
        await startup_progress_manager.disconnect(websocket)


# HTTP endpoint for polling fallback
@router.get("/api/startup-progress")
async def get_startup_progress():
    """HTTP endpoint for polling-based progress updates (fallback for WebSocket)"""
    return startup_progress_manager.current_status


@router.get("/api/startup-progress/ready")
async def get_ready_status():
    """
    Quick endpoint to check if system is truly ready.
    Use this before announcing "ready" via voice or UI.
    """
    is_ready = startup_progress_manager.is_ready()
    status = startup_progress_manager.current_status
    return {
        "is_ready": is_ready,
        "progress": status.get("progress", 0),
        "phase": status.get("phase", "unknown"),
        "message": status.get("message", "")
    }


# Convenience function for use in start_system.py
def get_startup_progress_manager() -> StartupProgressManager:
    """Get the global startup progress manager instance"""
    return startup_progress_manager
