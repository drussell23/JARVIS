"""
JARVIS Unified Agentic API v2.0
================================

REST API endpoints for the unified Two-Tier Agentic Security System.
All agentic task execution flows through the AgenticTaskRunner.

Endpoints:
- POST /api/agentic/execute - Execute a single goal (Tier 2)
- POST /api/agentic/tier1 - Execute safe command (Tier 1)
- POST /api/agentic/route - Route command through TieredRouter
- GET /api/agentic/status - Get unified system status
- GET /api/agentic/metrics - Get execution metrics
- GET /api/agentic/watchdog - Get watchdog status
- WebSocket /ws/agentic - Real-time task execution updates

Architecture:
    CLI/API -> TieredRouter -> VBIA Auth -> AgenticRunner -> Computer Use
                            -> Watchdog (safety monitoring)

Usage:
    # Execute Tier 2 task
    curl -X POST http://localhost:8000/api/agentic/execute \\
        -H "Content-Type: application/json" \\
        -d '{"goal": "Open Safari and find the weather"}'

    # Route through tiered system
    curl -X POST http://localhost:8000/api/agentic/route \\
        -H "Content-Type: application/json" \\
        -d '{"command": "JARVIS ACCESS organize my desktop"}'
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.core.secure_logging import sanitize_for_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agentic", tags=["agentic"])


# ============================================================================
# Request/Response Models
# ============================================================================

class ExecuteGoalRequest(BaseModel):
    """Request to execute a single goal via AgenticTaskRunner."""
    goal: str = Field(..., description="Natural language goal to achieve")
    mode: str = Field("autonomous", description="Execution mode: autonomous, supervised, direct")
    context: Optional[Dict[str, Any]] = Field(None, description="Additional context")
    narrate: bool = Field(True, description="Enable voice narration")
    timeout_seconds: float = Field(300.0, description="Maximum execution time")
    # VBIA override for API clients (they can pass their own verification)
    vbia_confidence: Optional[float] = Field(None, description="Pre-verified VBIA confidence")
    speaker_id: Optional[str] = Field(None, description="Pre-verified speaker ID")


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
    reasoning_steps: int = 0
    learning_insights: List[str] = []
    watchdog_status: Optional[str] = None
    error: Optional[str] = None


class RouteCommandRequest(BaseModel):
    """Request to route a command through the TieredCommandRouter."""
    command: str = Field(..., description="Voice command with wake word")
    vbia_confidence: Optional[float] = Field(None, description="Pre-verified VBIA confidence")
    speaker_id: Optional[str] = Field(None, description="Pre-verified speaker ID")
    execute: bool = Field(True, description="Execute after routing or just return decision")


class RouteCommandResponse(BaseModel):
    """Response from command routing."""
    tier: str
    backend: str
    command: str
    auth_required: bool
    auth_result: Optional[str]
    vbia_confidence: Optional[float]
    watchdog_armed: bool
    execution_allowed: bool
    denial_reason: Optional[str]
    # Execution result (if execute=True)
    executed: bool = False
    execution_result: Optional[Dict[str, Any]] = None


class AgenticStatusResponse(BaseModel):
    """Unified agentic system status."""
    initialized: bool
    runner_ready: bool
    router_ready: bool
    watchdog_active: bool
    vbia_adapter_ready: bool
    # Component details
    components: Dict[str, bool]
    # Stats
    tasks_executed: int = 0
    tasks_succeeded: int = 0
    success_rate: float = 0.0
    # Watchdog
    watchdog_status: Optional[Dict[str, Any]] = None
    # Config
    tier1_backend: str = "gemini"
    tier2_backend: str = "claude"
    vbia_tier1_threshold: float = 0.70
    vbia_tier2_threshold: float = 0.85


class WatchdogStatusResponse(BaseModel):
    """Watchdog safety system status."""
    active: bool
    mode: str
    kill_switch_armed: bool
    heartbeat_healthy: bool
    agentic_allowed: bool
    uptime_seconds: float = 0.0
    active_task: Optional[str] = None
    consecutive_failures: int = 0
    last_activity: Optional[float] = None


# ============================================================================
# Helper Functions - Use module-level registries as primary source
# ============================================================================

def get_agentic_runner(request: Request):
    """
    Get the AgenticTaskRunner.

    Priority:
    1. Module-level registry (set by supervisor)
    2. app.state fallback (for compatibility)
    """
    try:
        from core.agentic_task_runner import get_agentic_runner as _get_runner
        runner = _get_runner()
        if runner is not None:
            return runner
    except ImportError:
        pass
    return getattr(request.app.state, 'agentic_runner', None)


def get_tiered_router(request: Request):
    """
    Get the TieredCommandRouter.

    Priority:
    1. Module-level registry (set by supervisor)
    2. app.state fallback (for compatibility)
    """
    try:
        from core.tiered_command_router import get_tiered_router as _get_router
        router = _get_router()
        if router is not None:
            return router
    except ImportError:
        pass
    return getattr(request.app.state, 'tiered_router', None)


def get_vbia_adapter(request: Request):
    """
    Get the TieredVBIAAdapter.

    Priority:
    1. Module-level registry (set by supervisor)
    2. app.state fallback (for compatibility)
    """
    try:
        from core.tiered_vbia_adapter import TieredVBIAAdapter
        # Use _adapter_instance directly (sync access, don't create new)
        from core.tiered_vbia_adapter import _adapter_instance
        if _adapter_instance is not None:
            return _adapter_instance
    except ImportError:
        pass
    return getattr(request.app.state, 'vbia_adapter', None)


def get_watchdog(request: Request):
    """
    Get the AgenticWatchdog.

    Priority:
    1. Module-level registry (set by supervisor)
    2. app.state fallback (for compatibility)
    """
    try:
        from core.agentic_watchdog import get_watchdog as _get_watchdog
        watchdog = _get_watchdog()
        if watchdog is not None:
            return watchdog
    except ImportError:
        pass
    return getattr(request.app.state, 'agentic_watchdog', None)


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/status", response_model=AgenticStatusResponse)
async def get_agentic_status(request: Request) -> AgenticStatusResponse:
    """
    Get the unified agentic system status.

    Returns information about all components: runner, router, watchdog, VBIA.
    """
    runner = get_agentic_runner(request)
    router_obj = get_tiered_router(request)
    watchdog = get_watchdog(request)
    vbia = get_vbia_adapter(request)

    # Get runner stats
    runner_stats = runner.get_stats() if runner else {}
    watchdog_status = None
    if watchdog:
        try:
            ws = watchdog.get_status()
            watchdog_status = {
                "mode": ws.mode.value if hasattr(ws.mode, 'value') else str(ws.mode),
                "kill_switch_armed": ws.kill_switch_armed,
                "heartbeat_healthy": ws.heartbeat_healthy,
                "uptime_seconds": ws.uptime_seconds,
            }
        except Exception:
            watchdog_status = {"active": True}

    # Get router config
    tier1_backend = "gemini"
    tier2_backend = "claude"
    tier1_threshold = 0.70
    tier2_threshold = 0.85
    if router_obj:
        tier1_backend = router_obj.config.tier1_backend
        tier2_backend = router_obj.config.tier2_backend
        tier1_threshold = router_obj.config.tier1_vbia_threshold
        tier2_threshold = router_obj.config.tier2_vbia_threshold

    return AgenticStatusResponse(
        initialized=runner is not None and (runner.is_ready if runner else False),
        runner_ready=runner is not None and (runner.is_ready if runner else False),
        router_ready=router_obj is not None,
        watchdog_active=watchdog is not None,
        vbia_adapter_ready=vbia is not None,
        components=runner_stats.get("components", {}) if runner_stats else {},
        tasks_executed=runner_stats.get("tasks_executed", 0),
        tasks_succeeded=runner_stats.get("tasks_succeeded", 0),
        success_rate=runner_stats.get("success_rate", 0.0),
        watchdog_status=watchdog_status,
        tier1_backend=tier1_backend,
        tier2_backend=tier2_backend,
        vbia_tier1_threshold=tier1_threshold,
        vbia_tier2_threshold=tier2_threshold,
    )


@router.get("/watchdog", response_model=WatchdogStatusResponse)
async def get_watchdog_status(request: Request) -> WatchdogStatusResponse:
    """
    Get detailed watchdog safety system status.
    """
    watchdog = get_watchdog(request)

    if not watchdog:
        return WatchdogStatusResponse(
            active=False,
            mode="disabled",
            kill_switch_armed=False,
            heartbeat_healthy=False,
            agentic_allowed=True,  # Allow if no watchdog
        )

    try:
        status = watchdog.get_status()
        return WatchdogStatusResponse(
            active=True,
            mode=status.mode.value if hasattr(status.mode, 'value') else str(status.mode),
            kill_switch_armed=status.kill_switch_armed,
            heartbeat_healthy=status.heartbeat_healthy,
            agentic_allowed=watchdog.is_agentic_allowed(),
            uptime_seconds=status.uptime_seconds,
            active_task=status.active_task_id,
            consecutive_failures=status.consecutive_failures,
            last_activity=status.last_activity_time,
        )
    except Exception as e:
        logger.error(f"Error getting watchdog status: {e}")
        return WatchdogStatusResponse(
            active=True,
            mode="unknown",
            kill_switch_armed=False,
            heartbeat_healthy=True,
            agentic_allowed=True,
        )


@router.post("/execute", response_model=ExecuteGoalResponse)
async def execute_goal(request: Request, body: ExecuteGoalRequest) -> ExecuteGoalResponse:
    """
    Execute a goal using the unified AgenticTaskRunner.

    This is a Tier 2 operation requiring VBIA authentication.
    API clients can pass pre-verified VBIA credentials.
    """
    task_id = str(uuid4())
    start_time = time.time()

    logger.info(f"[AgenticAPI] Execute goal: {sanitize_for_log(body.goal, 50)}...")

    runner = get_agentic_runner(request)
    vbia = get_vbia_adapter(request)
    watchdog = get_watchdog(request)

    if not runner:
        raise HTTPException(
            status_code=503,
            detail="AgenticTaskRunner not initialized. Supervisor may not be fully started."
        )

    if not runner.is_ready:
        raise HTTPException(
            status_code=503,
            detail="AgenticTaskRunner not ready. Computer Use capability unavailable."
        )

    # Check watchdog
    if watchdog and not watchdog.is_agentic_allowed():
        raise HTTPException(
            status_code=403,
            detail="Agentic execution blocked by watchdog safety system"
        )

    # Set VBIA cache if pre-verified credentials provided
    if vbia and body.vbia_confidence is not None:
        vbia.set_verification_result(
            confidence=body.vbia_confidence,
            speaker_id=body.speaker_id,
            is_owner=True,
            verified=body.vbia_confidence >= 0.85,
        )
        logger.debug(f"[AgenticAPI] VBIA cache set: {body.vbia_confidence:.2f}")

    try:
        # Import RunnerMode
        from core.agentic_task_runner import RunnerMode

        # Execute via runner
        result = await asyncio.wait_for(
            runner.run(
                goal=body.goal,
                mode=RunnerMode(body.mode),
                context=body.context,
                narrate=body.narrate,
            ),
            timeout=body.timeout_seconds
        )

        execution_time = (time.time() - start_time) * 1000

        return ExecuteGoalResponse(
            success=result.success,
            task_id=task_id,
            goal=body.goal,
            mode=result.mode,
            status="completed" if result.success else "failed",
            final_message=result.final_message,
            actions_count=result.actions_count,
            execution_time_ms=execution_time,
            reasoning_steps=result.reasoning_steps,
            learning_insights=result.learning_insights,
            watchdog_status=result.watchdog_status,
            error=result.error,
        )

    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=408,
            detail=f"Goal execution timed out after {body.timeout_seconds}s"
        )
    except Exception as e:
        logger.error(f"[AgenticAPI] Execution failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Goal execution failed: {str(e)}"
        )


@router.post("/route", response_model=RouteCommandResponse)
async def route_command(request: Request, body: RouteCommandRequest) -> RouteCommandResponse:
    """
    Route a voice command through the TieredCommandRouter.

    This parses wake words, classifies intent, performs VBIA authentication,
    and optionally executes the command via the appropriate backend.
    """
    logger.info(f"[AgenticAPI] Route command: {sanitize_for_log(body.command, 50)}...")

    router_obj = get_tiered_router(request)
    vbia = get_vbia_adapter(request)
    runner = get_agentic_runner(request)

    if not router_obj:
        raise HTTPException(
            status_code=503,
            detail="TieredCommandRouter not initialized"
        )

    # Set VBIA cache if pre-verified
    if vbia and body.vbia_confidence is not None:
        vbia.set_verification_result(
            confidence=body.vbia_confidence,
            speaker_id=body.speaker_id,
            is_owner=True,
            verified=body.vbia_confidence >= 0.85,
        )

    try:
        # Route the command
        route_result = await router_obj.route(body.command)

        response = RouteCommandResponse(
            tier=route_result.tier.value,
            backend=route_result.backend,
            command=route_result.command,
            auth_required=route_result.auth_required,
            auth_result=route_result.auth_result.value if route_result.auth_result else None,
            vbia_confidence=route_result.vbia_confidence,
            watchdog_armed=route_result.watchdog_armed,
            execution_allowed=route_result.execution_allowed,
            denial_reason=route_result.denial_reason,
        )

        # Execute if requested and allowed
        if body.execute and route_result.execution_allowed:
            from core.tiered_command_router import CommandTier

            if route_result.tier == CommandTier.TIER2_AGENTIC and runner:
                # Execute via runner
                from core.agentic_task_runner import RunnerMode
                exec_result = await runner.run(
                    goal=route_result.command,
                    mode=RunnerMode.AUTONOMOUS,
                    narrate=True,
                )
                response.executed = True
                response.execution_result = {
                    "success": exec_result.success,
                    "message": exec_result.final_message,
                    "actions": exec_result.actions_count,
                    "time_ms": exec_result.execution_time_ms,
                }

            elif route_result.tier == CommandTier.TIER1_STANDARD:
                # Execute Tier 1 via router's handler
                exec_result = await router_obj.execute_tier1(route_result.command)
                response.executed = True
                response.execution_result = exec_result

        return response

    except Exception as e:
        logger.error(f"[AgenticAPI] Route failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Command routing failed: {str(e)}"
        )


@router.post("/tier1")
async def execute_tier1(request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a Tier 1 (safe) command.

    No strict VBIA required, uses Gemini Flash backend.
    """
    command = body.get("command", "")
    context = body.get("context", {})

    if not command:
        raise HTTPException(status_code=400, detail="Command required")

    router_obj = get_tiered_router(request)
    if not router_obj:
        raise HTTPException(status_code=503, detail="Router not available")

    try:
        result = await router_obj.execute_tier1(command, context)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def agentic_health(request: Request) -> Dict[str, Any]:
    """
    Health check for the unified agentic system.
    """
    runner = get_agentic_runner(request)
    router_obj = get_tiered_router(request)
    watchdog = get_watchdog(request)
    vbia = get_vbia_adapter(request)

    components = {
        "runner": runner is not None and (runner.is_ready if runner else False),
        "router": router_obj is not None,
        "watchdog": watchdog is not None,
        "vbia": vbia is not None,
    }

    all_ready = all([runner, router_obj])  # Minimum required

    if not all_ready:
        return {
            "status": "initializing",
            "message": "Agentic system is starting up",
            "components": components,
        }

    if watchdog and not watchdog.is_agentic_allowed():
        return {
            "status": "restricted",
            "message": "Watchdog has restricted agentic execution",
            "components": components,
        }

    return {
        "status": "healthy",
        "message": "Unified agentic system operational",
        "components": components,
        "capabilities": {
            "tier1": True,
            "tier2": runner.is_ready if runner else False,
            "watchdog": watchdog is not None,
            "vbia": vbia is not None,
        }
    }


