#!/usr/bin/env python3
"""
JARVIS Broadcast Router
========================

FastAPI router for supervisor → frontend communication.
Receives maintenance events from the supervisor and broadcasts
them to all connected WebSocket clients.

Features:
- /api/broadcast endpoint for supervisor to inject events
- WebSocket connection manager for real-time client updates
- Async parallel broadcast to all connections
- Graceful handling of disconnected clients

Usage:
    from backend.api.broadcast_router import broadcast_router, manager
    app.include_router(broadcast_router)

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ==============================================================================
# Pydantic Models
# ==============================================================================

class BroadcastEvent(BaseModel):
    """Event to broadcast to all WebSocket clients."""
    event: str  # Event type: system_updating, system_restarting, etc.
    data: Dict[str, Any] = {}


class BroadcastResponse(BaseModel):
    """Response from broadcast endpoint."""
    success: bool
    message: str
    clients_notified: int = 0


# ==============================================================================
# WebSocket Connection Manager
# ==============================================================================

@dataclass
class ConnectionInfo:
    """Information about a connected WebSocket client."""
    websocket: WebSocket
    connected_at: datetime = field(default_factory=datetime.now)
    client_id: str = ""
    last_ping: float = field(default_factory=time.time)


class BroadcastConnectionManager:
    """
    Manages WebSocket connections for broadcast events.
    
    Features:
    - Thread-safe connection tracking
    - Parallel broadcast to all clients
    - Automatic cleanup of dead connections
    - Connection statistics
    
    Example:
        >>> manager = BroadcastConnectionManager()
        >>> await manager.connect(websocket)
        >>> await manager.broadcast({"type": "system_updating", ...})
    """
    
    def __init__(self):
        self._connections: Dict[int, ConnectionInfo] = {}
        self._lock = asyncio.Lock()
        self._message_count = 0
        logger.info("📡 Broadcast connection manager initialized")
    
    async def connect(self, websocket: WebSocket, client_id: str = "") -> int:
        """
        Accept a new WebSocket connection.
        
        Args:
            websocket: The WebSocket to connect
            client_id: Optional client identifier
            
        Returns:
            Connection ID
        """
        await websocket.accept()
        
        async with self._lock:
            conn_id = id(websocket)
            self._connections[conn_id] = ConnectionInfo(
                websocket=websocket,
                client_id=client_id or f"client_{conn_id}",
            )
        
        logger.info(f"📱 Client connected: {conn_id} (total: {len(self._connections)})")
        return conn_id
    
    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        conn_id = id(websocket)
        
        async with self._lock:
            if conn_id in self._connections:
                del self._connections[conn_id]
                logger.info(f"📱 Client disconnected: {conn_id} (remaining: {len(self._connections)})")
    
    async def broadcast(self, message: Dict[str, Any]) -> int:
        """
        Broadcast a message to all connected clients.
        
        Args:
            message: Message to broadcast (dict)
            
        Returns:
            Number of clients successfully notified
        """
        if not self._connections:
            logger.debug("No clients connected for broadcast")
            return 0
        
        self._message_count += 1
        
        # Get snapshot of connections
        async with self._lock:
            connections = list(self._connections.items())
        
        # Broadcast in parallel
        async def send_to_client(conn_id: int, info: ConnectionInfo) -> bool:
            try:
                await info.websocket.send_json(message)
                return True
            except Exception as e:
                logger.debug(f"Failed to send to {conn_id}: {e}")
                # Mark for removal
                async with self._lock:
                    if conn_id in self._connections:
                        del self._connections[conn_id]
                return False
        
        results = await asyncio.gather(
            *(send_to_client(cid, info) for cid, info in connections),
            return_exceptions=True,
        )
        
        success_count = sum(1 for r in results if r is True)
        logger.info(f"📡 Broadcast to {success_count}/{len(connections)} clients: {message.get('type', 'unknown')}")
        
        return success_count
    
    async def send_personal(self, websocket: WebSocket, message: Dict[str, Any]) -> bool:
        """Send a message to a specific client."""
        try:
            await websocket.send_json(message)
            return True
        except Exception as e:
            logger.debug(f"Failed to send personal message: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get connection statistics."""
        return {
            "active_connections": len(self._connections),
            "total_messages": self._message_count,
            "connections": [
                {
                    "id": cid,
                    "client_id": info.client_id,
                    "connected_at": info.connected_at.isoformat(),
                    "age_seconds": (datetime.now() - info.connected_at).total_seconds(),
                }
                for cid, info in self._connections.items()
            ],
        }


