"""
Ghost Hands API
===============

REST API endpoints for controlling the Ghost Hands background automation system.

Endpoints:
- POST /ghost-hands/start - Start the Ghost Hands system
- POST /ghost-hands/stop - Stop the Ghost Hands system
- GET  /ghost-hands/status - Get system status
- GET  /ghost-hands/stats - Get statistics

Task Management:
- POST /ghost-hands/tasks - Create a new Ghost Task
- GET  /ghost-hands/tasks - List all tasks
- GET  /ghost-hands/tasks/{name} - Get task details
- POST /ghost-hands/tasks/{name}/pause - Pause a task
- POST /ghost-hands/tasks/{name}/resume - Resume a task
- DELETE /ghost-hands/tasks/{name} - Cancel a task

Quick Actions:
- POST /ghost-hands/watch-and-react - Create watch-and-react task
- POST /ghost-hands/auto-retry - Create auto-retry task
- POST /ghost-hands/notify - Create notification task

Components:
- GET  /ghost-hands/vision/windows - List all visible windows
- GET  /ghost-hands/vision/stats - Get vision system stats
- GET  /ghost-hands/actuator/stats - Get actuator stats
- GET  /ghost-hands/narration/stats - Get narration stats
- POST /ghost-hands/narration/speak - Speak custom text
- POST /ghost-hands/narration/verbosity - Set verbosity level
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union
from datetime import datetime
from enum import Enum
import logging

logger = logging.getLogger(__name__)

# Lazy imports to avoid circular imports
_ghost_hands = None
_initialized = False


async def _get_ghost_hands():
    """Get the Ghost Hands orchestrator (lazy initialization)."""
    global _ghost_hands, _initialized

    if _ghost_hands is None or not _initialized:
        try:
            from ghost_hands import get_ghost_hands
            _ghost_hands = await get_ghost_hands()
            _initialized = True
        except Exception as e:
            logger.error(f"Failed to initialize Ghost Hands: {e}")
            raise HTTPException(
                status_code=503,
                detail=f"Ghost Hands not available: {str(e)}"
            )

    return _ghost_hands


# =============================================================================
# Pydantic Models
# =============================================================================

class ActionModel(BaseModel):
    """A single action in a Ghost Task."""
    type: str = Field(..., description="Action type: click, type_text, press_key, wait, narrate_*, etc.")
    selector: Optional[str] = Field(None, description="CSS/XPath selector for element")
    coordinates: Optional[List[int]] = Field(None, description="[x, y] coordinates for click")
    text: Optional[str] = Field(None, description="Text to type or narrate")
    key: Optional[str] = Field(None, description="Key to press")
    modifiers: Optional[List[str]] = Field(None, description="Key modifiers: command, shift, option, control")
    seconds: Optional[float] = Field(None, description="Duration to wait")
    script: Optional[str] = Field(None, description="Custom AppleScript to run")


class CreateTaskRequest(BaseModel):
    """Request to create a new Ghost Task."""
    name: str = Field(..., description="Unique task name")
    watch_app: Optional[str] = Field(None, description="Application name to watch")
    watch_window_id: Optional[int] = Field(None, description="Specific window ID to watch")
    trigger_text: Optional[Union[str, List[str]]] = Field(None, description="Text trigger(s)")
    trigger_pattern: Optional[str] = Field(None, description="Regex pattern trigger")
    actions: List[ActionModel] = Field(default_factory=list, description="Actions to execute")
    one_shot: bool = Field(True, description="Stop after first trigger if True")
    priority: int = Field(5, ge=1, le=10, description="Task priority (1=highest)")


class WatchAndReactRequest(BaseModel):
    """Request for quick watch-and-react task."""
    app_name: str = Field(..., description="Application to watch")
    trigger_text: str = Field(..., description="Text that triggers reaction")
    reaction: str = Field(..., description="Text to type as reaction")
    task_name: Optional[str] = Field(None, description="Optional task name")


class AutoRetryRequest(BaseModel):
    """Request for auto-retry on failure task."""
    app_name: str = Field(..., description="Application to watch (e.g., Terminal)")
    failure_text: str = Field(..., description="Text indicating failure (e.g., BUILD FAILED)")
    retry_command: str = Field(..., description="Command to retry (e.g., npm run build)")
    max_retries: int = Field(3, ge=1, le=10, description="Maximum retry attempts")


class NotifyRequest(BaseModel):
    """Request for notification task."""
    app_name: str = Field(..., description="Application to watch")
    completion_text: str = Field(..., description="Text indicating completion")
    notification: str = Field(..., description="What to say when complete")


class SpeakRequest(BaseModel):
    """Request to speak custom text."""
    text: str = Field(..., description="Text to speak")
    priority: str = Field("normal", description="Priority: critical, high, normal, low")


class VerbosityRequest(BaseModel):
    """Request to set verbosity level."""
    level: str = Field(..., description="Verbosity: silent, minimal, normal, verbose, debug")


class TaskResponse(BaseModel):
    """Response for task operations."""
    status: str
    task_name: str
    state: Optional[str] = None
    message: Optional[str] = None


class StatsResponse(BaseModel):
    """Response for statistics."""
    is_running: bool
    components: Dict[str, bool]
    active_tasks: int
    total_triggers: int
    total_actions_executed: int
    start_time: Optional[str] = None


# =============================================================================
# API Router
# =============================================================================

class GhostHandsAPI:
    """API for Ghost Hands background automation system."""

    def __init__(self):
        self.router = APIRouter(prefix="/ghost-hands", tags=["ghost-hands"])
        self._register_routes()

    def _register_routes(self):
        """Register API routes."""
        # System control
        self.router.add_api_route("/start", self.start_system, methods=["POST"])
        self.router.add_api_route("/stop", self.stop_system, methods=["POST"])
        self.router.add_api_route("/status", self.get_status, methods=["GET"])
        self.router.add_api_route("/stats", self.get_stats, methods=["GET"])

        # Task management
        self.router.add_api_route("/tasks", self.create_task, methods=["POST"])
        self.router.add_api_route("/tasks", self.list_tasks, methods=["GET"])
        self.router.add_api_route("/tasks/{name}", self.get_task, methods=["GET"])
        self.router.add_api_route("/tasks/{name}/pause", self.pause_task, methods=["POST"])
        self.router.add_api_route("/tasks/{name}/resume", self.resume_task, methods=["POST"])
        self.router.add_api_route("/tasks/{name}", self.cancel_task, methods=["DELETE"])

        # Quick actions
        self.router.add_api_route("/watch-and-react", self.watch_and_react, methods=["POST"])
        self.router.add_api_route("/auto-retry", self.auto_retry, methods=["POST"])
        self.router.add_api_route("/notify", self.notify_on_completion, methods=["POST"])

        # Component endpoints
        self.router.add_api_route("/vision/windows", self.list_windows, methods=["GET"])
        self.router.add_api_route("/vision/stats", self.get_vision_stats, methods=["GET"])
        self.router.add_api_route("/actuator/stats", self.get_actuator_stats, methods=["GET"])
        self.router.add_api_route("/actuator/history", self.get_action_history, methods=["GET"])
        self.router.add_api_route("/narration/stats", self.get_narration_stats, methods=["GET"])
        self.router.add_api_route("/narration/speak", self.speak, methods=["POST"])
        self.router.add_api_route("/narration/verbosity", self.set_verbosity, methods=["POST"])

        # Execution history
        self.router.add_api_route("/history", self.get_execution_history, methods=["GET"])

    # =========================================================================
    # System Control
    # =========================================================================

    async def start_system(self) -> Dict:
        """Start the Ghost Hands system."""
        try:
            ghost = await _get_ghost_hands()
            if ghost._is_running:
                return {
                    "status": "already_running",
                    "message": "Ghost Hands is already running"
                }

            success = await ghost.start()
            return {
                "status": "started" if success else "failed",
                "message": "Ghost Hands system started" if success else "Failed to start"
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def stop_system(self) -> Dict:
        """Stop the Ghost Hands system."""
        global _ghost_hands, _initialized

        try:
            if _ghost_hands and _initialized:
                await _ghost_hands.stop()
                _initialized = False
                return {
                    "status": "stopped",
                    "message": "Ghost Hands system stopped"
                }
            else:
                return {
                    "status": "not_running",
                    "message": "Ghost Hands is not running"
                }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_status(self) -> Dict:
        """Get Ghost Hands system status."""
        try:
            ghost = await _get_ghost_hands()
            stats = ghost.get_stats()

            return {
                "status": "running" if ghost._is_running else "stopped",
                "is_running": ghost._is_running,
                "components": stats.get("components", {}),
                "active_tasks": stats.get("active_tasks", 0),
                "watching_tasks": stats.get("watching_tasks", 0),
            }
        except HTTPException:
            return {
                "status": "not_initialized",
                "is_running": False,
                "components": {},
                "active_tasks": 0,
            }

    async def get_stats(self) -> Dict:
        """Get Ghost Hands statistics."""
        try:
            ghost = await _get_ghost_hands()
            stats = ghost.get_stats()

            return {
                "is_running": ghost._is_running,
                "components": stats.get("components", {}),
                "active_tasks": stats.get("active_tasks", 0),
                "watching_tasks": stats.get("watching_tasks", 0),
                "total_tasks_created": stats.get("total_tasks_created", 0),
                "total_triggers": stats.get("total_triggers", 0),
                "total_actions_executed": stats.get("total_actions_executed", 0),
                "successful_executions": stats.get("successful_executions", 0),
                "failed_executions": stats.get("failed_executions", 0),
                "start_time": stats.get("start_time").isoformat() if stats.get("start_time") else None,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # =========================================================================
    # Task Management
    # =========================================================================

    async def create_task(self, request: CreateTaskRequest) -> Dict:
        """Create a new Ghost Task."""
        try:
            ghost = await _get_ghost_hands()

            # Convert action models to GhostActions
            from ghost_hands import GhostAction

            actions = []
            for action_model in request.actions:
                action = self._convert_action(action_model)
                if action:
                    actions.append(action)

            task = await ghost.create_task(
                name=request.name,
                watch_app=request.watch_app,
                watch_window_id=request.watch_window_id,
                trigger_text=request.trigger_text,
                trigger_pattern=request.trigger_pattern,
                actions=actions,
                one_shot=request.one_shot,
                priority=request.priority,
            )

            return {
                "status": "created",
                "task_name": task.name,
                "state": task.state.name,
                "message": f"Task '{task.name}' created and watching"
            }

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    def _convert_action(self, model: ActionModel):
        """Convert ActionModel to GhostAction."""
        from ghost_hands import GhostAction

        action_type = model.type.lower()

        if action_type == "click":
            return GhostAction.click(
                selector=model.selector,
                coordinates=tuple(model.coordinates) if model.coordinates else None,
            )
        elif action_type == "type_text":
            return GhostAction.type_text(model.text or "", model.selector)
        elif action_type == "press_key":
            return GhostAction.press_key(model.key or "return", model.modifiers)
        elif action_type == "wait":
            return GhostAction.wait(model.seconds or 1)
        elif action_type == "narrate_perception":
            return GhostAction.narrate_perception(model.text or "")
        elif action_type == "narrate_intent":
            return GhostAction.narrate_intent(model.text or "")
        elif action_type == "narrate_action":
            return GhostAction.narrate_action(model.text or "")
        elif action_type == "narrate_confirmation":
            return GhostAction.narrate_confirmation(model.text or "")
        elif action_type == "narrate_custom":
            return GhostAction.narrate_custom(model.text or "")
        elif action_type == "run_script":
            return GhostAction.run_script(model.script or "", "applescript")
        else:
            logger.warning(f"Unknown action type: {action_type}")
            return None

    async def list_tasks(self) -> Dict:
        """List all Ghost Tasks."""
        try:
            ghost = await _get_ghost_hands()
            tasks = ghost.list_tasks()

            return {
                "tasks": tasks,
                "count": len(tasks)
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_task(self, name: str) -> Dict:
        """Get details of a specific task."""
        try:
            ghost = await _get_ghost_hands()
            task = ghost.get_task(name)

            if not task:
                raise HTTPException(status_code=404, detail=f"Task '{name}' not found")

            return {
                "name": task.name,
                "state": task.state.name,
                "watch_app": task.watch_app,
                "trigger_text": task.trigger_text,
                "trigger_pattern": task.trigger_pattern,
                "one_shot": task.one_shot,
                "enabled": task.enabled,
                "trigger_count": task.trigger_count,
                "last_triggered": task.last_triggered.isoformat() if task.last_triggered else None,
                "created_at": task.created_at.isoformat(),
                "error": task.error,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def pause_task(self, name: str) -> Dict:
        """Pause a Ghost Task."""
        try:
            ghost = await _get_ghost_hands()
            success = await ghost.pause_task(name)

            if not success:
                raise HTTPException(status_code=404, detail=f"Task '{name}' not found")

            return {
                "status": "paused",
                "task_name": name,
                "message": f"Task '{name}' paused"
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def resume_task(self, name: str) -> Dict:
        """Resume a paused Ghost Task."""
        try:
            ghost = await _get_ghost_hands()
            success = await ghost.resume_task(name)

            if not success:
                raise HTTPException(status_code=404, detail=f"Task '{name}' not found")

            return {
                "status": "resumed",
                "task_name": name,
                "message": f"Task '{name}' resumed"
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def cancel_task(self, name: str) -> Dict:
        """Cancel and remove a Ghost Task."""
        try:
            ghost = await _get_ghost_hands()
            success = await ghost.cancel_task(name)

            if not success:
                raise HTTPException(status_code=404, detail=f"Task '{name}' not found")

            return {
                "status": "cancelled",
                "task_name": name,
                "message": f"Task '{name}' cancelled and removed"
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # =========================================================================
    # Quick Actions
    # =========================================================================

    async def watch_and_react(self, request: WatchAndReactRequest) -> Dict:
        """Create a quick watch-and-react task."""
        try:
            ghost = await _get_ghost_hands()
            task = await ghost.watch_and_react(
                app_name=request.app_name,
                trigger_text=request.trigger_text,
                reaction=request.reaction,
                task_name=request.task_name,
            )

            return {
                "status": "created",
                "task_name": task.name,
                "state": task.state.name,
                "message": f"Watch-and-react task created for {request.app_name}"
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def auto_retry(self, request: AutoRetryRequest) -> Dict:
        """Create an auto-retry on failure task."""
        try:
            ghost = await _get_ghost_hands()
            task = await ghost.auto_retry_on_failure(
                app_name=request.app_name,
                failure_text=request.failure_text,
                retry_command=request.retry_command,
                max_retries=request.max_retries,
            )

            return {
                "status": "created",
                "task_name": task.name,
                "state": task.state.name,
                "message": f"Auto-retry task created for {request.app_name}"
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def notify_on_completion(self, request: NotifyRequest) -> Dict:
        """Create a notification task."""
        try:
            ghost = await _get_ghost_hands()
            task = await ghost.notify_on_completion(
                app_name=request.app_name,
                completion_text=request.completion_text,
                notification=request.notification,
            )

            return {
                "status": "created",
                "task_name": task.name,
                "state": task.state.name,
                "message": f"Notification task created for {request.app_name}"
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # =========================================================================
    # Component Endpoints
    # =========================================================================

    async def list_windows(self) -> Dict:
        """List all visible windows across all Spaces."""
        try:
            ghost = await _get_ghost_hands()

            if ghost._n_optic:
                windows = await ghost._n_optic.get_all_windows()
                return {
                    "windows": windows,
                    "count": len(windows)
                }
            else:
                return {
                    "windows": [],
                    "count": 0,
                    "message": "Vision system not available"
                }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_vision_stats(self) -> Dict:
        """Get N-Optic Nerve statistics."""
        try:
            ghost = await _get_ghost_hands()

            if ghost._n_optic:
                return ghost._n_optic.get_stats()
            else:
                return {"error": "Vision system not available"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_actuator_stats(self) -> Dict:
        """Get Background Actuator statistics."""
        try:
            ghost = await _get_ghost_hands()

            if ghost._actuator:
                return ghost._actuator.get_stats()
            else:
                return {"error": "Actuator system not available"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_action_history(self, limit: int = 10) -> Dict:
        """Get recent action history."""
        try:
            ghost = await _get_ghost_hands()

            if ghost._actuator:
                history = ghost._actuator.get_history(limit)
                return {
                    "history": history,
                    "count": len(history)
                }
            else:
                return {"history": [], "count": 0}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_narration_stats(self) -> Dict:
        """Get Narration Engine statistics."""
        try:
            ghost = await _get_ghost_hands()

            if ghost._narration:
                return ghost._narration.get_stats()
            else:
                return {"error": "Narration system not available"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def speak(self, request: SpeakRequest) -> Dict:
        """Speak custom text through the narration engine."""
        try:
            ghost = await _get_ghost_hands()

            if ghost._narration:
                await ghost._narration.narrate_custom(request.text)
                return {
                    "status": "queued",
                    "text": request.text,
                    "message": "Text queued for speech"
                }
            else:
                raise HTTPException(
                    status_code=503,
                    detail="Narration system not available"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def set_verbosity(self, request: VerbosityRequest) -> Dict:
        """Set narration verbosity level."""
        try:
            ghost = await _get_ghost_hands()

            if ghost._narration:
                from ghost_hands import VerbosityLevel

                try:
                    level = VerbosityLevel[request.level.upper()]
                except KeyError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid verbosity level: {request.level}. "
                               f"Valid: silent, minimal, normal, verbose, debug"
                    )

                ghost._narration.set_verbosity(level)
                return {
                    "status": "updated",
                    "verbosity": level.name,
                    "message": f"Verbosity set to {level.name}"
                }
            else:
                raise HTTPException(
                    status_code=503,
                    detail="Narration system not available"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_execution_history(self, limit: int = 10) -> Dict:
        """Get recent task execution history."""
        try:
            ghost = await _get_ghost_hands()
            history = ghost.get_execution_history(limit)

            return {
                "history": history,
                "count": len(history)
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Router Factory
# =============================================================================

def create_ghost_hands_router() -> APIRouter:
    """Create and return the Ghost Hands API router."""
    api = GhostHandsAPI()
    return api.router


# For direct import
ghost_hands_router = create_ghost_hands_router()
