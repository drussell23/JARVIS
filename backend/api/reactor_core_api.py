"""
Reactor-Core Feedback API - Real-Time Training Status Receiver
===============================================================

This module provides the receiver endpoint for Reactor-Core to push
real-time training status updates back to JARVIS.

Architecture:
    ┌────────────────────────────────────────────────────────────────┐
    │                      Reactor-Core                               │
    │  ┌──────────────┐   ┌──────────────────┐   ┌───────────────┐   │
    │  │   Training   │ → │  Status          │ → │   HTTP POST   │   │
    │  │   Pipeline   │   │  Broadcaster     │   │   to JARVIS   │   │
    │  └──────────────┘   └──────────────────┘   └───────────────┘   │
    └────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
    ┌────────────────────────────────────────────────────────────────┐
    │                     JARVIS-AI-Agent                            │
    │  ┌──────────────┐   ┌──────────────────┐   ┌───────────────┐  │
    │  │   This API   │ → │  Status Hub      │ → │   TTS Voice   │  │
    │  │   Receiver   │   │  & Logging       │   │   Announce    │  │
    │  └──────────────┘   └──────────────────┘   └───────────────┘  │
    │                              │                                 │
    │                    ┌─────────▼─────────┐                       │
    │                    │   WebSocket       │                       │
    │                    │   Broadcast       │                       │
    │                    └───────────────────┘                       │
    └────────────────────────────────────────────────────────────────┘

Features:
- Real-time training status updates
- Voice announcements at key milestones
- WebSocket broadcasting to connected clients
- Cross-repo event bridge integration
- Intelligent progress tracking with history

Author: JARVIS AI System
Version: 1.0.0 (Feedback Loop)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class TrainingStage(str, Enum):
    """Training pipeline stages."""
    IDLE = "idle"
    DATA_PREP = "data_prep"
    SCOUTING = "scouting"
    INGESTING = "ingesting"
    FORMATTING = "formatting"
    DISTILLING = "distilling"
    FINE_TUNING = "fine_tuning"
    TRAINING = "training"
    EVALUATION = "evaluation"
    EVALUATING = "evaluating"
    QUANTIZING = "quantizing"
    EXPORTING = "exporting"
    DEPLOYING = "deploying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TrainingStatus(str, Enum):
    """Training job status."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# =============================================================================
# Request/Response Models
# =============================================================================

class TrainingStatusUpdate(BaseModel):
    """Training status update from Reactor-Core."""
    job_id: str = Field(..., description="Training job identifier")
    status: str = Field(..., description="Current status (queued, running, completed, failed)")
    progress: float = Field(0.0, ge=0.0, le=100.0, description="Progress percentage 0-100")
    stage: str = Field("idle", description="Current pipeline stage")
    message: str = Field("", description="Human-readable status message")
    # Optional details
    metrics: Dict[str, Any] = Field(default_factory=dict, description="Training metrics")
    started_at: Optional[str] = Field(None, description="ISO timestamp when job started")
    estimated_completion: Optional[str] = Field(None, description="Estimated completion time")
    experience_count: int = Field(0, description="Number of experiences being processed")
    # Model output info (for hot-swap integration)
    output_model_path: Optional[str] = Field(None, description="Path to output model file")


class TrainingStatusResponse(BaseModel):
    """Response for training status update."""
    received: bool = True
    job_id: str
    progress: float
    announced: bool = False
    broadcast: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class TrainingHistoryEntry(BaseModel):
    """Entry in training history."""
    job_id: str
    timestamp: str
    status: str
    progress: float
    stage: str
    message: str


# =============================================================================
# Training Status Hub - Central State Management
# =============================================================================

