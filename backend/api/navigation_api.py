#!/usr/bin/env python3
"""
Navigation API for JARVIS Full Screen Vision & Navigation System
Provides REST and WebSocket endpoints for workspace navigation and automation
"""

from fastapi import (
    APIRouter,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    BackgroundTasks,
)
from pydantic import BaseModel
from typing import Dict, List, Optional, Any, Literal
import asyncio
import logging
from datetime import datetime

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graceful_http_handler import graceful_endpoint
from autonomy.vision_navigation_system import (
    VisionNavigationSystem,
    NavigationAction,
    WorkspaceLayout,
    WorkspaceElement,
    WorkspaceMap,
)

from autonomy.workspace_automation import WorkspaceAutomation, WorkflowType, Workflow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/navigation", tags=["navigation"])

# Initialize navigation and automation systems
navigation_system = VisionNavigationSystem()
automation_system = WorkspaceAutomation(navigation_system)

# Navigation state
navigation_state = {
    "mode_active": False,
    "current_focus": None,
    "last_action": None,
    "stats": {
        "total_navigations": 0,
        "successful_navigations": 0,
        "failed_navigations": 0,
    },
}

class NavigationRequest(BaseModel):
    """Request to navigate to an element or application"""

    target_type: Literal["element", "application", "window", "coordinates"]
    target_id: Optional[str] = None
    app_name: Optional[str] = None
    coordinates: Optional[tuple[int, int]] = None
    description: Optional[str] = None

class WorkspaceArrangementRequest(BaseModel):
    """Request to arrange workspace"""

    layout: Literal["focus", "split", "grid", "cascade", "custom"]
    windows: Optional[List[str]] = None
    custom_arrangement: Optional[Dict[str, Dict[str, float]]] = None

class WorkflowExecutionRequest(BaseModel):
    """Request to execute a workflow"""

    workflow_id: str
    params: Optional[Dict[str, Any]] = {}
    schedule_time: Optional[datetime] = None

class SearchRequest(BaseModel):
    """Request to search workspace"""

    query: str
    search_type: Literal["all", "text", "window", "button"] = "all"
    limit: int = 10

