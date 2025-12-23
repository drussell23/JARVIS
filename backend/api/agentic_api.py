"""
JARVIS Agentic API

REST API endpoints for autonomous task execution using Computer Use.

Endpoints:
- POST /api/agentic/execute - Execute a single goal
- POST /api/agentic/workflow - Execute a multi-step workflow
- GET /api/agentic/status - Get agentic system status
- GET /api/agentic/metrics - Get execution metrics
- WebSocket /ws/agentic - Real-time task execution updates

Usage:
    curl -X POST http://localhost:8000/api/agentic/execute \
        -H "Content-Type: application/json" \
        -d '{"goal": "Open Safari and find the weather"}'
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agentic", tags=["agentic"])


# ============================================================================
# Request/Response Models
# ============================================================================

class ExecuteGoalRequest(BaseModel):
    """Request to execute a single goal."""
    goal: str = Field(..., description="Natural language goal to achieve")
    mode: str = Field("supervised", description="Execution mode: autonomous, supervised, direct")
    context: Optional[Dict[str, Any]] = Field(None, description="Additional context")
    narrate: bool = Field(True, description="Enable voice narration")
    timeout_seconds: float = Field(300.0, description="Maximum execution time")


class ExecuteGoalResponse(BaseModel):
    """Response from goal execution."""
    success: bool
    task_id: str
    goal: str
    mode: str
    status: str
    final_message: str
    actions_count: int
    execution_time_ms: float
    learning_insights: List[str] = []
    error: Optional[str] = None


class WorkflowRequest(BaseModel):
    """Request to execute a multi-step workflow."""
    goal: str = Field(..., description="High-level goal")
    steps: Optional[List[str]] = Field(None, description="Optional step descriptions")
    context: Optional[Dict[str, Any]] = Field(None, description="Additional context")
    parallel: bool = Field(False, description="Execute independent steps in parallel")
    narrate: bool = Field(True, description="Enable voice narration")


class WorkflowResponse(BaseModel):
    """Response from workflow execution."""
    success: bool
    workflow_id: str
    goal: str
    steps_completed: int
    steps_total: int
    total_duration_ms: float
    steps_results: List[Dict[str, Any]]
    learning_insights: List[str] = []
    final_message: str
    error: Optional[str] = None


class AgenticStatusResponse(BaseModel):
    """Agentic system status."""
    initialized: bool
    computer_use_available: bool
    uae_routing_enabled: bool
    neural_mesh_agent: bool
    workflow_executor: bool
    config_available: bool
    uptime_seconds: Optional[float] = None


class AgenticMetricsResponse(BaseModel):
    """Agentic system metrics."""
    total_goals: int = 0
    successful_goals: int = 0
    failed_goals: int = 0
    success_rate: float = 0.0
    total_workflows: int = 0
    successful_workflows: int = 0
    average_execution_time_ms: float = 0.0
    computer_use_metrics: Optional[Dict[str, Any]] = None


# ============================================================================
# Helper Functions
# ============================================================================

def get_agentic_system(request: Request) -> Optional[Dict[str, Any]]:
    """Get the agentic system from app state."""
    return getattr(request.app.state, 'agentic_system', None)


def get_computer_use_tool(request: Request):
    """Get the computer use tool from app state."""
    return getattr(request.app.state, 'computer_use_tool', None)


def get_uae_engine(request: Request):
    """Get the UAE engine from app state."""
    return getattr(request.app.state, 'uae_engine', None)


def get_workflow_executor(request: Request):
    """Get the workflow executor from app state."""
    return getattr(request.app.state, 'agentic_workflow_executor', None)


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/status", response_model=AgenticStatusResponse)
async def get_agentic_status(request: Request) -> AgenticStatusResponse:
    """
    Get the current status of the agentic system.

    Returns information about available components and capabilities.
    """
    agentic_system = get_agentic_system(request)

    if not agentic_system:
        return AgenticStatusResponse(
            initialized=False,
            computer_use_available=False,
            uae_routing_enabled=False,
            neural_mesh_agent=False,
            workflow_executor=False,
            config_available=False,
        )

    uptime = None
    if agentic_system.get("timestamp"):
        uptime = time.time() - agentic_system["timestamp"]

    return AgenticStatusResponse(
        initialized=agentic_system.get("initialized", False),
        computer_use_available=agentic_system.get("computer_use_available", False),
        uae_routing_enabled=agentic_system.get("uae_routing_enabled", False),
        neural_mesh_agent=agentic_system.get("neural_mesh_agent", False),
        workflow_executor=agentic_system.get("workflow_executor", False),
        config_available=agentic_system.get("config_available", False),
        uptime_seconds=uptime,
    )


@router.get("/metrics", response_model=AgenticMetricsResponse)
async def get_agentic_metrics(request: Request) -> AgenticMetricsResponse:
    """
    Get execution metrics for the agentic system.
    """
    computer_use_tool = get_computer_use_tool(request)
    workflow_executor = get_workflow_executor(request)

    metrics = AgenticMetricsResponse()

    if computer_use_tool:
        tool_metrics = computer_use_tool.get_metrics()
        metrics.total_goals = tool_metrics.get("total_goals", 0)
        metrics.successful_goals = tool_metrics.get("successful_goals", 0)
        metrics.failed_goals = tool_metrics.get("failed_goals", 0)
        metrics.success_rate = tool_metrics.get("success_rate", 0.0)
        metrics.computer_use_metrics = tool_metrics

    if workflow_executor:
        executor_metrics = workflow_executor.get_metrics()
        metrics.total_workflows = executor_metrics.get("workflows_executed", 0)
        metrics.successful_workflows = executor_metrics.get("workflows_succeeded", 0)

    return metrics


@router.post("/execute", response_model=ExecuteGoalResponse)
async def execute_goal(request: Request, body: ExecuteGoalRequest) -> ExecuteGoalResponse:
    """
    Execute a single goal using Computer Use.

    This endpoint uses vision-based UI automation to achieve the specified goal.
    """
    task_id = str(uuid4())
    start_time = time.time()

    logger.info(f"[AgenticAPI] Executing goal: {body.goal}")

    # Check if agentic system is available
    agentic_system = get_agentic_system(request)
    if not agentic_system or not agentic_system.get("initialized"):
        raise HTTPException(
            status_code=503,
            detail="Agentic system not initialized. Please wait for startup to complete."
        )

    # Prefer UAE routing if available
    uae_engine = get_uae_engine(request)
    computer_use_tool = get_computer_use_tool(request)

    if not computer_use_tool and not uae_engine:
        raise HTTPException(
            status_code=503,
            detail="No execution backend available. Computer Use or UAE required."
        )

    try:
        # Execute via UAE if routing is enabled
        if uae_engine and agentic_system.get("uae_routing_enabled"):
            result = await asyncio.wait_for(
                uae_engine.route_to_computer_use(
                    goal=body.goal,
                    context=body.context,
                    narrate=body.narrate,
                ),
                timeout=body.timeout_seconds
            )
        elif computer_use_tool:
            # Direct Computer Use execution
            result = await asyncio.wait_for(
                computer_use_tool.run(
                    goal=body.goal,
                    context=body.context,
                    narrate=body.narrate,
                ),
                timeout=body.timeout_seconds
            )
            # Convert ComputerUseResult to dict
            result = {
                "success": result.success,
                "status": result.status,
                "final_message": result.final_message,
                "actions_count": result.actions_count,
                "duration_ms": result.total_duration_ms,
                "learning_insights": result.learning_insights,
            }
        else:
            raise HTTPException(status_code=503, detail="No execution backend available")

        execution_time = (time.time() - start_time) * 1000

        return ExecuteGoalResponse(
            success=result.get("success", False),
            task_id=task_id,
            goal=body.goal,
            mode=body.mode,
            status=result.get("status", "unknown"),
            final_message=result.get("final_message", ""),
            actions_count=result.get("actions_count", 0),
            execution_time_ms=execution_time,
            learning_insights=result.get("learning_insights", []),
            error=result.get("error"),
        )

    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=408,
            detail=f"Goal execution timed out after {body.timeout_seconds}s"
        )
    except Exception as e:
        logger.error(f"[AgenticAPI] Goal execution failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Goal execution failed: {str(e)}"
        )


@router.post("/workflow", response_model=WorkflowResponse)
async def execute_workflow(request: Request, body: WorkflowRequest) -> WorkflowResponse:
    """
    Execute a multi-step workflow.

    This endpoint breaks down complex goals into steps and executes them
    using Computer Use, with optional parallel execution.
    """
    workflow_id = str(uuid4())
    start_time = time.time()

    logger.info(f"[AgenticAPI] Executing workflow: {body.goal}")

    # Check if workflow executor is available
    workflow_executor = get_workflow_executor(request)
    if not workflow_executor:
        raise HTTPException(
            status_code=503,
            detail="Workflow executor not available. Check agentic system status."
        )

    try:
        result = await workflow_executor.execute_workflow(
            goal=body.goal,
            steps=body.steps,
            context=body.context,
            parallel=body.parallel,
            narrate=body.narrate,
        )

        return WorkflowResponse(
            success=result.success,
            workflow_id=result.workflow_id,
            goal=result.goal,
            steps_completed=result.steps_completed,
            steps_total=result.steps_total,
            total_duration_ms=result.total_duration_ms,
            steps_results=result.steps_results,
            learning_insights=result.learning_insights,
            final_message=result.final_message,
            error=result.error,
        )

    except Exception as e:
        logger.error(f"[AgenticAPI] Workflow execution failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Workflow execution failed: {str(e)}"
        )


@router.get("/health")
async def agentic_health(request: Request) -> Dict[str, Any]:
    """
    Health check for the agentic system.
    """
    agentic_system = get_agentic_system(request)

    if not agentic_system:
        return {
            "status": "unavailable",
            "message": "Agentic system not initialized"
        }

    if not agentic_system.get("initialized"):
        return {
            "status": "initializing",
            "message": "Agentic system is starting up"
        }

    if agentic_system.get("error"):
        return {
            "status": "error",
            "message": agentic_system.get("error")
        }

    return {
        "status": "healthy",
        "message": "Agentic system operational",
        "capabilities": {
            "computer_use": agentic_system.get("computer_use_available", False),
            "uae_routing": agentic_system.get("uae_routing_enabled", False),
            "neural_mesh": agentic_system.get("neural_mesh_agent", False),
            "workflows": agentic_system.get("workflow_executor", False),
        }
    }


# ============================================================================
# WebSocket for Real-time Updates
# ============================================================================

class AgenticWebSocketManager:
    """Manages WebSocket connections for real-time agentic updates."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass


