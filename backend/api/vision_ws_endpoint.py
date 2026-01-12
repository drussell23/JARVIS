"""
Vision WebSocket Endpoint
========================

Provides /vision/ws/vision endpoint for vision-related WebSocket connections.
"""

import asyncio
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import logging
import json
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter()

# Vision WebSocket manager
class VisionWebSocketManager:
    """Manages vision WebSocket connections"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.vision_analyzer = None
        
    async def connect(self, websocket: WebSocket, client_id: str):
        """Accept and store WebSocket connection"""
        await websocket.accept()
        self.active_connections[client_id] = websocket
        logger.info(f"Vision WebSocket connected: {client_id}")
        
        # Send welcome message
        await websocket.send_json({
            "type": "connection_established",
            "client_id": client_id,
            "timestamp": datetime.now().isoformat(),
            "service": "vision",
            "capabilities": ["screen_capture", "analyze", "monitor", "workspace_analysis"]
        })
        
    def disconnect(self, client_id: str):
        """Remove WebSocket connection"""
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.info(f"Vision WebSocket disconnected: {client_id}")
            
    async def handle_message(self, websocket: WebSocket, message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming vision WebSocket messages"""
        msg_type = message.get("type", "")
        
        try:
            if msg_type == "ping":
                return {"type": "pong", "timestamp": datetime.now().isoformat()}
                
            elif msg_type == "capture_screen":
                if not self.vision_analyzer:
                    return {"type": "error", "error": "Vision analyzer not available"}
                    
                result = await self.vision_analyzer.capture_screen()
                return {
                    "type": "screen_captured",
                    "success": result is not None,
                    "has_image": result is not None
                }
                
            elif msg_type == "analyze_screen":
                if not self.vision_analyzer:
                    return {"type": "error", "error": "Vision analyzer not available"}
                    
                prompt = message.get("prompt", "What's on the screen?")
                result = await self.vision_analyzer.analyze_screen(prompt)
                
                return {
                    "type": "analysis_result",
                    "result": result,
                    "timestamp": datetime.now().isoformat()
                }
                
            elif msg_type == "start_monitoring":
                if not self.vision_analyzer:
                    return {"type": "error", "error": "Vision analyzer not available"}
                    
                await self.vision_analyzer.start_video_streaming()
                return {
                    "type": "monitoring_started",
                    "timestamp": datetime.now().isoformat()
                }
                
            elif msg_type == "stop_monitoring":
                if not self.vision_analyzer:
                    return {"type": "error", "error": "Vision analyzer not available"}
                    
                await self.vision_analyzer.stop_video_streaming()
                return {
                    "type": "monitoring_stopped",
                    "timestamp": datetime.now().isoformat()
                }
                
            else:
                return {
                    "type": "error",
                    "error": f"Unknown message type: {msg_type}",
                    "supported_types": ["ping", "capture_screen", "analyze_screen", "start_monitoring", "stop_monitoring"]
                }
                
        except Exception as e:
            logger.error(f"Error handling vision WebSocket message: {e}")
            return {
                "type": "error",
                "error": str(e),
                "original_type": msg_type
            }
            
    async def broadcast(self, message: Dict[str, Any]):
        """Broadcast message to all connected clients"""
        disconnected = []
        
        for client_id, websocket in self.active_connections.items():
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.append(client_id)
                
        # Clean up disconnected clients
        for client_id in disconnected:
            self.disconnect(client_id)
            
    def set_vision_analyzer(self, analyzer):
        """Set the vision analyzer instance"""
        self.vision_analyzer = analyzer
        logger.info("Vision analyzer set in VisionWebSocketManager")

# Create global manager instance
vision_ws_manager = VisionWebSocketManager()

@router.websocket("/vision/ws/vision")
async def vision_websocket_endpoint(websocket: WebSocket):
    """Vision WebSocket endpoint at /vision/ws/vision"""
    # Generate client ID
    client_host = websocket.client.host if websocket.client else "unknown"
    client_id = f"vision_{client_host}_{datetime.now().timestamp()}"
    
    # Connect
    await vision_ws_manager.connect(websocket, client_id)
    
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
                logger.info(f"Vision WebSocket idle timeout for {client_id}, closing connection")
                break

            # Handle message
            response = await vision_ws_manager.handle_message(websocket, data)

            # Send response
            await websocket.send_json(response)

    except WebSocketDisconnect:
        vision_ws_manager.disconnect(client_id)
    except Exception as e:
        logger.error(f"Vision WebSocket error: {e}")
        vision_ws_manager.disconnect(client_id)
        try:
            await websocket.close()
        except Exception:
            pass

def set_vision_analyzer(analyzer):
    """Set the vision analyzer in the WebSocket manager"""
    vision_ws_manager.set_vision_analyzer(analyzer)