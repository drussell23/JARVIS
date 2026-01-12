#!/usr/bin/env python3
"""
Enhanced Vision API for JARVIS
Integrates continuous monitoring with Claude AI
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from typing import Dict, Any, List, Set
import asyncio
import logging
import json
from datetime import datetime
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graceful_http_handler import graceful_endpoint

from core.jarvis_ai_core import get_jarvis_ai_core
from vision.continuous_vision_monitor import ContinuousVisionMonitor
from api.autonomy_handler import get_autonomy_handler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vision", tags=["vision"])

class EnhancedVisionWebSocketManager:
    """Manages WebSocket connections for enhanced vision system"""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.ai_core = get_jarvis_ai_core()
        self.autonomy_handler = get_autonomy_handler()
        
        # Initialize continuous monitor
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            self.vision_monitor = ContinuousVisionMonitor(api_key)
            # Set up callbacks
            self.vision_monitor.add_update_callback(self._handle_vision_update)
            self.vision_monitor.add_action_callback(self._handle_action_suggestion)
        else:
            self.vision_monitor = None
            logger.warning("Vision monitor not initialized - no API key")
        
        self.monitoring_active = False
        
    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"Enhanced Vision WebSocket connected. Total: {len(self.active_connections)}")
        
        # Send initial state
        await self.send_initial_state(websocket)
        
        # Start monitoring if in autonomous mode
        if self.autonomy_handler.is_autonomous and not self.monitoring_active:
            await self.start_monitoring()
    
    def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection"""
        self.active_connections.discard(websocket)
        logger.info(f"Vision WebSocket disconnected. Total: {len(self.active_connections)}")
        
        # Stop monitoring if no connections
        if not self.active_connections and self.monitoring_active:
            asyncio.create_task(self.stop_monitoring())
    
    async def send_initial_state(self, websocket: WebSocket):
        """Send initial state to new connection"""
        try:
            # Get current AI core status
            ai_status = self.ai_core.get_status()
            
            initial_state = {
                "type": "initial_state",
                "timestamp": datetime.now().isoformat(),
                "workspace": {
                    "window_count": ai_status.get("workspace_context", {}).get("window_count", 0),
                    "focused_app": ai_status.get("workspace_context", {}).get("focused_app"),
                    "context": ai_status.get("workspace_context", {}).get("context", "Initializing..."),
                    "notifications": [],
                    "confidence": 0.9
                },
                "autonomous_mode": self.autonomy_handler.is_autonomous,
                "monitoring_active": self.monitoring_active,
                "ai_status": ai_status
            }
            
            await websocket.send_json(initial_state)
            
        except Exception as e:
            logger.error(f"Error sending initial state: {e}")
    
    async def broadcast(self, message: Dict[str, Any]):
        """Broadcast message to all connected clients"""
        disconnected = set()
        
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting: {e}")
                disconnected.add(connection)
        
        # Clean up disconnected clients
        for conn in disconnected:
            self.disconnect(conn)
    
    async def start_monitoring(self):
        """Start continuous monitoring"""
        if not self.vision_monitor:
            logger.error("Vision monitor not initialized")
            return
        
        if self.monitoring_active:
            return
        
        self.monitoring_active = True
        await self.vision_monitor.start_monitoring()
        logger.info("Started enhanced vision monitoring")
        
        # Notify clients
        await self.broadcast({
            "type": "monitoring_status",
            "monitoring_active": True,
            "timestamp": datetime.now().isoformat()
        })
    
    async def stop_monitoring(self):
        """Stop continuous monitoring"""
        if not self.vision_monitor or not self.monitoring_active:
            return
        
        self.monitoring_active = False
        await self.vision_monitor.stop_monitoring()
        logger.info("Stopped vision monitoring")
        
        # Notify clients
        await self.broadcast({
            "type": "monitoring_status",
            "monitoring_active": False,
            "timestamp": datetime.now().isoformat()
        })
    
    async def _handle_vision_update(self, update: Dict[str, Any]):
        """Handle vision updates from monitor"""
        # Process through AI core
        ai_analysis = await self.ai_core.process_vision(
            update.get("screenshot", {}),
            mode="multi"
        )
        
        # Prepare update message
        message = {
            "type": "workspace_update",
            "timestamp": datetime.now().isoformat(),
            "workspace": {
                "window_count": update.get("window_count", 0),
                "focused_app": update.get("focused_app"),
                "context": ai_analysis.get("context", ""),
                "notifications": ai_analysis.get("notifications", []),
                "suggestions": ai_analysis.get("suggestions", []),
                "confidence": 0.9
            },
            "ai_analysis": ai_analysis
        }
        
        # Broadcast to all clients
        await self.broadcast(message)
    
    async def _handle_action_suggestion(self, actions: List[Dict[str, Any]]):
        """Handle suggested actions from vision monitor"""
        # Filter actions based on confidence
        high_confidence_actions = [
            action for action in actions 
            if action.get("priority") in ["high", "urgent"]
        ]
        
        if high_confidence_actions:
            # Process through AI core
            for action in high_confidence_actions:
                result = await self.ai_core.execute_task(action)
                
                # Notify clients
                await self.broadcast({
                    "type": "action_executed",
                    "action": action,
                    "result": result,
                    "timestamp": datetime.now().isoformat()
                })
    
    async def handle_command(self, websocket: WebSocket, data: Dict[str, Any]):
        """Handle commands from client"""
        command_type = data.get("type")
        
        if command_type == "request_analysis":
            # Immediate analysis
            if self.vision_monitor:
                analysis = await self.vision_monitor.analyze_current_screen()
                await websocket.send_json({
                    "type": "workspace_analysis",
                    "timestamp": datetime.now().isoformat(),
                    "analysis": analysis
                })
        
        elif command_type == "set_monitoring":
            # Toggle monitoring
            enabled = data.get("enabled", False)
            if enabled:
                await self.start_monitoring()
            else:
                await self.stop_monitoring()
        
        elif command_type == "execute_action":
            # Execute a specific action
            action = data.get("action", {})
            result = await self.ai_core.execute_task(action)
            await websocket.send_json({
                "type": "action_result",
                "action": action,
                "result": result,
                "timestamp": datetime.now().isoformat()
            })

