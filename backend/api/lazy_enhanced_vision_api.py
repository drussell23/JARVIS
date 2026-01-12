#!/usr/bin/env python3
"""
Lazy Enhanced Vision API for JARVIS
Defers initialization until first use
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from typing import Dict, Any, List, Set, Optional
import asyncio
import logging
import json
from datetime import datetime
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vision", tags=["vision"])

class LazyEnhancedVisionWebSocketManager:
    """Manages WebSocket connections with lazy initialization"""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        
        # Lazy components
        self._ai_core = None
        self._autonomy_handler = None
        self._vision_monitor = None
        self._initialized = False
        self.monitoring_active = False
        
    async def _initialize_if_needed(self):
        """Initialize components on first use"""
        if self._initialized:
            return
            
        logger.info("Lazy initializing Enhanced Vision WebSocket Manager")
        
        # Import and initialize only when needed
        try:
            from core.jarvis_ai_core import get_jarvis_ai_core
            self._ai_core = get_jarvis_ai_core()
        except ImportError:
            logger.warning("Could not import jarvis_ai_core")
            
        try:
            from api.autonomy_handler import get_autonomy_handler
            self._autonomy_handler = get_autonomy_handler()
        except ImportError:
            logger.warning("Could not import autonomy_handler")
        
        # Initialize vision monitor only if API key exists
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            try:
                from vision.continuous_vision_monitor import ContinuousVisionMonitor
                self._vision_monitor = ContinuousVisionMonitor(api_key)
                # Set up callbacks
                self._vision_monitor.add_update_callback(self._handle_vision_update)
                self._vision_monitor.add_action_callback(self._handle_action_suggestion)
                logger.info("Vision monitor initialized with API key")
            except ImportError:
                logger.warning("Could not import ContinuousVisionMonitor")
        else:
            logger.warning("Vision monitor not initialized - no API key")
            
        self._initialized = True
        
    @property
    def ai_core(self):
        """Get AI core (lazy)"""
        if not self._initialized:
            asyncio.create_task(self._initialize_if_needed())
        return self._ai_core
        
    @property
    def autonomy_handler(self):
        """Get autonomy handler (lazy)"""
        if not self._initialized:
            asyncio.create_task(self._initialize_if_needed())
        return self._autonomy_handler
        
    @property
    def vision_monitor(self):
        """Get vision monitor (lazy)"""
        if not self._initialized:
            asyncio.create_task(self._initialize_if_needed())
        return self._vision_monitor
        
    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection"""
        await websocket.accept()
        
        # Initialize if needed
        await self._initialize_if_needed()
        
        self.active_connections.add(websocket)
        logger.info(f"Vision WebSocket connected. Total connections: {len(self.active_connections)}")
        
        # Send initial status
        await self.send_to_client(websocket, {
            "type": "connection",
            "status": "connected",
            "monitoring": self.monitoring_active,
            "timestamp": datetime.now().isoformat()
        })
        
    async def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection"""
        self.active_connections.discard(websocket)
        logger.info(f"Vision WebSocket disconnected. Remaining connections: {len(self.active_connections)}")
        
        # Stop monitoring if no connections
        if not self.active_connections and self.monitoring_active:
            await self.stop_monitoring()
            
    async def send_to_client(self, websocket: WebSocket, data: Dict[str, Any]):
        """Send data to specific client"""
        try:
            await websocket.send_json(data)
        except Exception as e:
            logger.error(f"Error sending to client: {e}")
            
    async def broadcast(self, data: Dict[str, Any]):
        """Broadcast data to all connected clients"""
        disconnected = set()
        for connection in self.active_connections:
            try:
                await connection.send_json(data)
            except Exception:
                disconnected.add(connection)
                
        # Remove disconnected clients
        for conn in disconnected:
            await self.disconnect(conn)
            
    async def start_monitoring(self):
        """Start continuous vision monitoring"""
        await self._initialize_if_needed()
        
        if not self.vision_monitor:
            return {"error": "Vision monitor not available"}
            
        if self.monitoring_active:
            return {"status": "already_monitoring"}
            
        self.monitoring_active = True
        asyncio.create_task(self.vision_monitor.start())
        
        await self.broadcast({
            "type": "monitoring_status",
            "active": True,
            "timestamp": datetime.now().isoformat()
        })
        
        logger.info("Started continuous vision monitoring")
        return {"status": "monitoring_started"}
        
    async def stop_monitoring(self):
        """Stop continuous vision monitoring"""
        if not self.monitoring_active or not self.vision_monitor:
            return {"status": "not_monitoring"}
            
        self.monitoring_active = False
        await self.vision_monitor.stop()
        
        await self.broadcast({
            "type": "monitoring_status", 
            "active": False,
            "timestamp": datetime.now().isoformat()
        })
        
        logger.info("Stopped continuous vision monitoring")
        return {"status": "monitoring_stopped"}
        
    async def _handle_vision_update(self, update: Dict[str, Any]):
        """Handle vision updates from monitor"""
        logger.info(f"Vision update: {update.get('type', 'unknown')}")
        
        # Process through AI core if available
        if self.ai_core:
            processed = await self.ai_core.process_vision_update(update)
            update['ai_analysis'] = processed
            
        # Check for autonomous actions
        if self.autonomy_handler and update.get('requires_action'):
            action = await self.autonomy_handler.handle_vision_update(update)
            update['autonomous_action'] = action
            
        await self.broadcast({
            "type": "vision_update",
            "data": update,
            "timestamp": datetime.now().isoformat()
        })
        
    async def _handle_action_suggestion(self, action: Dict[str, Any]):
        """Handle action suggestions from vision monitor"""
        logger.info(f"Action suggestion: {action.get('type', 'unknown')}")
        
        # Process through autonomy handler if available
        if self.autonomy_handler:
            result = await self.autonomy_handler.execute_action(action)
            action['execution_result'] = result
            
        await self.broadcast({
            "type": "action_suggestion",
            "data": action,
            "timestamp": datetime.now().isoformat()
        })
        
    async def handle_message(self, websocket: WebSocket, message: Dict[str, Any]):
        """Handle incoming WebSocket message"""
        msg_type = message.get("type", "")
        
        if msg_type == "start_monitoring":
            result = await self.start_monitoring()
            await self.send_to_client(websocket, result)
            
        elif msg_type == "stop_monitoring":
            result = await self.stop_monitoring()
            await self.send_to_client(websocket, result)
            
        elif msg_type == "capture":
            if self.vision_monitor:
                screenshot = await self.vision_monitor.capture_screenshot()
                await self.send_to_client(websocket, {
                    "type": "screenshot",
                    "data": screenshot,
                    "timestamp": datetime.now().isoformat()
                })
            else:
                await self.send_to_client(websocket, {"error": "Vision monitor not available"})
                
        elif msg_type == "analyze":
            if self.vision_monitor:
                analysis = await self.vision_monitor.analyze_current_view()
                await self.send_to_client(websocket, {
                    "type": "analysis",
                    "data": analysis,
                    "timestamp": datetime.now().isoformat()
                })
            else:
                await self.send_to_client(websocket, {"error": "Vision monitor not available"})
                
        else:
            await self.send_to_client(websocket, {
                "error": f"Unknown message type: {msg_type}"
            })

# Create manager instance
manager = LazyEnhancedVisionWebSocketManager()

@router.websocket("/ws/vision")
async def vision_websocket_endpoint(websocket: WebSocket):
    """Enhanced vision WebSocket endpoint with continuous monitoring"""
    await manager.connect(websocket)
    
    # WebSocket idle timeout protection
    idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

    try:
        while True:
            # Receive message with timeout
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("Lazy enhanced vision WebSocket idle timeout, closing connection")
                break

            # Handle message
            await manager.handle_message(websocket, data)

    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await manager.disconnect(websocket)

@router.post("/start_monitoring")
async def start_monitoring():
    """Start continuous vision monitoring"""
    try:
        from graceful_http_handler import graceful_endpoint
        endpoint = graceful_endpoint(fallback_response={"error": "Service temporarily unavailable"})
        return await endpoint(manager.start_monitoring)()
    except ImportError:
        return await manager.start_monitoring()

@router.post("/stop_monitoring") 
async def stop_monitoring():
    """Stop continuous vision monitoring"""
    try:
        from graceful_http_handler import graceful_endpoint
        endpoint = graceful_endpoint(fallback_response={"error": "Service temporarily unavailable"})
        return await endpoint(manager.stop_monitoring)()
    except ImportError:
        return await manager.stop_monitoring()

@router.get("/status")
async def get_vision_status():
    """Get current vision system status"""
    await manager._initialize_if_needed()
    
    status = {
        "monitoring_active": manager.monitoring_active,
        "connections": len(manager.active_connections),
        "vision_monitor": manager.vision_monitor is not None,
        "ai_integration": "Claude Opus 4" if manager.vision_monitor else "Not available",
        "capabilities": []
    }
    
    if manager.vision_monitor:
        status["capabilities"] = [
            "continuous_monitoring",
            "claude_vision_analysis",
            "notification_detection",
            "workspace_understanding",
            "pattern_learning",
            "autonomous_suggestions"
        ]
        
    return status