class TrainingStatusHub:
    """
    Central hub for tracking training status across the system.

    Features:
    - Maintains current training state
    - Tracks history of updates
    - Broadcasts to WebSocket clients
    - Triggers voice announcements
    - Writes to cross-repo bridge
    """

    def __init__(self):
        self._current_job: Optional[Dict[str, Any]] = None
        self._history: List[Dict[str, Any]] = []
        self._max_history = 100

        # Progress milestones for announcements (avoid spam)
        self._announced_milestones: Set[int] = set()
        self._milestone_thresholds = [0, 25, 50, 75, 100]

        # WebSocket connections
        self._ws_connections: List[WebSocket] = []

        # Callbacks
        self._tts_callback: Optional[Callable] = None
        self._on_completed_callbacks: List[Callable] = []
        self._on_failed_callbacks: List[Callable] = []

        # Stats
        self._updates_received = 0
        self._announcements_made = 0
        self._start_time = datetime.now()

        logger.info("[ReactorCoreAPI] TrainingStatusHub initialized")

    def set_tts_callback(self, callback: Callable) -> None:
        """Set the TTS callback for voice announcements."""
        self._tts_callback = callback
        logger.info("[ReactorCoreAPI] TTS callback registered")

    def on_training_completed(self, callback: Callable) -> None:
        """Register callback for training completion."""
        self._on_completed_callbacks.append(callback)

    def on_training_failed(self, callback: Callable) -> None:
        """Register callback for training failure."""
        self._on_failed_callbacks.append(callback)

    async def register_websocket(self, ws: WebSocket) -> None:
        """Register a WebSocket connection for updates."""
        await ws.accept()
        self._ws_connections.append(ws)
        logger.debug(f"[ReactorCoreAPI] WebSocket registered ({len(self._ws_connections)} active)")

    def unregister_websocket(self, ws: WebSocket) -> None:
        """Unregister a WebSocket connection."""
        if ws in self._ws_connections:
            self._ws_connections.remove(ws)
            logger.debug(f"[ReactorCoreAPI] WebSocket unregistered ({len(self._ws_connections)} active)")

    async def process_update(self, update: TrainingStatusUpdate) -> Dict[str, Any]:
        """
        Process a training status update.

        Args:
            update: The status update from Reactor-Core

        Returns:
            Result dict with announced/broadcast flags
        """
        self._updates_received += 1
        timestamp = datetime.now().isoformat()

        # Log the update clearly
        log_message = self._format_log_message(update)
        logger.info(log_message)

        # Update current job state
        if self._current_job is None or self._current_job.get("job_id") != update.job_id:
            # New job started
            self._current_job = {
                "job_id": update.job_id,
                "started_at": update.started_at or timestamp,
            }
            self._announced_milestones.clear()

        self._current_job.update({
            "status": update.status,
            "progress": update.progress,
            "stage": update.stage,
            "message": update.message,
            "metrics": update.metrics,
            "last_update": timestamp,
            "experience_count": update.experience_count,
            "output_model_path": update.output_model_path,
        })

        # Add to history
        self._add_to_history(update, timestamp)

        # Check for milestone announcement
        announced = await self._check_and_announce(update)

        # Broadcast to WebSocket clients
        broadcast = await self._broadcast_update(update, timestamp)

        # Write to cross-repo bridge
        await self._write_bridge_event(update, timestamp)

        # Handle completion/failure callbacks
        if update.status == "completed":
            await self._trigger_completed_callbacks(update)
        elif update.status == "failed":
            await self._trigger_failed_callbacks(update)

        return {
            "announced": announced,
            "broadcast": broadcast,
            "timestamp": timestamp,
        }

    def _format_log_message(self, update: TrainingStatusUpdate) -> str:
        """Format a clear log message for the update."""
        stage_display = update.stage.replace("_", " ").title()

        if update.progress >= 100:
            return f"[Reactor Update] {stage_display}: COMPLETE - {update.message}"
        elif update.progress > 0:
            return f"[Reactor Update] {stage_display}: {update.progress:.0f}% - {update.message}"
        else:
            return f"[Reactor Update] {stage_display}: Started - {update.message}"

    def _add_to_history(self, update: TrainingStatusUpdate, timestamp: str) -> None:
        """Add update to history, maintaining max size."""
        entry = {
            "job_id": update.job_id,
            "timestamp": timestamp,
            "status": update.status,
            "progress": update.progress,
            "stage": update.stage,
            "message": update.message,
        }
        self._history.append(entry)

        # Trim history
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    async def _check_and_announce(self, update: TrainingStatusUpdate) -> bool:
        """Check if we should make a voice announcement."""
        if not self._tts_callback:
            return False

        announced = False

        # Check for milestone crossing
        for milestone in self._milestone_thresholds:
            if (milestone not in self._announced_milestones and
                update.progress >= milestone):
                self._announced_milestones.add(milestone)

                # Generate announcement
                announcement = self._generate_announcement(update, milestone)
                if announcement:
                    try:
                        if asyncio.iscoroutinefunction(self._tts_callback):
                            await self._tts_callback(announcement)
                        else:
                            self._tts_callback(announcement)
                        announced = True
                        self._announcements_made += 1
                        logger.debug(f"[ReactorCoreAPI] Announced: {announcement}")
                    except Exception as e:
                        logger.warning(f"[ReactorCoreAPI] TTS announcement failed: {e}")

        # Special announcements for status changes
        if update.status == "completed" and "completed" not in str(self._announced_milestones):
            try:
                announcement = "Sir, the training pipeline has completed successfully. The new model is ready for deployment."
                if asyncio.iscoroutinefunction(self._tts_callback):
                    await self._tts_callback(announcement)
                else:
                    self._tts_callback(announcement)
                announced = True
                self._announcements_made += 1
            except Exception as e:
                logger.warning(f"[ReactorCoreAPI] TTS announcement failed: {e}")

        elif update.status == "failed" and "failed" not in str(self._announced_milestones):
            try:
                announcement = f"Sir, the training pipeline has encountered an error. {update.message}"
                if asyncio.iscoroutinefunction(self._tts_callback):
                    await self._tts_callback(announcement)
                else:
                    self._tts_callback(announcement)
                announced = True
                self._announcements_made += 1
            except Exception as e:
                logger.warning(f"[ReactorCoreAPI] TTS announcement failed: {e}")

        return announced

    def _generate_announcement(self, update: TrainingStatusUpdate, milestone: int) -> Optional[str]:
        """Generate a voice announcement for a milestone."""
        stage_display = update.stage.replace("_", " ").lower()

        if milestone == 0:
            return f"Sir, training has begun. Currently in {stage_display} phase."
        elif milestone == 25:
            return f"Sir, training is 25 percent complete. {stage_display.title()} in progress."
        elif milestone == 50:
            return "Sir, training is halfway complete."
        elif milestone == 75:
            return "Sir, training is 75 percent complete. Nearly finished."
        elif milestone == 100:
            return None  # Handled by status == "completed" above

        return None

    async def _broadcast_update(self, update: TrainingStatusUpdate, timestamp: str) -> bool:
        """Broadcast update to all connected WebSocket clients."""
        if not self._ws_connections:
            return False

        message = {
            "type": "training_status",
            "data": {
                "job_id": update.job_id,
                "status": update.status,
                "progress": update.progress,
                "stage": update.stage,
                "message": update.message,
                "metrics": update.metrics,
                "timestamp": timestamp,
            }
        }

        disconnected = []
        for ws in self._ws_connections[:]:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(ws)

        # Clean up disconnected
        for ws in disconnected:
            self.unregister_websocket(ws)

        return len(self._ws_connections) > 0

    async def _write_bridge_event(self, update: TrainingStatusUpdate, timestamp: str) -> None:
        """Write update to cross-repo bridge."""
        try:
            bridge_dir = Path.home() / ".jarvis" / "cross_repo" / "training_events"
            bridge_dir.mkdir(parents=True, exist_ok=True)

            event = {
                "event_id": str(uuid.uuid4())[:8],
                "event_type": "training_status_update",
                "source": "reactor_core",
                "timestamp": timestamp,
                "payload": {
                    "job_id": update.job_id,
                    "status": update.status,
                    "progress": update.progress,
                    "stage": update.stage,
                    "message": update.message,
                },
            }

            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{event['event_id']}.json"
            filepath = bridge_dir / filename

            with open(filepath, "w") as f:
                json.dump(event, f, indent=2)

        except Exception as e:
            logger.debug(f"[ReactorCoreAPI] Bridge write error: {e}")

    async def _trigger_completed_callbacks(self, update: TrainingStatusUpdate) -> None:
        """Trigger training completed callbacks."""
        data = {
            "job_id": update.job_id,
            "metrics": update.metrics,
            "output_model_path": update.output_model_path,
            "experience_count": update.experience_count,
        }

        for callback in self._on_completed_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                logger.warning(f"[ReactorCoreAPI] Completed callback error: {e}")

    async def _trigger_failed_callbacks(self, update: TrainingStatusUpdate) -> None:
        """Trigger training failed callbacks."""
        data = {
            "job_id": update.job_id,
            "error": update.message,
            "stage": update.stage,
        }

        for callback in self._on_failed_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                logger.warning(f"[ReactorCoreAPI] Failed callback error: {e}")

    def get_current_job(self) -> Optional[Dict[str, Any]]:
        """Get the current training job state."""
        return self._current_job

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get training update history."""
        return self._history[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        """Get hub statistics."""
        return {
            "updates_received": self._updates_received,
            "announcements_made": self._announcements_made,
            "active_websockets": len(self._ws_connections),
            "history_size": len(self._history),
            "uptime_seconds": (datetime.now() - self._start_time).total_seconds(),
            "current_job": self._current_job,
            "tts_available": self._tts_callback is not None,
        }


# =============================================================================
# Global Hub Instance
# =============================================================================

_training_hub: Optional[TrainingStatusHub] = None


def get_training_hub() -> TrainingStatusHub:
    """Get or create the global training status hub."""
    global _training_hub
    if _training_hub is None:
        _training_hub = TrainingStatusHub()
    return _training_hub


def set_training_hub_tts(callback: Callable) -> None:
    """Set the TTS callback on the global hub."""
    hub = get_training_hub()
    hub.set_tts_callback(callback)


# =============================================================================
# FastAPI Router
# =============================================================================

router = APIRouter(prefix="/reactor-core", tags=["reactor-core"])


@router.post("/training/status", response_model=TrainingStatusResponse)
async def receive_training_status(
    update: TrainingStatusUpdate,
    request: Request,
) -> TrainingStatusResponse:
    """
    Receive training status update from Reactor-Core.

    This endpoint is called by Reactor-Core's training pipeline
    to push real-time progress updates to JARVIS.

    The update will:
    1. Be logged clearly to the console
    2. Trigger voice announcements at key milestones (0%, 25%, 50%, 75%, 100%)
    3. Broadcast to connected WebSocket clients
    4. Update the training status hub state

    Args:
        update: Training status update payload

    Returns:
        Confirmation response with announcement/broadcast flags
    """
    hub = get_training_hub()

    # Try to get TTS callback from app state if not set
    if hub._tts_callback is None:
        # Try to get from agentic runner
        try:
            from backend.core.agentic_task_runner import get_agentic_runner
            runner = get_agentic_runner()
            if runner and runner.tts_callback:
                hub.set_tts_callback(runner.tts_callback)
        except Exception:
            pass

        # Try from app.state
        if hasattr(request.app.state, 'tts_callback'):
            hub.set_tts_callback(request.app.state.tts_callback)

    # Process the update
    result = await hub.process_update(update)

    return TrainingStatusResponse(
        received=True,
        job_id=update.job_id,
        progress=update.progress,
        announced=result.get("announced", False),
        broadcast=result.get("broadcast", False),
        timestamp=result.get("timestamp", datetime.now().isoformat()),
    )


@router.get("/training/current")
async def get_current_training() -> Dict[str, Any]:
    """Get the current training job status."""
    hub = get_training_hub()
    current = hub.get_current_job()

    if not current:
        return {
            "active": False,
            "message": "No training job currently active",
        }

    return {
        "active": current.get("status") == "running",
        "job": current,
    }


@router.get("/training/history")
async def get_training_history(limit: int = 20) -> Dict[str, Any]:
    """Get training update history."""
    hub = get_training_hub()
    history = hub.get_history(limit=limit)

    return {
        "count": len(history),
        "history": history,
    }


@router.get("/status")
async def get_reactor_core_api_status() -> Dict[str, Any]:
    """Get Reactor-Core API status."""
    hub = get_training_hub()
    stats = hub.get_stats()

    return {
        "status": "healthy",
        "hub_stats": stats,
    }


@router.websocket("/training/ws")
async def training_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for real-time training updates.

    Clients can connect to receive live training status updates
    as they happen.
    """
    hub = get_training_hub()
    await hub.register_websocket(websocket)

    try:
        # Send current state
        current = hub.get_current_job()
        if current:
            await websocket.send_json({
                "type": "current_state",
                "data": current,
            })

        await websocket.send_json({
            "type": "connected",
            "message": "Connected to Reactor-Core training feed",
            "timestamp": datetime.now().isoformat(),
        })

        # Keep connection alive with timeout protection
        idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("Reactor-Core WebSocket idle timeout, closing connection")
                break

            msg_type = data.get("type", "")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong", "timestamp": time.time()})
            elif msg_type == "get_status":
                current = hub.get_current_job()
                await websocket.send_json({
                    "type": "status",
                    "data": current or {"active": False},
                })

    except WebSocketDisconnect:
        hub.unregister_websocket(websocket)
    except Exception as e:
        logger.warning(f"[ReactorCoreAPI] WebSocket error: {e}")
        hub.unregister_websocket(websocket)


# =============================================================================
# Integration Helpers
# =============================================================================

async def connect_to_agentic_runner() -> bool:
    """
    Connect the training hub to the agentic runner for callbacks.

    This should be called after the agentic runner is initialized.
    """
    hub = get_training_hub()

    try:
        from backend.core.agentic_task_runner import get_agentic_runner
        runner = get_agentic_runner()

        if runner:
            # Set TTS callback
            if runner.tts_callback:
                hub.set_tts_callback(runner.tts_callback)

            # Register for training completed events
            # This enables hot-swap integration
            hub.on_training_completed(runner._on_training_completed)
            hub.on_training_failed(runner._on_training_failed)

            logger.info("[ReactorCoreAPI] Connected to AgenticTaskRunner")
            return True

    except Exception as e:
        logger.warning(f"[ReactorCoreAPI] Failed to connect to runner: {e}")

    return False
