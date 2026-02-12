"""
JARVIS Neural Mesh - Context Tracker Agent

Tracks and manages contextual information across the system.
Maintains awareness of current state, active tasks, and environment.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import AgentMessage, KnowledgeType, MessagePriority, MessageType

logger = logging.getLogger(__name__)


class ContextTrackerAgent(BaseNeuralMeshAgent):
    """
    Context Tracker Agent - Maintains system-wide context awareness.

    Capabilities:
    - track_context: Update current context
    - get_context: Retrieve current context
    - get_history: Get context history
    - analyze_patterns: Find patterns in context changes
    - predict_next: Predict likely next context
    """

    def __init__(self) -> None:
        super().__init__(
            agent_name="context_tracker_agent",
            agent_type="intelligence",
            capabilities={
                "track_context",
                "get_context",
                "get_history",
                "analyze_patterns",
                "get_environment",
                "session_info",
            },
            version="1.0.0",
        )

        self._current_context: Dict[str, Any] = {}
        self._context_history: List[Dict[str, Any]] = []
        self._session_start = datetime.now()
        self._context_updates = 0

    async def on_initialize(self) -> None:
        logger.info("Initializing ContextTrackerAgent")

        # Initialize with environment context
        self._current_context = await self._gather_environment_context()

        # Subscribe to context updates
        await self.subscribe(
            MessageType.CUSTOM,
            self._handle_context_update,
        )

        logger.info("ContextTrackerAgent initialized")

    async def on_start(self) -> None:
        logger.info("ContextTrackerAgent started - tracking context")

    async def on_stop(self) -> None:
        logger.info(
            f"ContextTrackerAgent stopping - tracked {self._context_updates} updates"
        )

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        action = payload.get("action", "")

        if action == "track_context":
            return await self._track_context(payload)
        elif action == "get_context":
            return self._get_current_context()
        elif action == "get_history":
            return self._get_context_history(payload.get("limit", 50))
        elif action == "get_environment":
            return await self._gather_environment_context()
        elif action == "session_info":
            return self._get_session_info()
        elif action == "analyze_patterns":
            return self._analyze_context_patterns()
        else:
            raise ValueError(f"Unknown context action: {action}")

    async def _track_context(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Update context with new information."""
        context_type = payload.get("context_type", "general")
        context_data = payload.get("data", {})
        source = payload.get("source", "unknown")

        # Store previous context in history
        if self._current_context:
            self._context_history.append({
                "timestamp": datetime.now().isoformat(),
                "context": self._current_context.copy(),
            })

            # Limit history size
            if len(self._context_history) > 1000:
                self._context_history = self._context_history[-1000:]

        # Update current context
        self._current_context.update({
            "last_update": datetime.now().isoformat(),
            "last_update_source": source,
            context_type: context_data,
        })

        self._context_updates += 1

        # Add to knowledge if significant
        if self.knowledge_graph:
            await self.add_knowledge(
                knowledge_type=KnowledgeType.CONTEXT,
                data={
                    "context_type": context_type,
                    "source": source,
                    "timestamp": datetime.now().isoformat(),
                },
                confidence=0.9,
            )

        return {
            "status": "updated",
            "context_type": context_type,
            "total_updates": self._context_updates,
        }

    def _get_current_context(self) -> Dict[str, Any]:
        """Get current context state."""
        return {
            "status": "success",
            "context": self._current_context,
            "session_duration_minutes": (datetime.now() - self._session_start).total_seconds() / 60,
        }

    def _get_context_history(self, limit: int = 50) -> Dict[str, Any]:
        """Get context history."""
        return {
            "status": "success",
            "count": len(self._context_history),
            "history": self._context_history[-limit:],
        }

    async def _gather_environment_context(self) -> Dict[str, Any]:
        """Gather current environment context."""
        context = {
            "gathered_at": datetime.now().isoformat(),
            "system": {
                "platform": platform.system(),
                "platform_release": platform.release(),
                "python_version": platform.python_version(),
                "hostname": platform.node(),
            },
            "environment": {
                "cwd": os.getcwd(),
                "user": os.getenv("USER", "unknown"),
                "home": os.path.expanduser("~"),
            },
            "session": {
                "start_time": self._session_start.isoformat(),
                "duration_minutes": (datetime.now() - self._session_start).total_seconds() / 60,
            },
        }

        # Add time context
        now = datetime.now()
        context["time"] = {
            "current": now.isoformat(),
            "hour": now.hour,
            "day_of_week": now.strftime("%A"),
            "is_weekend": now.weekday() >= 5,
            "period": self._get_time_period(now.hour),
        }

        return context

    def _get_time_period(self, hour: int) -> str:
        """Determine time period."""
        if 5 <= hour < 9:
            return "early_morning"
        elif 9 <= hour < 12:
            return "morning"
        elif 12 <= hour < 14:
            return "midday"
        elif 14 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 21:
            return "evening"
        elif 21 <= hour < 24:
            return "night"
        else:
            return "late_night"

    def _get_session_info(self) -> Dict[str, Any]:
        """Get session information."""
        duration = datetime.now() - self._session_start
        return {
            "status": "success",
            "session_start": self._session_start.isoformat(),
            "duration_seconds": duration.total_seconds(),
            "duration_minutes": duration.total_seconds() / 60,
            "context_updates": self._context_updates,
            "history_entries": len(self._context_history),
        }

    def _analyze_context_patterns(self) -> Dict[str, Any]:
        """Analyze patterns in context history."""
        if len(self._context_history) < 10:
            return {
                "status": "insufficient_data",
                "message": "Need at least 10 context entries for pattern analysis",
            }

        # Simple pattern analysis
        context_types = {}
        sources = {}

        for entry in self._context_history:
            ctx = entry.get("context", {})
            source = ctx.get("last_update_source", "unknown")
            sources[source] = sources.get(source, 0) + 1

            for key in ctx:
                if key not in ("last_update", "last_update_source"):
                    context_types[key] = context_types.get(key, 0) + 1

        return {
            "status": "success",
            "total_entries": len(self._context_history),
            "context_types": context_types,
            "sources": sources,
            "most_common_type": max(context_types, key=context_types.get) if context_types else None,
            "most_active_source": max(sources, key=sources.get) if sources else None,
        }

    async def _handle_context_update(self, message: AgentMessage) -> None:
        """Handle context update messages."""
        if message.payload.get("type") == "context_update":
            context_type = message.payload.get("context_type", "general")
            await self._track_context({
                "context_type": context_type,
                "data": message.payload.get("data", {}),
                "source": message.from_agent,
            })

            # v238.0: Broadcast tracked context for cross-agent awareness
            try:
                await self.broadcast(
                    message_type=MessageType.CONTEXT_UPDATE,
                    payload={
                        "type": "context_tracked",
                        "context_type": context_type,
                        "source": message.from_agent,
                    },
                    priority=MessagePriority.LOW,
                )
            except Exception:
                pass  # Best-effort broadcast
