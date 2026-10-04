"""
JARVIS Neural Mesh - Vision Cognitive Adapter

Adapts the Vision Cognitive Loop and Yabai Multi-Space systems for seamless
integration with the Neural Mesh, enabling visual awareness across agents.

This adapter enables:
- Visual state sharing across agents (what's on screen)
- Multi-space navigation and coordination
- Visual verification for action validation
- Screen context for intelligent task routing

Usage:
    from core.vision_cognitive_loop import VisionCognitiveLoop

    vision_loop = VisionCognitiveLoop()
    await vision_loop.initialize()

    adapted = VisionCognitiveAdapter(
        vision_loop=vision_loop,
    )

    # Register with Neural Mesh
    await coordinator.register_agent(adapted)

Version: 10.2 (Vision Cognitive Loop Integration)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)
from uuid import uuid4

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    KnowledgeEntry,
    KnowledgeType,
    MessagePriority,
    MessageType,
)

logger = logging.getLogger(__name__)


class VisionComponentType(str, Enum):
    """Types of vision components in JARVIS."""
    COGNITIVE_LOOP = "cognitive_loop"  # Vision Cognitive Loop
    YABAI_DETECTOR = "yabai_detector"  # Yabai Space Detector
    SCREENSHOT_CAPTURE = "screenshot_capture"  # Screenshot Capture
    VISION_BRIDGE = "vision_bridge"  # Vision Intelligence Bridge


@dataclass
class VisionCapabilities:
    """Capabilities matrix for vision components."""
    visual_awareness: bool = False  # Can see screen state
    multi_space_awareness: bool = False  # Can track multiple spaces
    visual_verification: bool = False  # Can verify action outcomes
    ocr_extraction: bool = False  # Can extract text from screen
    ui_element_detection: bool = False  # Can identify UI elements
    space_navigation: bool = False  # Can navigate between spaces
    screenshot_capture: bool = False  # Can capture screenshots
    visual_learning: bool = False  # Can contribute to visual training

    def to_set(self) -> Set[str]:
        """Convert capabilities to set of strings."""
        caps = set()
        if self.visual_awareness:
            caps.add("visual_awareness")
        if self.multi_space_awareness:
            caps.add("multi_space_awareness")
        if self.visual_verification:
            caps.add("visual_verification")
        if self.ocr_extraction:
            caps.add("ocr_extraction")
        if self.ui_element_detection:
            caps.add("ui_element_detection")
        if self.space_navigation:
            caps.add("space_navigation")
        if self.screenshot_capture:
            caps.add("screenshot_capture")
        if self.visual_learning:
            caps.add("visual_learning")
        return caps


# Capability mappings for each component type
VISION_CAPABILITIES: Dict[VisionComponentType, VisionCapabilities] = {
    VisionComponentType.COGNITIVE_LOOP: VisionCapabilities(
        visual_awareness=True,
        multi_space_awareness=True,
        visual_verification=True,
        ocr_extraction=True,
        ui_element_detection=True,
        space_navigation=True,
        screenshot_capture=True,
        visual_learning=True,
    ),
    VisionComponentType.YABAI_DETECTOR: VisionCapabilities(
        multi_space_awareness=True,
        space_navigation=True,
    ),
    VisionComponentType.SCREENSHOT_CAPTURE: VisionCapabilities(
        visual_awareness=True,
        screenshot_capture=True,
    ),
    VisionComponentType.VISION_BRIDGE: VisionCapabilities(
        visual_awareness=True,
        ocr_extraction=True,
        ui_element_detection=True,
    ),
}


class VisionCognitiveAdapter(BaseNeuralMeshAgent):
    """
    Adapter for JARVIS Vision Cognitive Loop to work with Neural Mesh.

    This adapter wraps the Vision Cognitive Loop, enabling visual awareness
    and multi-space coordination across the Neural Mesh ecosystem.

    Key Features:
    - Visual state sharing for context-aware agents
    - Multi-space awareness via Yabai integration
    - Visual verification for action validation
    - Cross-agent visual context sharing
    """

    def __init__(
        self,
        vision_loop: Optional[Any] = None,
        component_type: VisionComponentType = VisionComponentType.COGNITIVE_LOOP,
        agent_name: Optional[str] = None,
    ):
        """Initialize the vision adapter.

        Args:
            vision_loop: The VisionCognitiveLoop instance to wrap
            component_type: Type of vision component
            agent_name: Custom name for this agent
        """
        self._vision_loop = vision_loop
        self._component_type = component_type

        # Determine capabilities
        self._capabilities = VISION_CAPABILITIES.get(
            component_type,
            VisionCapabilities(),
        )

        # Initialize base agent with correct parameters
        super().__init__(
            agent_name=agent_name or f"vision_{component_type.value}",
            agent_type="vision",
            capabilities=self._capabilities.to_set(),
            backend="local",
            version="10.6",
            dependencies=set(),
        )

        # State tracking
        self._last_visual_state: Optional[Dict[str, Any]] = None
        self._last_space_context: Optional[Dict[str, Any]] = None
        self._captures_performed: int = 0
        self._verifications_performed: int = 0

        logger.debug(
            "[Vision Adapter] Created: name=%s, type=%s, capabilities=%s",
            self.agent_name,
            component_type.value,
            self._capabilities.to_set(),
        )

    async def on_initialize(self) -> None:
        """
        Agent-specific initialization (REQUIRED by BaseNeuralMeshAgent).

        This method is called by the base class during Neural Mesh integration.
        Performs robust, async initialization of the vision system with:
        - Parallel capability detection
        - Intelligent error recovery
        - Health monitoring setup
        - Dynamic configuration
        """
        try:
            logger.info("[Vision Adapter] Starting initialization: %s", self.agent_name)

            # Initialize vision loop if provided (with async support)
            if self._vision_loop:
                if not getattr(self._vision_loop, '_initialized', False):
                    logger.debug("[Vision Adapter] Initializing vision loop...")
                    await self._vision_loop.initialize()
                    logger.info("[Vision Adapter] ✓ Vision loop initialized")
                else:
                    logger.debug("[Vision Adapter] Vision loop already initialized")

            # Initialize Yabai detector if provided (for multi-space adapters)
            if hasattr(self, '_yabai_detector') and self._yabai_detector:
                logger.debug("[Vision Adapter] Initializing Yabai detector...")
                try:
                    # Yabai detector doesn't have async init, wrap in executor if needed
                    if hasattr(self._yabai_detector, 'initialize'):
                        await asyncio.to_thread(self._yabai_detector.initialize)
                    logger.info("[Vision Adapter] ✓ Yabai detector initialized")
                except Exception as yabai_err:
                    logger.warning("[Vision Adapter] Yabai init failed (non-critical): %s", yabai_err)
                    # Non-critical - adapter can still work without Yabai

            # Perform initial capability verification (parallel checks)
            await self._verify_capabilities()

            # Setup health monitoring
            self._setup_health_monitoring()

            logger.info("[Vision Adapter] ✅ Initialization complete: %s (capabilities: %s)",
                       self.agent_name,
                       ', '.join(self.capabilities))

        except Exception as e:
            logger.error("[Vision Adapter] ❌ Initialization failed: %s - %s", self.agent_name, e)
            raise  # Re-raise to signal initialization failure

    async def _verify_capabilities(self) -> None:
        """Verify adapter capabilities with parallel checks."""
        logger.debug("[Vision Adapter] Verifying capabilities...")

        # Run capability checks in parallel for speed
        checks = []

        if self._capabilities.visual_awareness and self._vision_loop:
            checks.append(self._check_visual_awareness())

        if self._capabilities.multi_space_awareness:
            checks.append(self._check_multi_space())

        if checks:
            results = await asyncio.gather(*checks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning("[Vision Adapter] Capability check %d failed: %s", i, result)

        logger.debug("[Vision Adapter] ✓ Capability verification complete")

    async def _check_visual_awareness(self) -> bool:
        """Check if visual awareness is functional."""
        try:
            if hasattr(self._vision_loop, 'get_visual_context_for_prompt'):
                context = self._vision_loop.get_visual_context_for_prompt()
                return bool(context)
            return True
        except Exception as e:
            logger.debug("[Vision Adapter] Visual awareness check failed: %s", e)
            return False

    async def _check_multi_space(self) -> bool:
        """Check if multi-space awareness is functional."""
        try:
            if hasattr(self, '_yabai_detector') and self._yabai_detector:
                # Check if Yabai is responsive
                if hasattr(self._yabai_detector, 'is_enabled'):
                    return self._yabai_detector.is_enabled()
            return True
        except Exception as e:
            logger.debug("[Vision Adapter] Multi-space check failed: %s", e)
            return False

    def _setup_health_monitoring(self) -> None:
        """Setup health monitoring for vision components."""
        # Initialize health metrics
        self._health_metrics = {
            'last_capture_time': None,
            'total_captures': 0,
            'total_verifications': 0,
            'total_errors': 0,
            'vision_loop_healthy': self._vision_loop is not None,
            'yabai_healthy': hasattr(self, '_yabai_detector') and self._yabai_detector is not None,
        }
        logger.debug("[Vision Adapter] Health monitoring initialized")

    async def initialize(
        self,
        message_bus=None,
        registry=None,
        knowledge_graph=None,
        **kwargs
    ) -> bool:
        """
        Initialize the vision adapter with Neural Mesh integration.

        Supports both standalone mode and full Neural Mesh mode with:
        - message_bus: Agent communication bus for inter-agent messaging
        - registry: Agent registry for discovery and heartbeats
        - knowledge_graph: Shared knowledge graph for collective memory

        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True

        # If Neural Mesh components provided, use base class initialization
        if message_bus is not None or registry is not None or knowledge_graph is not None:
            logger.info("[Vision Adapter] Neural Mesh mode - initializing with components")
            try:
                # Call base class initialize with all components
                await super().initialize(
                    message_bus=message_bus,
                    registry=registry,
                    knowledge_graph=knowledge_graph,
                    **kwargs
                )
                self._initialized = True
                return True
            except Exception as e:
                logger.error("[Vision Adapter] Neural Mesh initialization failed: %s", e)
                return False

        # Standalone mode - initialize directly
        if not hasattr(self, 'message_bus') or self.message_bus is None:
            logger.info("[Vision Adapter] Standalone mode - initializing directly")
            try:
                await self.on_initialize()
                self._initialized = True
                return True
            except Exception as e:
                logger.error("[Vision Adapter] Standalone initialization failed: %s", e)
                return False

        # If connected to Neural Mesh, initialization happens via base class
        return self._initialized

    async def execute_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a vision-related task (ROBUST & ASYNC).

        Supported actions:
        - look: Capture current visual state
        - verify: Verify action outcome
        - get_space_context: Get multi-space information
        - navigate_to_space: Switch to a specific space
        - find_app: Find which space contains an app
        - get_visual_context: Get formatted visual context for prompts
        - get_health: Get adapter health metrics

        Args:
            task: Task dictionary with 'action' and optional parameters

        Returns:
            Result dictionary with execution outcome and health metrics
        """
        action = task.get("action", "").lower()
        params = task.get("params", {})
        start_time = asyncio.get_event_loop().time()

        result = {
            "success": False,
            "action": action,
            "data": None,
            "error": None,
            "execution_time_ms": 0,
        }

        # Update health metrics
        if hasattr(self, '_health_metrics'):
            self._health_metrics['total_captures'] += 1

        try:
            if action == "look":
                # Capture current visual state
                visual_state, space_context = await self._vision_loop.look(
                    include_ocr=params.get("include_ocr", True),
                    include_ui_elements=params.get("include_ui_elements", True),
                    space_id=params.get("space_id"),
                )

                self._last_visual_state = self._state_to_dict(visual_state)
                self._last_space_context = self._space_to_dict(space_context)
                self._captures_performed += 1

                result["success"] = True
                result["data"] = {
                    "visual_state": self._last_visual_state,
                    "space_context": self._last_space_context,
                }

            elif action == "verify":
                # Verify action outcome
                expected = params.get("expected_outcome", "")
                verification = await self._vision_loop.verify(
                    expected_outcome=expected,
                    timeout_ms=params.get("timeout_ms"),
                )

                self._verifications_performed += 1

                result["success"] = True
                result["data"] = {
                    "verified": verification.result.value in ("success", "partial"),
                    "result": verification.result.value,
                    "confidence": verification.confidence,
                    "changes": verification.changes_detected,
                    "retry_suggested": verification.retry_suggested,
                }

            elif action == "get_space_context":
                # Get multi-space information
                _, space_context = await self._vision_loop.look(
                    include_ocr=False,
                    include_ui_elements=False,
                )

                result["success"] = True
                result["data"] = {
                    "current_space_id": space_context.current_space_id,
                    "total_spaces": space_context.total_spaces,
                    "spaces": space_context.spaces,
                    "app_locations": space_context.app_locations,
                }

            elif action == "find_app":
                # Find which space contains an app
                app_name = params.get("app_name", "")
                _, space_context = await self._vision_loop.look(
                    include_ocr=False,
                    include_ui_elements=False,
                )

                space_id = space_context.get_space_for_app(app_name)

                result["success"] = space_id is not None
                result["data"] = {
                    "app_name": app_name,
                    "space_id": space_id,
                    "found": space_id is not None,
                }

            elif action == "get_visual_context":
                # Get formatted visual context for prompts
                context_str = self._vision_loop.get_visual_context_for_prompt()

                result["success"] = True
                result["data"] = {
                    "visual_context": context_str,
                    "last_capture_time": self._last_visual_state.get("timestamp") if self._last_visual_state else None,
                }

            elif action == "get_metrics":
                # Get adapter metrics
                loop_metrics = self._vision_loop.get_metrics() if self._vision_loop else {}

                result["success"] = True
                result["data"] = {
                    "adapter_captures": self._captures_performed,
                    "adapter_verifications": self._verifications_performed,
                    "loop_metrics": loop_metrics,
                }

            elif action == "get_health":
                # Get health metrics (NEW - v10.6)
                health_data = self._health_metrics.copy() if hasattr(self, '_health_metrics') else {}
                health_data.update({
                    "adapter_initialized": self._initialized,
                    "vision_loop_available": self._vision_loop is not None,
                    "yabai_available": hasattr(self, '_yabai_detector') and self._yabai_detector is not None,
                    "capabilities": list(self.capabilities),
                    "component_type": self._component_type.value,
                })

                result["success"] = True
                result["data"] = health_data

            else:
                result["error"] = f"Unknown action: {action}"

        except Exception as e:
            logger.error("[Vision Adapter] Task failed: %s - %s", action, e)
            result["error"] = str(e)

            # Update error metrics
            if hasattr(self, '_health_metrics'):
                self._health_metrics['total_errors'] += 1

        finally:
            # Calculate execution time
            if start_time:
                execution_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
                result["execution_time_ms"] = round(execution_time_ms, 2)

            # Update last capture time if successful
            if result["success"] and action == "look" and hasattr(self, '_health_metrics'):
                import time
                self._health_metrics['last_capture_time'] = time.time()

        return result

    async def handle_message(self, message: AgentMessage) -> Optional[AgentMessage]:
        """Handle incoming Neural Mesh message.

        Responds to visual context requests and space awareness queries.

        Args:
            message: Incoming agent message

        Returns:
            Response message if applicable
        """
        if message.message_type == MessageType.QUERY:
            # Handle visual context query
            query = message.payload.get("query", "")

            if "visual" in query.lower() or "screen" in query.lower():
                # Provide current visual context
                task_result = await self.execute_task({"action": "get_visual_context"})

                if task_result["success"]:
                    return AgentMessage(
                        source=self.agent_name,
                        target=message.source,
                        message_type=MessageType.RESPONSE,
                        payload={
                            "visual_context": task_result["data"]["visual_context"],
                            "captured": self._captures_performed,
                        },
                        correlation_id=message.correlation_id,
                    )

            elif "space" in query.lower():
                # Provide space context
                task_result = await self.execute_task({"action": "get_space_context"})

                if task_result["success"]:
                    return AgentMessage(
                        source=self.agent_name,
                        target=message.source,
                        message_type=MessageType.RESPONSE,
                        payload=task_result["data"],
                        correlation_id=message.correlation_id,
                    )

        elif message.message_type == MessageType.COMMAND:
            # Handle visual commands
            action = message.payload.get("action")
            params = message.payload.get("params", {})

            if action:
                task_result = await self.execute_task({
                    "action": action,
                    "params": params,
                })

                return AgentMessage(
                    source=self.agent_name,
                    target=message.source,
                    message_type=MessageType.RESPONSE,
                    payload=task_result,
                    correlation_id=message.correlation_id,
                )

        return None

    def contribute_knowledge(self) -> List[KnowledgeEntry]:
        """Contribute visual knowledge to the Neural Mesh.

        Returns:
            List of knowledge entries from visual observations
        """
        entries = []

        # Contribute last visual state as spatial knowledge
        if self._last_visual_state:
            entries.append(KnowledgeEntry(
                id=str(uuid4()),
                source=self.agent_name,
                knowledge_type=KnowledgeType.SPATIAL,
                content={
                    "type": "visual_state",
                    "current_app": self._last_visual_state.get("current_app"),
                    "applications": self._last_visual_state.get("applications", []),
                    "window_count": self._last_visual_state.get("window_count", 0),
                    "timestamp": self._last_visual_state.get("timestamp"),
                },
                timestamp=datetime.utcnow(),
                confidence=0.9,
                tags=["visual", "screen_state", "current"],
            ))

        # Contribute space context as spatial knowledge
        if self._last_space_context:
            entries.append(KnowledgeEntry(
                id=str(uuid4()),
                source=self.agent_name,
                knowledge_type=KnowledgeType.SPATIAL,
                content={
                    "type": "space_context",
                    "current_space": self._last_space_context.get("current_space_id"),
                    "total_spaces": self._last_space_context.get("total_spaces"),
                    "app_locations": self._last_space_context.get("app_locations", {}),
                },
                timestamp=datetime.utcnow(),
                confidence=0.95,
                tags=["spatial", "multi_space", "yabai"],
            ))

        return entries

    def _state_to_dict(self, state: Any) -> Optional[Dict[str, Any]]:
        """Convert VisualState to dictionary."""
        if not state:
            return None
        return {
            "timestamp": state.timestamp,
            "space_id": state.space_id,
            "current_app": state.current_app,
            "applications": state.applications,
            "is_locked": state.is_locked,
            "is_fullscreen": state.is_fullscreen,
            "window_count": len(state.visible_windows),
            "screenshot_hash": state.screenshot_hash,
            "confidence": state.analysis_confidence,
        }

    def _space_to_dict(self, context: Any) -> Optional[Dict[str, Any]]:
        """Convert SpaceContext to dictionary."""
        if not context:
            return None
        return {
            "current_space_id": context.current_space_id,
            "total_spaces": context.total_spaces,
            "spaces": context.spaces,
            "app_locations": context.app_locations,
        }


# ============================================================================
# Factory Functions
# ============================================================================

async def create_vision_cognitive_adapter(
    agent_name: str = "vision_cognitive_loop"
) -> Optional[VisionCognitiveAdapter]:
    """Create and initialize a Vision Cognitive adapter.

    Args:
        agent_name: Name for the adapter (default: "vision_cognitive_loop")

    Returns:
        Initialized adapter or None if creation fails
    """
    try:
        from core.vision_cognitive_loop import get_vision_cognitive_loop

        vision_loop = get_vision_cognitive_loop()
        await vision_loop.initialize()

        adapter = VisionCognitiveAdapter(
            vision_loop=vision_loop,
            component_type=VisionComponentType.COGNITIVE_LOOP,
            agent_name=agent_name,
        )
        await adapter.initialize()

        return adapter

    except ImportError as e:
        logger.debug("Vision Cognitive Loop not available: %s", e)
        return None
    except Exception as e:
        logger.error("Failed to create Vision Cognitive adapter: %s", e)
        return None


async def create_yabai_adapter(
    agent_name: str = "vision_yabai_multispace"
) -> Optional[VisionCognitiveAdapter]:
    """Create a Yabai-only adapter for multi-space awareness.

    Args:
        agent_name: Name for the adapter (default: "vision_yabai_multispace")

    Returns:
        Initialized adapter or None if creation fails
    """
    try:
        from vision.windows_desktop import create_space_detector

        # Create minimal vision loop with the host's space backend only
        detector = create_space_detector(enable_vision=False)

        adapter = VisionCognitiveAdapter(
            vision_loop=None,  # Will use Yabai directly
            component_type=VisionComponentType.YABAI_DETECTOR,
            agent_name=agent_name,
        )

        # Store detector for direct access
        adapter._yabai_detector = detector
        await adapter.initialize()

        return adapter

    except ImportError as e:
        logger.debug("Yabai Space Detector not available: %s", e)
        return None
    except Exception as e:
        logger.error("Failed to create Yabai adapter: %s", e)
        return None