ws_manager = AgenticWebSocketManager()


@router.websocket("/ws")
async def agentic_websocket(websocket: WebSocket, request: Request = None):
    """
    WebSocket endpoint for real-time agentic task updates.

    Messages sent to clients:
    - {"type": "status", "data": {...}} - System status updates
    - {"type": "task_started", "data": {...}} - Task started
    - {"type": "action_executed", "data": {...}} - Action completed
    - {"type": "task_completed", "data": {...}} - Task finished
    - {"type": "error", "data": {...}} - Error occurred
    """
    await ws_manager.connect(websocket)

    try:
        # Send initial status
        await websocket.send_json({
            "type": "connected",
            "message": "Connected to agentic WebSocket"
        })

        while True:
            # Wait for messages from client
            data = await websocket.receive_json()

            # Handle different message types
            message_type = data.get("type", "")

            if message_type == "ping":
                await websocket.send_json({"type": "pong", "timestamp": time.time()})

            elif message_type == "get_status":
                # Get current status
                status = {
                    "type": "status",
                    "data": {
                        "initialized": True,
                        "timestamp": time.time()
                    }
                }
                await websocket.send_json(status)

            elif message_type == "execute":
                # Execute goal via WebSocket
                goal = data.get("goal", "")
                if goal:
                    await websocket.send_json({
                        "type": "task_started",
                        "data": {"goal": goal, "timestamp": time.time()}
                    })

                    # Note: In production, this would trigger actual execution
                    # and stream updates back to the client

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)