# Create manager instance
vision_ws_manager = EnhancedVisionWebSocketManager()

@router.websocket("/ws/vision")
async def enhanced_vision_websocket(websocket: WebSocket):
    """Enhanced WebSocket endpoint for vision system"""
    await vision_ws_manager.connect(websocket)
    
    # WebSocket idle timeout protection
    idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

    try:
        while True:
            # Receive message from client with timeout
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("Enhanced vision WebSocket idle timeout, closing connection")
                break

            # Handle the command
            await vision_ws_manager.handle_command(websocket, data)

    except WebSocketDisconnect:
        vision_ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        vision_ws_manager.disconnect(websocket)

@router.get("/status")
async def get_enhanced_vision_status():
    """Get enhanced vision system status"""
    ai_status = vision_ws_manager.ai_core.get_status()
    
    monitor_status = {}
    if vision_ws_manager.vision_monitor:
        monitor_status = vision_ws_manager.vision_monitor.get_status()
    
    return {
        "vision_enabled": True,
        "monitoring_active": vision_ws_manager.monitoring_active,
        "connected_clients": len(vision_ws_manager.active_connections),
        "ai_integration": "Claude Opus 4",
        "autonomous_mode": vision_ws_manager.autonomy_handler.is_autonomous,
        "capabilities": [
            "continuous_monitoring",
            "claude_vision_analysis",
            "multi_window_detection",
            "notification_detection",
            "autonomous_actions",
            "pattern_learning"
        ],
        "ai_status": ai_status,
        "monitor_status": monitor_status
    }

@router.post("/toggle_monitoring")
async def toggle_monitoring(enabled: bool = True):
    """Toggle vision monitoring"""
    if enabled:
        await vision_ws_manager.start_monitoring()
    else:
        await vision_ws_manager.stop_monitoring()
    
    return {
        "monitoring_active": vision_ws_manager.monitoring_active,
        "status": "enabled" if vision_ws_manager.monitoring_active else "disabled"
    }

@router.post("/analyze_now")
@graceful_endpoint
async def analyze_current_screen():
    """Analyze screen immediately with timeout"""
    if not vision_ws_manager.vision_monitor:
        # Return graceful response instead of 503
        return {
            "analysis": "Vision system is initializing. Please try again shortly.",
            "status": "initializing",
            "confidence": 0.7,
            "retry_after_seconds": 3
        }
    
    try:
        # Add 30 second timeout for vision analysis
        analysis = await asyncio.wait_for(
            vision_ws_manager.vision_monitor.analyze_current_screen(),
            timeout=30.0
        )
        
        # Process through AI core with 15 second timeout
        ai_analysis = await asyncio.wait_for(
            vision_ws_manager.ai_core.process_vision(
                analysis.get("screenshot", {}),
                mode="multi"
            ),
            timeout=15.0
        )
        
        return {
            "raw_analysis": analysis,
            "ai_analysis": ai_analysis,
            "timestamp": datetime.now().isoformat()
        }
    except asyncio.TimeoutError:
        logger.error("Vision analysis timed out after 30 seconds")
        raise HTTPException(
            status_code=504,
            detail="Vision analysis timed out. Please try again."
        )
    except Exception as e:
        logger.error(f"Vision analysis error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Vision analysis failed: {str(e)}"
        )

@router.get("/can_see")
async def vision_confirmation():
    """
    Fast endpoint to confirm vision capability.
    Returns in <100ms instead of 3-9 seconds.
    """
    from vision.natural_responses import confirm_vision_capability
    
    result = await confirm_vision_capability()
    
    return {
        "status": "success" if result["success"] else "error",
        "message": result["response"],
        "performance_ms": result["performance_ms"],
        "cached": result.get("cached", False),
        "screen_info": result.get("screen_info"),
        "confidence": result["confidence"]
    }