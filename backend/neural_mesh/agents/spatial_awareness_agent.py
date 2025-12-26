"""
JARVIS Neural Mesh - Spatial Awareness Agent
=============================================

The "Body" of the Grand Unification - provides 3D OS Awareness (Proprioception)
to all agents in the Neural Mesh.

This agent wraps the Proprioception system and exposes:
- get_spatial_context: Know which Space, Window, and App is active
- switch_to_app: Smart app switching with Yabai teleportation
- find_window: Locate any window across all Spaces
- get_app_locations: Map of all apps and their Spaces

Other agents (like GoogleWorkspaceAgent) can request these services:
    await self.request(
        to_agent="spatial_awareness_agent",
        payload={"action": "switch_to_app", "app_name": "Calendar"}
    )

This enables true multi-agent workflows where the "Chief of Staff"
(Google Agent) can ask the "Body" (Spatial Agent) to navigate to apps.

Author: JARVIS AI System
Version: 1.0.0 - Grand Unification
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    KnowledgeType,
    MessageType,
    MessagePriority,
)

logger = logging.getLogger(__name__)


# Import proprioception functions (lazy to avoid circular imports)
def _get_proprioception_functions():
    """Lazy import of proprioception functions."""
    try:
        from core.computer_use_bridge import (
            get_current_context,
            switch_to_app_smart,
            get_spatial_manager,
            SpatialContext,
            SwitchOperation,
            SwitchResult,
        )
        return {
            "get_current_context": get_current_context,
            "switch_to_app_smart": switch_to_app_smart,
            "get_spatial_manager": get_spatial_manager,
            "SpatialContext": SpatialContext,
            "SwitchOperation": SwitchOperation,
            "SwitchResult": SwitchResult,
        }
    except ImportError as e:
        logger.warning(f"Proprioception functions not available: {e}")
        return None


@dataclass
class SpatialAwarenessConfig:
    """Configuration for Spatial Awareness Agent."""
    # Cache settings
    context_cache_ttl_seconds: float = 2.0  # How long to cache spatial context
    # Voice narration
    narrate_switches: bool = True  # Speak when switching apps
    # Cross-repo sharing
    share_context_cross_repo: bool = True  # Write to ~/.jarvis/cross_repo/


class SpatialAwarenessAgent(BaseNeuralMeshAgent):
    """
    Spatial Awareness Agent - The "Body" of JARVIS.

    Provides 3D OS Awareness (Proprioception) to all Neural Mesh agents:
    - Knows which macOS Space is active (1-16)
    - Knows which Window is focused
    - Can teleport to any app across Spaces via Yabai
    - Shares spatial state with other repos (Prime, Reactor Core)

    Capabilities:
    - get_spatial_context: Get current spatial awareness
    - switch_to_app: Smart switch to any app
    - find_window: Locate a window
    - get_app_locations: Get map of all apps -> Spaces

    Usage from other agents:
        result = await coordinator.request(
            to_agent="spatial_awareness_agent",
            payload={"action": "switch_to_app", "app_name": "Calendar"}
        )

    Example flow:
        User: "Check my calendar"
        1. GoogleWorkspaceAgent receives query
        2. GoogleWorkspaceAgent requests spatial_awareness_agent to switch to Calendar
        3. SpatialAwarenessAgent uses Yabai to teleport to Calendar's Space
        4. SpatialAwarenessAgent narrates: "Switching to Calendar on Space 3"
        5. GoogleWorkspaceAgent continues with Calendar operations
    """

    def __init__(self, config: Optional[SpatialAwarenessConfig] = None) -> None:
        """Initialize the Spatial Awareness Agent."""
        super().__init__(
            agent_name="spatial_awareness_agent",
            agent_type="spatial",  # New agent type for spatial awareness
            capabilities={
                "get_spatial_context",
                "switch_to_app",
                "find_window",
                "get_app_locations",
                "proprioception",  # Meta capability
                "3d_os_awareness",  # Meta capability
            },
            version="1.0.0",
        )

        self.config = config or SpatialAwarenessConfig()
        self._proprioception = None  # Lazy load
        self._initialized = False

        # Statistics
        self._context_queries = 0
        self._app_switches = 0
        self._successful_switches = 0
        self._failed_switches = 0
        self._space_teleports = 0

        # Cache
        self._last_context = None
        self._last_context_time = 0.0

    async def on_initialize(self) -> None:
        """Initialize agent resources."""
        logger.info("Initializing SpatialAwarenessAgent v1.0.0 (Grand Unification)")

        # Load proprioception functions
        self._proprioception = _get_proprioception_functions()

        if self._proprioception:
            logger.info("Proprioception functions loaded successfully")
            # Initialize the spatial manager
            try:
                manager = await self._proprioception["get_spatial_manager"]()
                if manager:
                    self._initialized = True
                    logger.info("Spatial Manager initialized - 3D OS Awareness active")
            except Exception as e:
                logger.warning(f"Spatial Manager init failed: {e}")
        else:
            logger.warning(
                "Proprioception not available - SpatialAwarenessAgent limited"
            )

        # Subscribe to spatial-related messages (only if connected to message bus)
        if self.message_bus:
            try:
                await self.subscribe(
                    MessageType.CUSTOM,
                    self._handle_spatial_message,
                )
            except RuntimeError:
                logger.debug("Message bus not available for subscription")

        # Announce availability to other agents (only if connected)
        if self.message_bus:
            try:
                await self.broadcast(
                    message_type=MessageType.ANNOUNCEMENT,
                    payload={
                        "agent": self.agent_name,
                        "event": "agent_ready",
                        "capabilities": list(self.capabilities),
                        "proprioception_available": self._initialized,
                    },
                )
            except RuntimeError:
                logger.debug("Message bus not available for broadcast")

        logger.info(
            f"SpatialAwarenessAgent initialized - "
            f"Proprioception: {'ACTIVE' if self._initialized else 'INACTIVE'}"
        )

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info("SpatialAwarenessAgent started - ready for spatial operations")

        # Get initial spatial context
        if self._initialized:
            try:
                context = await self._get_spatial_context()
                if context:
                    logger.info(
                        f"Initial context: Space {context.get('current_space_id')}, "
                        f"App: {context.get('focused_app')}"
                    )
            except Exception as e:
                logger.debug(f"Initial context fetch failed: {e}")

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info(
            f"SpatialAwarenessAgent stopping - "
            f"Switches: {self._app_switches} "
            f"(Success: {self._successful_switches}, "
            f"Failed: {self._failed_switches}), "
            f"Teleports: {self._space_teleports}"
        )

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """
        Execute a spatial awareness task.

        Supported actions:
        - get_spatial_context: Get current 3D OS context
        - switch_to_app: Switch to an app (with Yabai teleportation)
        - find_window: Find which Space an app is on
        - get_app_locations: Get map of all apps -> Spaces
        """
        action = payload.get("action", "")

        logger.debug(f"SpatialAwarenessAgent executing: {action}")

        if action == "get_spatial_context":
            return await self._get_spatial_context()
        elif action == "switch_to_app":
            app_name = payload.get("app_name", "")
            narrate = payload.get("narrate", self.config.narrate_switches)
            return await self._switch_to_app(app_name, narrate=narrate)
        elif action == "find_window":
            app_name = payload.get("app_name", "")
            return await self._find_window(app_name)
        elif action == "get_app_locations":
            return await self._get_app_locations()
        else:
            raise ValueError(f"Unknown spatial action: {action}")

    async def _get_spatial_context(self) -> Dict[str, Any]:
        """Get current spatial context (proprioception)."""
        self._context_queries += 1

        if not self._proprioception:
            return {
                "error": "Proprioception not available",
                "proprioception_active": False,
            }

        # Check cache
        import time
        now = time.time()
        if (
            self._last_context
            and (now - self._last_context_time) < self.config.context_cache_ttl_seconds
        ):
            return self._last_context

        try:
            get_context = self._proprioception["get_current_context"]
            context = await get_context()

            if context:
                # Convert to dict for serialization
                result = {
                    "current_space_id": context.current_space_id,
                    "current_display_id": context.current_display_id,
                    "total_spaces": context.total_spaces,
                    "focused_app": context.focused_app,
                    "focused_window": (
                        asdict(context.focused_window)
                        if context.focused_window
                        else None
                    ),
                    "app_locations": dict(context.app_locations),
                    "timestamp": context.timestamp,
                    "proprioception_active": True,
                    "context_prompt": context.get_context_prompt(),
                }

                # Cache
                self._last_context = result
                self._last_context_time = now

                # Store in knowledge graph
                if self.knowledge_graph:
                    await self.add_knowledge(
                        knowledge_type=KnowledgeType.OBSERVATION,
                        data={
                            "type": "spatial_context",
                            "space_id": context.current_space_id,
                            "focused_app": context.focused_app,
                            "total_windows": sum(
                                len(spaces) for spaces in context.app_locations.values()
                            ),
                            "timestamp": context.timestamp,
                        },
                        confidence=1.0,
                        ttl_seconds=60,  # Short TTL - spatial context changes often
                    )

                return result
            else:
                return {
                    "error": "Failed to get spatial context",
                    "proprioception_active": False,
                }

        except Exception as e:
            logger.exception(f"Error getting spatial context: {e}")
            return {
                "error": str(e),
                "proprioception_active": False,
            }

    async def _switch_to_app(
        self,
        app_name: str,
        narrate: bool = True,
    ) -> Dict[str, Any]:
        """
        Smart switch to an app using Yabai teleportation.

        This is the key capability that other agents use to navigate the OS.
        """
        self._app_switches += 1

        if not app_name:
            return {"error": "app_name is required", "success": False}

        if not self._proprioception:
            return {
                "error": "Proprioception not available",
                "success": False,
                "proprioception_active": False,
            }

        try:
            switch_fn = self._proprioception["switch_to_app_smart"]
            SwitchResult = self._proprioception["SwitchResult"]

            result = await switch_fn(app_name, narrate=narrate)

            # Check success
            is_success = result.result in (
                SwitchResult.SUCCESS,
                SwitchResult.ALREADY_FOCUSED,
                SwitchResult.SWITCHED_SPACE,
                SwitchResult.LAUNCHED_APP,
            )

            if is_success:
                self._successful_switches += 1
                if result.from_space != result.to_space:
                    self._space_teleports += 1

            else:
                self._failed_switches += 1

            # Invalidate context cache (we just moved)
            self._last_context = None

            response = {
                "success": is_success,
                "result": result.result.value,
                "app_name": result.app_name,
                "from_space": result.from_space,
                "to_space": result.to_space,
                "window_id": result.window_id,
                "execution_time_ms": result.execution_time_ms,
                "narration": result.narration,
                "space_changed": result.from_space != result.to_space,
            }

            # Log for observability
            if is_success:
                logger.info(
                    f"Switched to {app_name}: {result.result.value} "
                    f"(Space {result.from_space} -> {result.to_space})"
                )

                # Store in knowledge graph
                if self.knowledge_graph:
                    await self.add_knowledge(
                        knowledge_type=KnowledgeType.OBSERVATION,
                        data={
                            "type": "app_switch",
                            "app_name": app_name,
                            "from_space": result.from_space,
                            "to_space": result.to_space,
                            "result": result.result.value,
                            "timestamp": datetime.now().isoformat(),
                        },
                        confidence=1.0,
                    )
            else:
                logger.warning(f"Failed to switch to {app_name}: {result.result.value}")

            return response

        except Exception as e:
            self._failed_switches += 1
            logger.exception(f"Error switching to {app_name}: {e}")
            return {
                "success": False,
                "error": str(e),
                "app_name": app_name,
            }

    async def _find_window(self, app_name: str) -> Dict[str, Any]:
        """Find which Space(s) an app is on."""
        if not app_name:
            return {"error": "app_name is required"}

        context = await self._get_spatial_context()

        if context.get("error"):
            return context

        app_locations = context.get("app_locations", {})

        # Case-insensitive search
        for app, spaces in app_locations.items():
            if app.lower() == app_name.lower():
                return {
                    "app_name": app,
                    "spaces": spaces,
                    "found": True,
                    "is_focused": context.get("focused_app", "").lower() == app_name.lower(),
                    "current_space": context.get("current_space_id"),
                }

        return {
            "app_name": app_name,
            "spaces": [],
            "found": False,
            "is_running": False,
        }

    async def _get_app_locations(self) -> Dict[str, Any]:
        """Get map of all apps and their Spaces."""
        context = await self._get_spatial_context()

        if context.get("error"):
            return context

        return {
            "app_locations": context.get("app_locations", {}),
            "total_spaces": context.get("total_spaces", 0),
            "current_space": context.get("current_space_id"),
        }

    async def _handle_spatial_message(self, message: AgentMessage) -> None:
        """Handle incoming spatial-related messages from other agents."""
        payload = message.payload

        # Check if this is a spatial request
        if payload.get("type") != "spatial_request":
            return

        action = payload.get("action")
        if not action:
            return

        logger.info(
            f"Received spatial request from {message.from_agent}: {action}"
        )

        try:
            # Execute the action
            result = await self.execute_task(payload)

            # Send response
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "spatial_response",
                        "action": action,
                        "result": result,
                    },
                    from_agent=self.agent_name,
                )

        except Exception as e:
            logger.exception(f"Error handling spatial message: {e}")
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "spatial_response",
                        "action": action,
                        "error": str(e),
                    },
                    from_agent=self.agent_name,
                )

    # =========================================================================
    # Convenience methods for direct access
    # =========================================================================

    async def get_context(self) -> Dict[str, Any]:
        """Quick method to get current spatial context."""
        return await self.execute_task({"action": "get_spatial_context"})

    async def switch_to(
        self,
        app_name: str,
        narrate: bool = True,
    ) -> Dict[str, Any]:
        """Quick method to switch to an app."""
        return await self.execute_task({
            "action": "switch_to_app",
            "app_name": app_name,
            "narrate": narrate,
        })

    async def where_is(self, app_name: str) -> Dict[str, Any]:
        """Quick method to find an app."""
        return await self.execute_task({
            "action": "find_window",
            "app_name": app_name,
        })

    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics."""
        return {
            "context_queries": self._context_queries,
            "app_switches": self._app_switches,
            "successful_switches": self._successful_switches,
            "failed_switches": self._failed_switches,
            "space_teleports": self._space_teleports,
            "success_rate": (
                self._successful_switches / self._app_switches
                if self._app_switches > 0
                else 1.0
            ),
            "proprioception_active": self._initialized,
            "capabilities": list(self.capabilities),
            "version": "1.0.0",
        }


# =============================================================================
# Factory function for agent registration
# =============================================================================

async def create_spatial_awareness_agent(
    config: Optional[SpatialAwarenessConfig] = None,
) -> SpatialAwarenessAgent:
    """
    Create a Spatial Awareness Agent.

    This is the factory function used by AgentInitializer.

    Args:
        config: Optional configuration

    Returns:
        Configured SpatialAwarenessAgent
    """
    return SpatialAwarenessAgent(config=config)