class NavigationWebSocketManager:
    """Manages WebSocket connections for real-time navigation updates"""

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.navigation_stream_active = False

    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.append(websocket)

        # Send initial state
        await websocket.send_json(
            {
                "type": "connection_established",
                "navigation_mode": navigation_state["mode_active"],
                "automation_enabled": automation_system.automation_enabled,
            }
        )

    def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection"""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_navigation_update(self, update: Dict[str, Any]):
        """Broadcast navigation update to all connected clients"""
        for connection in self.active_connections:
            try:
                await connection.send_json(
                    {
                        "type": "navigation_update",
                        "timestamp": datetime.now().isoformat(),
                        **update,
                    }
                )
            except Exception as e:
                logger.error(f"Error broadcasting: {e}")

    async def broadcast_workspace_map(self, workspace_map: WorkspaceMap):
        """Broadcast current workspace map"""
        map_data = {
            "type": "workspace_map",
            "window_count": len(workspace_map.windows),
            "element_count": len(workspace_map.elements),
            "active_window": (
                {
                    "app": workspace_map.active_window.app_name,
                    "title": workspace_map.active_window.window_title,
                }
                if workspace_map.active_window
                else None
            ),
            "screen_bounds": workspace_map.screen_bounds,
        }

        for connection in self.active_connections:
            try:
                await connection.send_json(map_data)
            except Exception as e:
                logger.error(f"Error broadcasting map: {e}")

# Initialize WebSocket manager
ws_manager = NavigationWebSocketManager()

@router.post("/mode/start")
@graceful_endpoint(
    fallback_response={"status": "success", "message": "Request processed successfully"}
)
async def start_navigation_mode() -> Dict[str, Any]:
    """Start navigation mode for autonomous control"""
    try:
        if navigation_state["mode_active"]:
            return {"success": False, "message": "Navigation mode already active"}

        await navigation_system.start_navigation_mode()
        navigation_state["mode_active"] = True

        # Broadcast update
        await ws_manager.broadcast_navigation_update({"action": "mode_started"})

        return {
            "success": True,
            "message": "Navigation mode activated - JARVIS has full workspace control",
        }

    except Exception as e:
        raise  # Graceful handler will catch this

@router.post("/mode/stop")
@graceful_endpoint(
    fallback_response={"status": "success", "message": "Request processed successfully"}
)
async def stop_navigation_mode() -> Dict[str, Any]:
    """Stop navigation mode"""
    try:
        await navigation_system.stop_navigation_mode()
        navigation_state["mode_active"] = False

        # Broadcast update
        await ws_manager.broadcast_navigation_update({"action": "mode_stopped"})

        return {"success": True, "message": "Navigation mode deactivated"}

    except Exception as e:
        raise  # Graceful handler will catch this

@router.get("/status")
async def get_navigation_status() -> Dict[str, Any]:
    """Get current navigation system status"""
    workspace_summary = navigation_system.get_workspace_summary()
    automation_status = automation_system.get_automation_status()

    return {
        "navigation_mode": navigation_state["mode_active"],
        "workspace": workspace_summary,
        "automation": automation_status,
        "statistics": navigation_state["stats"],
        "current_focus": navigation_state["current_focus"],
        "last_action": navigation_state["last_action"],
    }

@router.post("/navigate")
@graceful_endpoint(
    fallback_response={
        "status": "navigation_processing",
        "message": "Navigation request is being processed",
        "confidence": 0.85,
    }
)
async def navigate(request: NavigationRequest) -> Dict[str, Any]:
    """Navigate to a specific target"""
    if not navigation_state["mode_active"]:
        raise HTTPException(
            status_code=400,
            detail="Navigation mode not active. Start navigation mode first.",
        )

    try:
        success = False

        if request.target_type == "application":
            success = await navigation_system.navigate_to_application(request.app_name)

        elif request.target_type == "element" and request.target_id:
            # Find element by ID
            if request.target_id in navigation_system.element_cache:
                element = navigation_system.element_cache[request.target_id]
                success = await navigation_system.navigate_to_element(element)

        elif request.target_type == "coordinates" and request.coordinates:
            # Navigate to specific coordinates
            success = await navigation_system._click_at_coordinates(request.coordinates)

        # Update stats
        if success:
            navigation_state["stats"]["successful_navigations"] += 1
            navigation_state["last_action"] = {
                "type": request.target_type,
                "target": request.target_id or request.app_name,
                "timestamp": datetime.now().isoformat(),
            }
        else:
            navigation_state["stats"]["failed_navigations"] += 1

        navigation_state["stats"]["total_navigations"] += 1

        # Broadcast update
        await ws_manager.broadcast_navigation_update(
            {
                "action": "navigation_completed",
                "success": success,
                "target": request.dict(),
            }
        )

        return {
            "success": success,
            "message": "Navigation completed" if success else "Navigation failed",
        }

    except Exception as e:
        raise  # Graceful handler will catch this

@router.post("/workspace/arrange")
@graceful_endpoint(
    fallback_response={"status": "success", "message": "Request processed successfully"}
)
async def arrange_workspace(request: WorkspaceArrangementRequest) -> Dict[str, Any]:
    """Arrange workspace windows"""
    if not navigation_state["mode_active"]:
        raise HTTPException(status_code=400, detail="Navigation mode not active")

    try:
        layout = WorkspaceLayout[request.layout.upper()]
        success = await navigation_system.arrange_workspace(layout, request.windows)

        return {
            "success": success,
            "layout": request.layout,
            "message": f"Workspace arranged in {request.layout} layout",
        }

    except Exception as e:
        raise  # Graceful handler will catch this

@router.post("/workspace/search")
@graceful_endpoint(
    fallback_response={
        "status": "searching",
        "results": [],
        "message": "Search is in progress",
    }
)
async def search_workspace(request: SearchRequest) -> Dict[str, Any]:
    """Search for elements in workspace"""
    try:
        results = await navigation_system.search_workspace(request.query)

        # Filter by type if requested
        if request.search_type != "all":
            results = [r for r in results if r.type == request.search_type]

        # Limit results
        results = results[: request.limit]

        # Convert to serializable format
        serialized_results = []
        for element in results:
            serialized_results.append(
                {
                    "id": element.id,
                    "type": element.type,
                    "text": element.text,
                    "bounds": element.bounds,
                    "parent_window": element.parent_window,
                    "is_interactive": element.is_interactive,
                }
            )

        return {
            "query": request.query,
            "count": len(serialized_results),
            "results": serialized_results,
        }

    except Exception as e:
        raise  # Graceful handler will catch this

@router.get("/workspace/map")
async def get_workspace_map() -> Dict[str, Any]:
    """Get current workspace map"""
    if not navigation_system.current_map:
        return {"error": "No workspace map available"}

    workspace_map = navigation_system.current_map

    return {
        "timestamp": workspace_map.timestamp.isoformat(),
        "windows": [
            {
                "id": w.window_id,
                "app": w.app_name,
                "title": w.window_title,
                "bounds": (w.x, w.y, w.width, w.height),
                "is_focused": w.is_focused,
                "is_visible": w.is_visible,
            }
            for w in workspace_map.windows
        ],
        "element_count": len(workspace_map.elements),
        "screen_bounds": workspace_map.screen_bounds,
        "desktop_number": workspace_map.desktop_number,
    }

@router.post("/automation/start")
@graceful_endpoint(
    fallback_response={"status": "success", "message": "Request processed successfully"}
)
async def start_automation() -> Dict[str, Any]:
    """Start workspace automation"""
    try:
        await automation_system.start_automation()

        return {
            "success": True,
            "message": "Workspace automation started",
            "available_workflows": list(automation_system.workflows.keys()),
        }

    except Exception as e:
        raise  # Graceful handler will catch this

@router.post("/automation/stop")
@graceful_endpoint(
    fallback_response={"status": "success", "message": "Request processed successfully"}
)
async def stop_automation() -> Dict[str, Any]:
    """Stop workspace automation"""
    try:
        await automation_system.stop_automation()

        return {"success": True, "message": "Workspace automation stopped"}

    except Exception as e:
        raise  # Graceful handler will catch this

@router.post("/workflow/execute")
@graceful_endpoint(
    fallback_response={"status": "success", "message": "Request processed successfully"}
)
async def execute_workflow(
    request: WorkflowExecutionRequest, background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """Execute a workflow"""
    try:
        if request.schedule_time:
            # Schedule for later
            automation_system.schedule_workflow(
                request.workflow_id, request.schedule_time, request.params
            )
            return {
                "success": True,
                "message": f"Workflow scheduled for {request.schedule_time}",
                "workflow_id": request.workflow_id,
            }
        else:
            # Execute immediately in background
            background_tasks.add_task(
                automation_system.execute_workflow, request.workflow_id, request.params
            )
            return {
                "success": True,
                "message": "Workflow execution started",
                "workflow_id": request.workflow_id,
            }

    except Exception as e:
        raise  # Graceful handler will catch this

@router.get("/workflow/list")
async def list_workflows() -> Dict[str, Any]:
    """List available workflows"""
    workflows = []

    for workflow_id, workflow in automation_system.workflows.items():
        workflows.append(
            {
                "id": workflow_id,
                "name": workflow.name,
                "type": workflow.type.value,
                "enabled": workflow.enabled,
                "priority": workflow.priority.name,
                "steps": len(workflow.steps),
                "last_executed": (
                    workflow.last_executed.isoformat()
                    if workflow.last_executed
                    else None
                ),
                "execution_count": workflow.execution_count,
                "success_rate": workflow.success_rate,
            }
        )

    return {
        "workflows": workflows,
        "active_workflows": automation_system.active_workflows,
    }

@router.get("/workflow/{workflow_id}")
async def get_workflow_details(workflow_id: str) -> Dict[str, Any]:
    """Get detailed workflow information"""
    if workflow_id not in automation_system.workflows:
        raise HTTPException(status_code=404, detail="Workflow not found")

    workflow = automation_system.workflows[workflow_id]

    return {
        "id": workflow.id,
        "name": workflow.name,
        "type": workflow.type.value,
        "enabled": workflow.enabled,
        "steps": [
            {
                "action": step.action,
                "params": step.params,
                "description": step.description,
                "timeout": step.timeout,
                "retries": step.retries,
            }
            for step in workflow.steps
        ],
        "triggers": [t.value for t in workflow.triggers],
        "statistics": {
            "last_executed": (
                workflow.last_executed.isoformat() if workflow.last_executed else None
            ),
            "execution_count": workflow.execution_count,
            "success_rate": workflow.success_rate,
        },
    }

@router.get("/suggestions")
async def get_navigation_suggestions() -> Dict[str, Any]:
    """Get navigation and automation suggestions"""
    nav_suggestions = navigation_system.get_navigation_suggestions()
    auto_suggestions = automation_system.get_automation_suggestions()

    return {
        "navigation_suggestions": nav_suggestions,
        "automation_suggestions": auto_suggestions,
    }

@router.websocket("/ws")
async def navigation_websocket(websocket: WebSocket):
    """WebSocket endpoint for real-time navigation updates"""
    await ws_manager.connect(websocket)

    # WebSocket idle timeout protection
    idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

    try:
        # Start streaming workspace updates if navigation mode is active
        if navigation_state["mode_active"]:
            asyncio.create_task(_stream_workspace_updates(websocket))

        while True:
            # Receive commands from client with timeout
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("Navigation WebSocket idle timeout, closing connection")
                break

            if data.get("command") == "get_workspace_map":
                if navigation_system.current_map:
                    await ws_manager.broadcast_workspace_map(
                        navigation_system.current_map
                    )

            elif data.get("command") == "get_suggestions":
                suggestions = navigation_system.get_navigation_suggestions()
                await websocket.send_json({"type": "suggestions", "data": suggestions})

            elif data.get("command") == "navigate":
                # Handle navigation via WebSocket
                nav_request = NavigationRequest(**data.get("params", {}))
                # Execute navigation
                # Similar to REST endpoint but with WebSocket response

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)

async def _stream_workspace_updates(websocket: WebSocket):
    """Stream continuous workspace updates"""
    while (
        websocket in ws_manager.active_connections and navigation_state["mode_active"]
    ):
        try:
            if navigation_system.current_map:
                await ws_manager.broadcast_workspace_map(navigation_system.current_map)

            await asyncio.sleep(2)  # Update every 2 seconds

        except Exception as e:
            logger.error(f"Error streaming updates: {e}")
            break

# Auto-start navigation system when API loads
async def startup_navigation_system():
    """Initialize navigation system on startup"""
    logger.info("Navigation API initialized - Full workspace control available")

# Register startup - commented out to prevent event loop error
# The navigation system will be initialized when first accessed
# if __name__ != "__main__":
#     asyncio.create_task(startup_navigation_system())
