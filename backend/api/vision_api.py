"""
Vision API endpoints for JARVIS screen comprehension
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Dict, List, Optional, Any, Tuple, Set
import os
from datetime import datetime
import asyncio
import logging
import traceback

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from graceful_http_handler import graceful_endpoint
except ImportError:
    # Fallback if graceful handler is not available
    def graceful_endpoint(fallback_response=None, **kwargs):
        def decorator(func):
            return func
        return decorator

from vision.screen_vision import ScreenVisionSystem, JARVISVisionIntegration
from vision.claude_vision_analyzer import ClaudeVisionAnalyzer
from vision.workspace_analyzer import WorkspaceAnalyzer
from vision.window_detector import WindowDetector
from vision.enhanced_monitoring import EnhancedWorkspaceMonitor
from vision.multi_monitor_detector import MultiMonitorDetector, MonitorCaptureResult, MACOS_AVAILABLE
from autonomy.autonomous_behaviors import AutonomousBehaviorManager
from autonomy.action_queue import ActionQueueManager
from autonomy.action_executor import ExecutionStatus
from autonomy.vision_decision_pipeline import VisionDecisionPipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vision", tags=["vision"])

# Initialize vision systems
vision_system = ScreenVisionSystem()
claude_analyzer = None
multi_monitor_detector = MultiMonitorDetector()

# Import unified handler for WebSocket integration
from api.unified_vision_handler import handle_vision_command as unified_handle_vision_command

# Initialize Claude analyzer if API key is available
anthropic_key = os.getenv("ANTHROPIC_API_KEY")
if anthropic_key:
    claude_analyzer = ClaudeVisionAnalyzer(anthropic_key)

jarvis_vision = JARVISVisionIntegration(vision_system)

# Initialize vision decision pipeline
vision_pipeline = VisionDecisionPipeline()

class VisionCommand(BaseModel):
    """Vision command request model"""
    command: str
    use_claude: bool = True
    region: Optional[Tuple[int, int, int, int]] = None  # (x, y, width, height)

class ScreenAnalysisRequest(BaseModel):
    """Screen analysis request model"""
    analysis_type: str  # "updates", "activity", "security", "text"
    prompt: Optional[str] = None
    region: Optional[Tuple[int, int, int, int]] = None

class UpdateMonitoringRequest(BaseModel):
    """Update monitoring configuration"""
    enabled: bool
    interval: int = 300  # seconds
    notify_critical_only: bool = False

# Global monitoring state
monitoring_config = {
    "active": False,
    "interval": 300,
    "last_check": None,
    "pending_updates": []
}

# WebSocket connection manager
class VisionWebSocketManager:
    """Manages WebSocket connections for real-time vision updates"""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.enhanced_monitor = EnhancedWorkspaceMonitor()
        self.workspace_analyzer = WorkspaceAnalyzer()
        self.window_detector = WindowDetector()
        self.behavior_manager = AutonomousBehaviorManager()
        self.action_queue = ActionQueueManager()
        self.monitoring_active = False
        self.monitoring_task = None
        self.update_interval = 2.0  # 2-second updates
        
        # Set up action queue callbacks
        self.action_queue.add_execution_callback(self._handle_action_execution)
        
    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"Vision WebSocket connected. Total connections: {len(self.active_connections)}")
        
        # Send initial state
        await self.send_initial_state(websocket)
        
        # Start monitoring if not already active
        if not self.monitoring_active and self.active_connections:
            await self.start_monitoring()
            
        # Start vision pipeline if not running
        if not vision_pipeline.is_running:
            await vision_pipeline.start_pipeline()
            
    def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection"""
        self.active_connections.discard(websocket)
        logger.info(f"Vision WebSocket disconnected. Total connections: {len(self.active_connections)}")
        
        # Stop monitoring if no connections
        if not self.active_connections and self.monitoring_active:
            self.stop_monitoring()
            
        # Stop vision pipeline if no connections
        if not self.active_connections and vision_pipeline.is_running:
            asyncio.create_task(vision_pipeline.stop_pipeline())
            
    async def send_initial_state(self, websocket: WebSocket):
        """Send initial workspace state to new connection"""
        try:
            # Get current workspace state
            windows = self.window_detector.get_all_windows()
            workspace_analysis = await self.workspace_analyzer.analyze_workspace()
            
            initial_state = {
                "type": "initial_state",
                "timestamp": datetime.now().isoformat(),
                "workspace": {
                    "window_count": len(windows),
                    "focused_app": next((w.app_name for w in windows if w.is_focused), None),
                    "notifications": workspace_analysis.important_notifications,
                    "context": workspace_analysis.workspace_context,
                    "confidence": workspace_analysis.confidence
                },
                "monitoring_active": self.monitoring_active,
                "update_interval": self.update_interval
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
                logger.error(f"Error broadcasting to client: {e}")
                disconnected.add(connection)
                
        # Clean up disconnected clients
        for conn in disconnected:
            self.disconnect(conn)
            
    async def start_monitoring(self):
        """Start continuous workspace monitoring"""
        if self.monitoring_active:
            return
            
        self.monitoring_active = True
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info("Started workspace monitoring")
        
    def stop_monitoring(self):
        """Stop workspace monitoring"""
        self.monitoring_active = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
        logger.info("Stopped workspace monitoring")
        
    async def _monitoring_loop(self):
        """Main monitoring loop"""
        while self.monitoring_active and self.active_connections:
            try:
                # Get enhanced workspace state
                enhanced_state = await self.enhanced_monitor.get_complete_workspace_state()
                
                # Extract components
                windows = enhanced_state['windows']
                workspace_analysis = enhanced_state['analysis']
                notifications = enhanced_state['notifications']
                ui_elements = enhanced_state['ui_elements']
                
                # Build workspace state for behavior manager
                workspace_state = {
                    "windows": windows,
                    "analysis": workspace_analysis,
                    "window_count": len(windows),
                    "timestamp": datetime.now(),
                    "notifications": notifications,
                    "ui_elements": ui_elements
                }
                
                # Get autonomous actions
                autonomous_actions = []
                if workspace_analysis.confidence > 0.5:
                    autonomous_actions = await self.behavior_manager.process_workspace_state(
                        workspace_state,
                        windows
                    )
                    
                    # Add high-confidence actions to queue
                    if autonomous_actions:
                        high_confidence = [a for a in autonomous_actions if a.confidence > 0.7]
                        await self.action_queue.add_actions(high_confidence)
                
                # Prepare enhanced update message
                update_message = {
                    "type": "workspace_update",
                    "timestamp": datetime.now().isoformat(),
                    "workspace": {
                        "window_count": len(windows),
                        "windows": [
                            {
                                "id": w.window_id,
                                "app": w.app_name,
                                "title": w.window_title,
                                "focused": w.is_focused,
                                "visible": w.is_visible
                            } for w in windows[:10]  # Limit to 10 windows
                        ],
                        "focused_task": workspace_analysis.focused_task,
                        "context": workspace_analysis.workspace_context,
                        "notifications": workspace_analysis.important_notifications,
                        "suggestions": workspace_analysis.suggestions,
                        "notification_details": {
                            "badges": len(notifications.get('badges', [])),
                            "messages": len(notifications.get('messages', [])),
                            "meetings": len(notifications.get('meetings', [])),
                            "alerts": len(notifications.get('alerts', []))
                        },
                        "actionable_items": len(ui_elements)
                    },
                    "autonomous_actions": [
                        {
                            "type": action.action_type,
                            "target": action.target,
                            "priority": action.priority.name,
                            "confidence": action.confidence,
                            "reasoning": action.reasoning,
                            "requires_permission": action.requires_permission
                        } for action in autonomous_actions[:5]  # Limit to 5 actions
                    ],
                    "enhanced_data": {
                        "state_changes": enhanced_state['state_changes'][:3],  # Recent changes
                        "ui_elements": ui_elements[:3],  # Top UI elements
                        "clipboard_active": enhanced_state['clipboard'] is not None
                    },
                    "queue_status": self.action_queue.get_queue_state()
                }
                
                # Broadcast update
                await self.broadcast(update_message)
                
                # Wait before next update
                await asyncio.sleep(self.update_interval)
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(self.update_interval)
                
        logger.info("Monitoring loop ended")
        
    async def _handle_action_execution(self, result):
        """Handle action execution results"""
        # Broadcast execution result to all clients
        await self.broadcast({
            "type": "action_executed",
            "timestamp": datetime.now().isoformat(),
            "action": {
                "type": result.action.action_type,
                "target": result.action.target,
                "status": result.status.value,
                "execution_time": result.execution_time
            },
            "success": result.status == ExecutionStatus.SUCCESS,
            "error": result.error
        })

# Initialize WebSocket manager
ws_manager = VisionWebSocketManager()

@router.get("/status")
async def get_vision_status() -> Dict[str, Any]:
    """Get current vision system status"""
    pipeline_status = vision_pipeline.get_pipeline_status()
    
    return {
        "vision_enabled": True,
        "claude_vision_available": claude_analyzer is not None,
        "monitoring_active": monitoring_config["active"],
        "pipeline_active": pipeline_status["is_running"],
        "pipeline_cycles": pipeline_status["cycle_count"],
        "last_scan": vision_system.last_scan_time.isoformat() if vision_system.last_scan_time else None,
        "detected_updates": len(vision_system.detected_updates),
        "capabilities": [
            "screen_capture",
            "text_extraction",
            "update_detection",
            "application_detection",
            "ui_element_detection",
            "autonomous_decision_pipeline",
            "claude_vision_analysis" if claude_analyzer else None
        ],
        "pipeline_performance": pipeline_status.get("performance", {})
    }

@router.post("/command")
@graceful_endpoint
async def process_vision_command(request: VisionCommand) -> Dict[str, Any]:
    """Process a vision-related voice command"""
    try:
        # First try JARVIS integration
        response = await jarvis_vision.handle_vision_command(request.command)
        
        # If Claude is requested and available, enhance the response
        if request.use_claude and claude_analyzer and "what" in request.command.lower():
            # Capture screen for Claude analysis
            region = request.region
            screenshot = await vision_system.capture_screen(region)

            # v257.0: Skip Claude enrichment if capture failed (don't crash, still return base response)
            if screenshot is not None:
                claude_result = await claude_analyzer.understand_user_activity(screenshot)
                response = f"{response} Additionally, {claude_result.get('description', '')}"
        
        return {
            "success": True,
            "response": response,
            "timestamp": datetime.now().isoformat()
        }
    
    except Exception as e:
        raise  # Graceful handler will catch this

@router.post("/analyze")
@graceful_endpoint
async def analyze_screen(request: ScreenAnalysisRequest) -> Dict[str, Any]:
    """Perform detailed screen analysis"""
    try:
        region = request.region
        screenshot = await vision_system.capture_screen(region)
        # v257.0: Early return if capture failed
        if screenshot is None:
            return {"success": False, "error": "Screen capture failed", "timestamp": datetime.now().isoformat()}

        result = {}
        
        if request.analysis_type == "updates":
            # Check for software updates
            if claude_analyzer:
                result = await claude_analyzer.check_for_software_updates(screenshot)
            else:
                updates = await vision_system.scan_for_updates()
                result = {
                    "updates_found": len(updates) > 0,
                    "update_details": [
                        {
                            "type": u.update_type.value,
                            "name": u.application,
                            "version": u.version,
                            "urgency": u.urgency,
                            "description": u.description
                        }
                        for u in updates
                    ]
                }
        
        elif request.analysis_type == "activity" and claude_analyzer:
            # Understand user activity
            result = await claude_analyzer.understand_user_activity(screenshot)
        
        elif request.analysis_type == "security" and claude_analyzer:
            # Security check
            result = await claude_analyzer.security_check(screenshot)
        
        elif request.analysis_type == "text":
            # Extract text
            if claude_analyzer and request.prompt:
                text = await claude_analyzer.read_text_content(screenshot, request.prompt)
                result = {"extracted_text": text}
            else:
                elements = await vision_system.detect_text_regions(screenshot)
                result = {
                    "text_elements": [
                        {
                            "text": elem.text,
                            "location": elem.location,
                            "confidence": elem.confidence
                        }
                        for elem in elements
                    ]
                }
        
        else:
            # General context
            result = await vision_system.get_screen_context(region)
        
        return {
            "success": True,
            "analysis_type": request.analysis_type,
            "result": result,
            "timestamp": datetime.now().isoformat()
        }
    
    except Exception as e:
        raise  # Graceful handler will catch this

@router.post("/monitor/updates")
async def configure_update_monitoring(
    request: UpdateMonitoringRequest,
    background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """Configure automatic update monitoring"""
    try:
        monitoring_config["active"] = request.enabled
        monitoring_config["interval"] = request.interval
        
        if request.enabled and not jarvis_vision.monitoring_active:
            # Start monitoring in background
            jarvis_vision.monitoring_active = True
            background_tasks.add_task(monitor_updates_task)
            
            return {
                "success": True,
                "message": "Update monitoring activated",
                "config": monitoring_config
            }
        
        elif not request.enabled:
            jarvis_vision.monitoring_active = False
            monitoring_config["active"] = False
            
            return {
                "success": True,
                "message": "Update monitoring deactivated",
                "config": monitoring_config
            }
        
        else:
            return {
                "success": True,
                "message": "Monitoring already active",
                "config": monitoring_config
            }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def monitor_updates_task():
    """Background task for monitoring updates"""
    while monitoring_config["active"]:
        try:
            # Scan for updates
            updates = await vision_system.scan_for_updates()
            
            if updates:
                # Store in config
                monitoring_config["pending_updates"] = [
                    {
                        "type": u.update_type.value,
                        "app": u.application,
                        "urgency": u.urgency,
                        "description": u.description,
                        "detected_at": u.detected_at.isoformat()
                    }
                    for u in updates
                ]
                monitoring_config["last_check"] = datetime.now().isoformat()
                
                # Here you would trigger JARVIS to speak about critical updates
                critical = [u for u in updates if u.urgency == "critical"]
                if critical:
                    print(f"JARVIS: Sir, {len(critical)} critical updates require your attention.")
            
        except Exception as e:
            print(f"Error in update monitoring: {e}")
        
        await asyncio.sleep(monitoring_config["interval"])

@router.get("/updates/pending")
async def get_pending_updates() -> Dict[str, Any]:
    """Get list of pending updates detected"""
    return {
        "pending_updates": monitoring_config["pending_updates"],
        "last_check": monitoring_config["last_check"],
        "monitoring_active": monitoring_config["active"]
    }

@router.post("/capture")
@graceful_endpoint
async def capture_screenshot() -> Dict[str, Any]:
    """Capture and describe current screen"""
    try:
        description = await vision_system.capture_and_describe()
        
        # If Claude is available, get more detailed analysis
        if claude_analyzer:
            screenshot = await vision_system.capture_screen()
            # v257.0: Skip enrichment if capture failed
            if screenshot is not None:
                claude_result = await claude_analyzer.understand_user_activity(screenshot)
                description += f" {claude_result.get('description', '')}"
        
        return {
            "success": True,
            "description": description,
            "timestamp": datetime.now().isoformat()
        }
    
    except Exception as e:
        raise  # Graceful handler will catch this

@router.get("/capabilities")
async def get_vision_capabilities() -> Dict[str, List[str]]:
    """Get detailed vision system capabilities"""
    return {
        "vision_commands": [
            "What's on my screen?",
            "Check for software updates",
            "Start monitoring for updates",
            "Stop monitoring",
            "Analyze my screen",
            "What applications are open?",
            "Read the text in [area]",
            "Is there anything I should update?"
        ],
        "analysis_types": [
            "updates - Check for software updates",
            "activity - Understand current user activity",
            "security - Check for security concerns",
            "text - Extract text from screen"
        ],
        "features": [
            "Real-time screen capture",
            "OCR text extraction",
            "Software update detection",
            "Application identification",
            "UI element detection",
            "Notification badge detection",
            "Autonomous decision pipeline",
            "Claude vision integration" if claude_analyzer else "Claude vision (requires API key)"
        ]
    }

@router.post("/pipeline/control")
@graceful_endpoint
async def control_vision_pipeline(action: str) -> Dict[str, Any]:
    """Control the vision decision pipeline"""
    try:
        if action == "start":
            if not vision_pipeline.is_running:
                await vision_pipeline.start_pipeline()
                return {
                    "success": True,
                    "message": "Vision pipeline started",
                    "status": vision_pipeline.get_pipeline_status()
                }
            else:
                return {
                    "success": False,
                    "message": "Pipeline already running",
                    "status": vision_pipeline.get_pipeline_status()
                }
                
        elif action == "stop":
            if vision_pipeline.is_running:
                await vision_pipeline.stop_pipeline()
                return {
                    "success": True,
                    "message": "Vision pipeline stopped",
                    "status": vision_pipeline.get_pipeline_status()
                }
            else:
                return {
                    "success": False,
                    "message": "Pipeline not running",
                    "status": vision_pipeline.get_pipeline_status()
                }
                
        else:
            return {
                "success": False,
                "message": f"Unknown action: {action}",
                "valid_actions": ["start", "stop"]
            }
            
    except Exception as e:
        raise  # Graceful handler will catch this

@router.get("/pipeline/status")
async def get_pipeline_status() -> Dict[str, Any]:
    """Get detailed vision pipeline status"""
    return vision_pipeline.get_pipeline_status()

@router.get("/monitoring/report")
async def get_monitoring_report() -> Dict[str, Any]:
    """Get comprehensive monitoring report"""
    status = vision_pipeline.get_pipeline_status()
    if 'monitoring_report' in status:
        return status['monitoring_report']
    return {
        "error": "Monitoring data not available",
        "message": "Pipeline may not be running"
    }

@router.get("/monitoring/health")
async def get_system_health() -> Dict[str, Any]:
    """Get system health status"""
    status = vision_pipeline.get_pipeline_status()
    
    # Extract health information
    system_state = status.get('system_state', {})
    monitoring = status.get('monitoring_report', {})
    
    return {
        "healthy": system_state.get('healthy_components', 0) > 0,
        "system_health": monitoring.get('current_values', {}).get('system_health', 0),
        "component_health": monitoring.get('component_health', {}),
        "uptime": monitoring.get('uptime', {}),
        "alerts": monitoring.get('alerts', [])
    }

@router.post("/handler")
async def websocket_vision_handler(request_data: Dict[str, Any]) -> Dict[str, Any]:
    """HTTP handler for WebSocket bridge - routes to unified vision handler"""
    try:
        # Extract the message from the request
        message = request_data.get('args', [{}])[0] if request_data.get('args') else {}
        kwargs = request_data.get('kwargs', {})
        
        # Call the unified vision handler
        result = await unified_handle_vision_command(message, **kwargs)
        
        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        logger.error(f"Vision handler error: {e}")
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc() if hasattr(e, '__traceback__') else None
        }

@router.websocket("/ws/vision")
async def vision_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time vision updates"""
    await ws_manager.connect(websocket)
    
    # WebSocket idle timeout protection
    idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

    try:
        while True:
            # Receive messages from client with timeout
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("Vision WebSocket idle timeout, closing connection")
                break

            # Handle different message types
            if data.get("type") == "set_interval":
                # Update monitoring interval
                new_interval = data.get("interval", 2.0)
                ws_manager.update_interval = max(0.5, min(10.0, new_interval))  # Clamp between 0.5-10s
                await websocket.send_json({
                    "type": "config_updated",
                    "update_interval": ws_manager.update_interval
                })
                
            elif data.get("type") == "request_analysis":
                # Immediate workspace analysis
                windows = ws_manager.window_detector.get_all_windows()
                analysis = await ws_manager.workspace_analyzer.analyze_workspace()
                
                await websocket.send_json({
                    "type": "workspace_analysis",
                    "timestamp": datetime.now().isoformat(),
                    "analysis": {
                        "focused_task": analysis.focused_task,
                        "context": analysis.workspace_context,
                        "notifications": analysis.important_notifications,
                        "suggestions": analysis.suggestions,
                        "confidence": analysis.confidence
                    }
                })
                
            elif data.get("type") == "execute_action":
                # Execute autonomous action (with permission check)
                action_data = data.get("action", {})
                # Here you would integrate with action executor
                await websocket.send_json({
                    "type": "action_result",
                    "success": True,
                    "action": action_data,
                    "message": "Action execution would happen here"
                })
                
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)


# Multi-Monitor Support Endpoints

@router.get("/displays")
@graceful_endpoint(fallback_response={"success": False, "error": "Multi-monitor support unavailable"})
async def get_displays():
    """
    Get information about all connected displays
    
    Returns:
        JSON with display information, resolutions, positions, and space mappings
    """
    try:
        summary = await multi_monitor_detector.get_display_summary()
        
        return {
            "success": True,
            "displays": summary.get("displays", []),
            "total_displays": summary.get("total_displays", 0),
            "space_mappings": summary.get("space_mappings", {}),
            "detection_time": summary.get("detection_time", 0),
            "capture_stats": summary.get("capture_stats", {})
        }
        
    except Exception as e:
        logger.error(f"Error getting displays: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get display information: {str(e)}")


@router.post("/displays/capture")
@graceful_endpoint(fallback_response={"success": False, "error": "Multi-monitor capture unavailable"})
async def capture_all_displays():
    """
    Capture screenshots from all connected displays
    
    Returns:
        JSON with capture results and metadata
    """
    try:
        result = await multi_monitor_detector.capture_all_displays()
        
        # Convert numpy arrays to base64 for JSON serialization
        displays_captured = {}
        for display_id, screenshot in result.displays_captured.items():
            # For now, just return metadata about the screenshot
            # In a full implementation, you'd encode the image data
            displays_captured[display_id] = {
                "shape": screenshot.shape,
                "dtype": str(screenshot.dtype),
                "size_bytes": screenshot.nbytes,
                "captured": True
            }
        
        return {
            "success": result.success,
            "displays_captured": displays_captured,
            "failed_displays": result.failed_displays,
            "capture_time": result.capture_time,
            "total_displays": result.total_displays,
            "error": result.error,
            "metadata": result.metadata
        }
        
    except Exception as e:
        logger.error(f"Error capturing displays: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to capture displays: {str(e)}")


@router.get("/displays/{display_id}")
@graceful_endpoint(fallback_response={"success": False, "error": "Display information unavailable"})
async def get_display_info(display_id: int):
    """
    Get detailed information about a specific display
    
    Args:
        display_id: ID of the display to query
        
    Returns:
        JSON with detailed display information
    """
    try:
        displays = await multi_monitor_detector.detect_displays()
        
        # Find the requested display
        display_info = None
        for display in displays:
            if display.display_id == display_id:
                display_info = display
                break
        
        if not display_info:
            raise HTTPException(status_code=404, detail=f"Display {display_id} not found")
        
        # Get space mappings for this display
        space_mappings = await multi_monitor_detector.get_space_display_mapping()
        display_spaces = [space_id for space_id, mapping in space_mappings.items() 
                         if mapping.display_id == display_id]
        
        return {
            "success": True,
            "display": {
                "id": display_info.display_id,
                "name": display_info.name,
                "resolution": display_info.resolution,
                "position": display_info.position,
                "is_primary": display_info.is_primary,
                "refresh_rate": display_info.refresh_rate,
                "color_depth": display_info.color_depth,
                "spaces": display_spaces,
                "active_space": display_info.active_space,
                "last_updated": display_info.last_updated
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting display {display_id} info: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get display information: {str(e)}")


@router.get("/displays/performance")
@graceful_endpoint(fallback_response={"success": False, "error": "Performance stats unavailable"})
async def get_monitor_performance():
    """
    Get performance statistics for multi-monitor operations
    
    Returns:
        JSON with performance metrics and statistics
    """
    try:
        stats = multi_monitor_detector.get_performance_stats()
        
        return {
            "success": True,
            "performance": {
                "capture_stats": stats["capture_stats"],
                "cache_info": {
                    "displays_cached": stats["displays_cached"],
                    "space_mappings_cached": stats["space_mappings_cached"],
                    "last_detection_time": stats["last_detection_time"],
                    "cache_age_seconds": stats["cache_age"]
                },
                "system_info": {
                    "macos_available": MACOS_AVAILABLE,
                    "yabai_path": multi_monitor_detector.yabai_path
                }
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting performance stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get performance stats: {str(e)}")


@router.post("/displays/refresh")
@graceful_endpoint(fallback_response={"success": False, "error": "Display refresh unavailable"})
async def refresh_display_info():
    """
    Force refresh of display detection and space mappings
    
    Returns:
        JSON with updated display information
    """
    try:
        # Force refresh both displays and mappings
        displays = await multi_monitor_detector.detect_displays(force_refresh=True)
        space_mappings = await multi_monitor_detector.get_space_display_mapping(force_refresh=True)
        
        return {
            "success": True,
            "message": "Display information refreshed",
            "displays": [
                {
                    "id": d.display_id,
                    "name": d.name,
                    "resolution": d.resolution,
                    "position": d.position,
                    "is_primary": d.is_primary
                } for d in displays
            ],
            "space_mappings": space_mappings,
            "total_displays": len(displays),
            "total_spaces": len(space_mappings)
        }
        
    except Exception as e:
        logger.error(f"Error refreshing display info: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to refresh display information: {str(e)}")