#!/usr/bin/env python3
"""
Notification Vision API Integration
Connects the vision system with intelligent notification detection and voice announcements
"""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import asyncio
import logging
from datetime import datetime

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autonomy.notification_intelligence import (
    NotificationIntelligence, 
    NotificationVisionIntegration,
    IntelligentNotification
)
from autonomy.vision_decision_pipeline import VisionDecisionPipeline
from api.vision_api import vision_pipeline, ws_manager
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graceful_http_handler import graceful_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

# Initialize notification intelligence
notification_intelligence = NotificationIntelligence()
notification_integration = NotificationVisionIntegration(vision_pipeline)

# Notification statistics
notification_stats = {
    'total_detected': 0,
    'total_announced': 0,
    'by_app': {},
    'by_context': {},
    'last_notification': None
}

class NotificationPreferences(BaseModel):
    """User preferences for notifications"""
    announce_all: bool = True
    min_urgency_threshold: float = 0.5
    quiet_hours_enabled: bool = False
    quiet_hours_start: Optional[int] = 22
    quiet_hours_end: Optional[int] = 8
    enabled_contexts: List[str] = [
        "message_received", "meeting_reminder", "urgent_alert", "work_update"
    ]

class NotificationWebSocketManager:
    """Manages WebSocket connections for real-time notification updates"""
    
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.notification_buffer: List[IntelligentNotification] = []
        
    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.append(websocket)
        
        # Send recent notifications
        await self.send_recent_notifications(websocket)
        
    def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection"""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            
    async def send_recent_notifications(self, websocket: WebSocket):
        """Send recent notifications to new connection"""
        recent = self.notification_buffer[-10:]  # Last 10 notifications
        
        for notification in recent:
            await websocket.send_json({
                "type": "historical_notification",
                "notification": self._serialize_notification(notification)
            })
            
    async def broadcast_notification(self, notification: IntelligentNotification):
        """Broadcast notification to all connected clients"""
        self.notification_buffer.append(notification)
        
        # Keep buffer size manageable
        if len(self.notification_buffer) > 100:
            self.notification_buffer = self.notification_buffer[-100:]
            
        # Broadcast to all connections
        for connection in self.active_connections:
            try:
                await connection.send_json({
                    "type": "new_notification",
                    "notification": self._serialize_notification(notification),
                    "timestamp": datetime.now().isoformat()
                })
            except Exception as e:
                logger.error(f"Error broadcasting notification: {e}")
                
    def _serialize_notification(self, notification: IntelligentNotification) -> Dict[str, Any]:
        """Convert notification to JSON-serializable format"""
        return {
            "app_name": notification.app_name,
            "context": notification.context.value,
            "text": notification.detected_text,
            "urgency": notification.urgency_score,
            "confidence": notification.confidence,
            "timestamp": notification.timestamp.isoformat()
        }

# Initialize WebSocket manager
notification_ws_manager = NotificationWebSocketManager()

# Hook into vision pipeline for notification detection
async def vision_pipeline_notification_hook(context: Dict[str, Any]):
    """
    Hook that runs in vision pipeline to detect notifications
    """
    # Extract workspace state
    workspace_state = context.get('workspace_state', {})
    
    # Detect notifications using intelligence system
    notifications = await notification_intelligence._detect_notifications_intelligently(workspace_state)
    
    # Update statistics
    for notification in notifications:
        notification_stats['total_detected'] += 1
        
        # Update app stats
        app_count = notification_stats['by_app'].get(notification.app_name, 0)
        notification_stats['by_app'][notification.app_name] = app_count + 1
        
        # Update context stats
        context_count = notification_stats['by_context'].get(notification.context.value, 0)
        notification_stats['by_context'][notification.context.value] = context_count + 1
        
        notification_stats['last_notification'] = datetime.now().isoformat()
        
        # Broadcast via WebSocket
        await notification_ws_manager.broadcast_notification(notification)
        
        # Process for announcement
        if await notification_intelligence._should_announce(notification):
            await notification_intelligence._process_intelligent_notification(notification)
            notification_stats['total_announced'] += 1
    
    # Add notifications to context for decision engine
    context['detected_notifications'] = notifications
    
    return context

# Register hook with vision pipeline
if hasattr(vision_pipeline, 'register_processing_hook'):
    vision_pipeline.register_processing_hook('notification_detection', vision_pipeline_notification_hook)

@router.get("/status")
async def get_notification_status() -> Dict[str, Any]:
    """Get notification system status"""
    return {
        "enabled": notification_intelligence.is_monitoring,
        "statistics": notification_stats,
        "detection_patterns": len(notification_intelligence.detection_patterns),
        "announcement_history": len(notification_intelligence.announcement_history),
        "active_websockets": len(notification_ws_manager.active_connections)
    }

@router.post("/preferences")
async def update_notification_preferences(preferences: NotificationPreferences) -> Dict[str, Any]:
    """Update notification preferences"""
    # Update intelligence system preferences
    # This would be integrated with the actual preference storage
    
    return {
        "success": True,
        "message": "Preferences updated",
        "preferences": preferences.dict()
    }

@router.post("/test")
async def test_notification_announcement(
    app_name: str = "Test App",
    message: str = "This is a test notification"
) -> Dict[str, Any]:
    """Test notification announcement"""
    try:
        # Create test notification
        test_notification = IntelligentNotification(
            source_window_id="test_window",
            app_name=app_name,
            detected_text=[message],
            visual_elements={},
            urgency_score=0.8,
            confidence=0.9
        )
        
        # Generate and speak announcement
        announcement = await notification_intelligence._generate_natural_announcement(test_notification)
        await notification_intelligence.voice_module.speak(announcement)
        
        return {
            "success": True,
            "notification": {
                "app_name": app_name,
                "message": message,
                "announcement": announcement
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history")
async def get_notification_history(limit: int = 50) -> Dict[str, Any]:
    """Get notification announcement history"""
    history = notification_intelligence.announcement_history[-limit:]
    
    return {
        "count": len(history),
        "notifications": [
            {
                "app_name": item['notification'].app_name,
                "context": item['notification'].context.value,
                "announcement": item['announcement'],
                "timestamp": item['timestamp'].isoformat()
            }
            for item in history
        ]
    }

@router.get("/learning/patterns")
async def get_learned_patterns() -> Dict[str, Any]:
    """Get learned notification detection patterns"""
    patterns = notification_intelligence.detection_patterns
    
    return {
        "pattern_types": list(patterns.keys()),
        "total_patterns": sum(len(p) for p in patterns.values()),
        "patterns": {
            ptype: len(plist) for ptype, plist in patterns.items()
        }
    }

@router.websocket("/ws")
async def notification_websocket(websocket: WebSocket):
    """WebSocket endpoint for real-time notification updates"""
    await notification_ws_manager.connect(websocket)
    
    # WebSocket idle timeout protection
    idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

    try:
        while True:
            # Keep connection alive and handle any client messages with timeout
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("Notification WebSocket idle timeout, closing connection")
                break

            # Handle client commands
            if data.get("command") == "get_stats":
                await websocket.send_json({
                    "type": "stats_update",
                    "statistics": notification_stats
                })
            elif data.get("command") == "clear_history":
                notification_intelligence.announcement_history.clear()
                await websocket.send_json({
                    "type": "history_cleared",
                    "success": True
                })

    except WebSocketDisconnect:
        notification_ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        notification_ws_manager.disconnect(websocket)

# Integration with main vision WebSocket
async def enhance_vision_websocket_with_notifications():
    """
    Enhance the main vision WebSocket to include notification data
    """
    # This would be called to add notification data to vision updates
    original_broadcast = ws_manager.broadcast
    
    async def enhanced_broadcast(message: Dict[str, Any]):
        # Add notification data to workspace updates
        if message.get("type") == "workspace_update":
            # Get recent notifications
            recent_notifications = notification_ws_manager.notification_buffer[-5:]
            
            message["notifications"] = {
                "recent": [
                    notification_ws_manager._serialize_notification(n)
                    for n in recent_notifications
                ],
                "stats": {
                    "total_today": notification_stats['total_detected'],
                    "announced_today": notification_stats['total_announced']
                }
            }
            
        # Call original broadcast
        await original_broadcast(message)
    
    # Replace broadcast method
    ws_manager.broadcast = enhanced_broadcast

# Auto-start notification intelligence when API loads
async def startup_notification_system():
    """Start notification system on API startup"""
    await notification_intelligence.start_intelligent_monitoring()
    await enhance_vision_websocket_with_notifications()
    logger.info("Notification intelligence system started")

# Register startup - commented out to prevent event loop error
# The notification system will be initialized when first accessed
# if __name__ != "__main__":
#     asyncio.create_task(startup_notification_system())