# ==============================================================================
# Global Manager Instance
# ==============================================================================

manager = BroadcastConnectionManager()


# ==============================================================================
# FastAPI Router
# ==============================================================================

broadcast_router = APIRouter(prefix="/api", tags=["broadcast"])


@broadcast_router.post("/broadcast", response_model=BroadcastResponse)
async def broadcast_event(event: BroadcastEvent) -> BroadcastResponse:
    """
    Broadcast an event to all connected WebSocket clients.
    
    This endpoint is called by the supervisor to inject maintenance events
    into the WebSocket stream before shutting down JARVIS.
    
    Example:
        POST /api/broadcast
        {
            "event": "system_updating",
            "data": {
                "type": "system_updating",
                "message": "Downloading updates...",
                "estimated_time": 30
            }
        }
    """
    logger.info(f"📡 Broadcast request: {event.event}")
    
    # Build message to broadcast
    message = {
        "type": event.event,
        **event.data,
    }
    
    # Broadcast to all clients
    notified = await manager.broadcast(message)
    
    return BroadcastResponse(
        success=True,
        message=f"Event {event.event} broadcast successfully",
        clients_notified=notified,
    )


@broadcast_router.get("/broadcast/stats")
async def get_broadcast_stats():
    """Get broadcast connection statistics."""
    return manager.get_stats()


@broadcast_router.get("/speech-state")
async def get_speech_state():
    """
    Get current JARVIS speech state for self-voice suppression.
    
    v8.0: Returns whether JARVIS is speaking, in cooldown, and recent spoken text.
    This allows frontend to synchronize speech state without waiting for WebSocket.
    
    Returns:
        {
            "is_speaking": bool,
            "in_cooldown": bool,
            "cooldown_remaining_ms": int,
            "last_spoken_text": str,
            "last_spoken_timestamp": float,
            "should_block_audio": bool
        }
    """
    try:
        from core.unified_speech_state import get_speech_state_manager
        speech_manager = await get_speech_state_manager()
        
        import time
        current_time = time.time()
        
        # Get state properties
        is_speaking = speech_manager.is_speaking
        is_in_cooldown = speech_manager.is_in_cooldown
        state = speech_manager.get_state()
        cooldown_remaining = state.get_cooldown_remaining_ms() if hasattr(state, 'get_cooldown_remaining_ms') else 0
        last_spoken_text = speech_manager.last_spoken_text
        last_spoken_timestamp = speech_manager.last_spoken_timestamp
        
        return {
            "is_speaking": is_speaking,
            "in_cooldown": is_in_cooldown,
            "cooldown_remaining_ms": cooldown_remaining,
            "last_spoken_text": last_spoken_text,
            "last_spoken_timestamp": last_spoken_timestamp,
            "should_block_audio": is_speaking or is_in_cooldown,
        }
    except ImportError:
        return {
            "is_speaking": False,
            "in_cooldown": False,
            "cooldown_remaining_ms": 0,
            "last_spoken_text": "",
            "last_spoken_timestamp": 0,
            "should_block_audio": False,
            "error": "UnifiedSpeechStateManager not available",
        }
    except Exception as e:
        return {
            "is_speaking": False,
            "in_cooldown": False,
            "cooldown_remaining_ms": 0,
            "last_spoken_text": "",
            "last_spoken_timestamp": 0,
            "should_block_audio": False,
            "error": str(e),
        }