@router.get("/metrics")
async def get_metrics(request: Request) -> Dict[str, Any]:
    """
    Get execution metrics for the agentic system.
    """
    runner = get_agentic_runner(request)
    router_obj = get_tiered_router(request)

    metrics = {
        "runner": runner.get_stats() if runner else {},
        "router": router_obj.get_stats() if router_obj else {},
    }

    return metrics


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
        for connection in self.active_connections[:]:
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


ws_manager = AgenticWebSocketManager()


def get_ws_manager() -> AgenticWebSocketManager:
    """Get the WebSocket manager for broadcasting from other modules."""
    return ws_manager


@router.websocket("/ws")
async def agentic_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for real-time agentic task updates.

    Messages:
    - {"type": "status"} - Get current status
    - {"type": "execute", "goal": "..."} - Execute goal
    - {"type": "ping"} - Keepalive
    """
    await ws_manager.connect(websocket)

    try:
        await websocket.send_json({
            "type": "connected",
            "message": "Connected to unified agentic WebSocket",
            "timestamp": time.time(),
        })

        # WebSocket idle timeout protection
        idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("Agentic WebSocket idle timeout, closing connection")
                break

            msg_type = data.get("type", "")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong", "timestamp": time.time()})

            elif msg_type == "status":
                # Send status
                await websocket.send_json({
                    "type": "status",
                    "data": {
                        "ready": True,
                        "timestamp": time.time(),
                    }
                })

            elif msg_type == "execute":
                goal = data.get("goal", "")
                if goal:
                    await websocket.send_json({
                        "type": "task_started",
                        "data": {"goal": goal, "timestamp": time.time()}
                    })

                    # Note: Full execution would be implemented here
                    # streaming updates back to client

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)