@broadcast_router.websocket("/broadcast/ws")
async def broadcast_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for receiving broadcast events.
    
    Clients connect here to receive maintenance mode notifications
    and other system events from the supervisor.
    
    Usage (JavaScript):
        const ws = new WebSocket('ws://localhost:8010/api/broadcast/ws');
        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === 'system_updating') {
                showMaintenanceOverlay();
            }
        };
    """
    conn_id = await manager.connect(websocket)
    
    # Send welcome message
    await manager.send_personal(websocket, {
        "type": "connected",
        "message": "Connected to JARVIS broadcast channel",
        "connection_id": conn_id,
    })
    
    # WebSocket idle timeout protection
    idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

    try:
        while True:
            # Keep connection alive, handle pings/pongs with timeout
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info(f"Broadcast WebSocket idle timeout for {conn_id}, closing connection")
                break

            # Handle ping/pong for keepalive
            if data.get("type") == "ping":
                await manager.send_personal(websocket, {"type": "pong"})

    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
        await manager.disconnect(websocket)


# ==============================================================================
# Alternative Endpoint (for compatibility)
# ==============================================================================

# Also register at root /broadcast for compatibility with maintenance_broadcaster.py
alt_router = APIRouter(tags=["broadcast"])


@alt_router.post("/broadcast")
async def broadcast_event_alt(event: BroadcastEvent) -> BroadcastResponse:
    """Alternative endpoint at /broadcast for compatibility."""
    return await broadcast_event(event)


@alt_router.websocket("/ws/broadcast")
async def broadcast_ws_alt(websocket: WebSocket):
    """Alternative WebSocket endpoint at /ws/broadcast."""
    await broadcast_websocket(websocket)


# ==============================================================================
# Utility Functions
# ==============================================================================

async def broadcast_maintenance_event(
    reason: str,
    message: str,
    estimated_time: int = 30,
) -> int:
    """
    Broadcast a maintenance event directly (for internal use).
    
    Args:
        reason: 'updating' | 'restarting' | 'rollback'
        message: Human-readable status message
        estimated_time: Seconds until back online
        
    Returns:
        Number of clients notified
    """
    event_message = {
        "type": f"system_{reason}",
        "message": message,
        "estimated_time": estimated_time,
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
    }
    
    return await manager.broadcast(event_message)


def get_routers():
    """Get all routers to include in the FastAPI app."""
    return [broadcast_router, alt_router]


# ==============================================================================
# Alternative Endpoint (for compatibility)
# ==============================================================================

# Also register at root /broadcast for compatibility with maintenance_broadcaster.py
alt_router = APIRouter(tags=["broadcast"])


@alt_router.post("/broadcast")
async def broadcast_event_alt(event: BroadcastEvent) -> BroadcastResponse:
    """Alternative endpoint at /broadcast for compatibility."""
    return await broadcast_event(event)


@alt_router.websocket("/ws/broadcast")
async def broadcast_ws_alt(websocket: WebSocket):
    """Alternative WebSocket endpoint at /ws/broadcast."""
    await broadcast_websocket(websocket)


# ==============================================================================
# Utility Functions
# ==============================================================================

async def broadcast_maintenance_event(
    reason: str,
    message: str,
    estimated_time: int = 30,
) -> int:
    """
    Broadcast a maintenance event directly (for internal use).
    
    Args:
        reason: 'updating' | 'restarting' | 'rollback'
        message: Human-readable status message
        estimated_time: Seconds until back online
        
    Returns:
        Number of clients notified
    """
    event_message = {
        "type": f"system_{reason}",
        "message": message,
        "estimated_time": estimated_time,
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
    }
    
    return await manager.broadcast(event_message)


def get_routers():
    """Get all routers to include in the FastAPI app."""
    return [broadcast_router, alt_router]
