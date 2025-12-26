"""
JARVIS Neural Mesh - Intelligence Engine Adapter

Adapts the JARVIS Intelligence Engines (UAE, SAI, CAI, ChainOfThought,
ReasoningGraph) for seamless integration with the Neural Mesh system.

This adapter enables:
- Unified awareness and spatial intelligence sharing
- Chain-of-thought reasoning distributed across agents
- Knowledge graph integration for collective learning
- Cross-engine coordination for complex tasks

Usage:
    # Wrap an existing UAE instance
    from intelligence.unified_awareness_engine import UnifiedAwarenessEngine

    uae = UnifiedAwarenessEngine()
    adapted = IntelligenceEngineAdapter(
        engine=uae,
        engine_type="uae",
    )

    # Register with Neural Mesh
    await coordinator.register_agent(adapted)
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC
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
    Type,
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


class IntelligenceEngineType(str, Enum):
    """Types of intelligence engines in JARVIS."""
    UAE = "uae"  # Unified Awareness Engine
    SAI = "sai"  # Spatial Awareness Intelligence
    CAI = "cai"  # Contextual Awareness Intelligence
    COT = "cot"  # Chain of Thought Engine
    RGE = "rge"  # Reasoning Graph Engine
    PIE = "pie"  # Proactive Intelligence Engine


@dataclass
class IntelligenceCapabilities:
    """Capabilities matrix for intelligence engines."""
    spatial_awareness: bool = False
    contextual_understanding: bool = False
    chain_of_thought: bool = False
    reasoning_graphs: bool = False
    proactive_suggestions: bool = False
    workspace_learning: bool = False
    natural_communication: bool = False
    goal_tracking: bool = False

    def to_set(self) -> Set[str]:
        """Convert capabilities to set of strings."""
        caps = set()
        if self.spatial_awareness:
            caps.add("spatial_awareness")
        if self.contextual_understanding:
            caps.add("contextual_understanding")
        if self.chain_of_thought:
            caps.add("chain_of_thought")
        if self.reasoning_graphs:
            caps.add("reasoning_graphs")
        if self.proactive_suggestions:
            caps.add("proactive_suggestions")
        if self.workspace_learning:
            caps.add("workspace_learning")
        if self.natural_communication:
            caps.add("natural_communication")
        if self.goal_tracking:
            caps.add("goal_tracking")
        return caps


# Capability mappings for each engine type
ENGINE_CAPABILITIES: Dict[IntelligenceEngineType, IntelligenceCapabilities] = {
    IntelligenceEngineType.UAE: IntelligenceCapabilities(
        spatial_awareness=True,
        contextual_understanding=True,
        proactive_suggestions=True,
        workspace_learning=True,
        natural_communication=True,
        goal_tracking=True,
    ),
    IntelligenceEngineType.SAI: IntelligenceCapabilities(
        spatial_awareness=True,
        workspace_learning=True,
    ),
    IntelligenceEngineType.CAI: IntelligenceCapabilities(
        contextual_understanding=True,
        proactive_suggestions=True,
    ),
    IntelligenceEngineType.COT: IntelligenceCapabilities(
        chain_of_thought=True,
        reasoning_graphs=True,
    ),
    IntelligenceEngineType.RGE: IntelligenceCapabilities(
        reasoning_graphs=True,
        chain_of_thought=True,
    ),
    IntelligenceEngineType.PIE: IntelligenceCapabilities(
        proactive_suggestions=True,
        contextual_understanding=True,
        goal_tracking=True,
    ),
}


class IntelligenceEngineAdapter(BaseNeuralMeshAgent):
    """
    Adapter for JARVIS Intelligence Engines to work with Neural Mesh.

    This adapter wraps UAE, SAI, CAI, ChainOfThought, and ReasoningGraph
    engines, exposing their capabilities through the Neural Mesh interface.

    Key Features:
    - Automatic capability detection from engine type
    - Async-first with sync fallback for legacy engines
    - Knowledge graph integration for insights sharing
    - Cross-engine coordination via message bus
    - Lazy initialization for resource efficiency

    Example - Wrapping UAE:
        uae = UnifiedAwarenessEngine()
        await uae.initialize()

        adapter = IntelligenceEngineAdapter(
            engine=uae,
            engine_type=IntelligenceEngineType.UAE,
        )
        await coordinator.register_agent(adapter)

    Example - Requesting spatial analysis:
        result = await adapter.execute_task({
            "action": "analyze_workspace",
            "input": {"space_id": 3, "include_windows": True}
        })
    """

    def __init__(
        self,
        engine: Any,
        engine_type: Union[IntelligenceEngineType, str],
        agent_name: Optional[str] = None,
        additional_capabilities: Optional[Set[str]] = None,
        version: str = "1.0.0",
    ) -> None:
        """Initialize the intelligence engine adapter.

        Args:
            engine: The intelligence engine instance to wrap
            engine_type: Type of engine (UAE, SAI, CAI, etc.)
            agent_name: Optional custom name (defaults to engine type)
            additional_capabilities: Extra capabilities beyond defaults
            version: Adapter version
        """
        # Normalize engine type
        if isinstance(engine_type, str):
            engine_type = IntelligenceEngineType(engine_type.lower())

        self._engine_type = engine_type
        self._engine = engine

        # Get capabilities for this engine type
        caps = ENGINE_CAPABILITIES.get(
            engine_type,
            IntelligenceCapabilities()
        )
        capabilities = caps.to_set()

        # Add any additional capabilities
        if additional_capabilities:
            capabilities.update(additional_capabilities)

        # Set agent name
        name = agent_name or f"intelligence_{engine_type.value}"

        super().__init__(
            agent_name=name,
            agent_type="intelligence",
            capabilities=capabilities,
            backend="local",
            version=version,
        )

        # Task handlers mapped by action
        self._task_handlers: Dict[str, Callable] = {}
        self._setup_handlers()

        # Cache for engine state
        self._last_analysis: Optional[Dict[str, Any]] = None
        self._analysis_cache_ttl = 5.0  # seconds
        self._last_analysis_time: float = 0

    def _setup_handlers(self) -> None:
        """Setup action handlers based on engine type."""
        # Common handlers
        self._task_handlers["get_status"] = self._handle_get_status
        self._task_handlers["get_insights"] = self._handle_get_insights

        # Engine-specific handlers
        if self._engine_type in (
            IntelligenceEngineType.UAE,
            IntelligenceEngineType.SAI,
        ):
            self._task_handlers["analyze_workspace"] = self._handle_analyze_workspace
            self._task_handlers["get_spatial_context"] = self._handle_spatial_context
            self._task_handlers["suggest_organization"] = self._handle_suggest_organization

        if self._engine_type == IntelligenceEngineType.UAE:
            self._task_handlers["process_query"] = self._handle_process_query
            self._task_handlers["get_awareness_state"] = self._handle_awareness_state
            self._task_handlers["update_context"] = self._handle_update_context

        if self._engine_type in (
            IntelligenceEngineType.COT,
            IntelligenceEngineType.RGE,
        ):
            self._task_handlers["reason"] = self._handle_reason
            self._task_handlers["explain"] = self._handle_explain
            self._task_handlers["analyze_problem"] = self._handle_analyze_problem

        if self._engine_type == IntelligenceEngineType.PIE:
            self._task_handlers["predict_needs"] = self._handle_predict_needs
            self._task_handlers["suggest_actions"] = self._handle_suggest_actions

    async def on_initialize(self) -> None:
        """Initialize the adapter and underlying engine."""
        logger.info(
            "Initializing IntelligenceEngineAdapter for %s",
            self._engine_type.value,
        )

        # Initialize the engine if it has an initialize method
        if hasattr(self._engine, "initialize"):
            result = self._engine.initialize()
            if asyncio.iscoroutine(result):
                await result

        # Subscribe to relevant messages
        await self.subscribe(
            MessageType.KNOWLEDGE_SHARED,
            self._handle_knowledge_shared,
        )
        await self.subscribe(
            MessageType.CONTEXT_UPDATE,
            self._handle_context_update,
        )
        await self.subscribe(
            MessageType.CUSTOM,
            self._handle_custom_message,
        )

        # Load any existing knowledge relevant to this engine
        if self.knowledge_graph:
            insights = await self.query_knowledge(
                query=f"intelligence {self._engine_type.value} insights",
                knowledge_types=[
                    KnowledgeType.INSIGHT,
                    KnowledgeType.OBSERVATION,
                ],
                limit=20,
            )
            if insights:
                logger.info(
                    "Loaded %d prior insights for %s",
                    len(insights),
                    self._engine_type.value,
                )

        logger.info(
            "IntelligenceEngineAdapter initialized: %s with capabilities %s",
            self.agent_name,
            self.capabilities,
        )

    async def on_start(self) -> None:
        """Called when agent starts processing."""
        logger.info("%s intelligence adapter started", self._engine_type.value)

        # Start any background monitoring if engine supports it
        if hasattr(self._engine, "start_monitoring"):
            result = self._engine.start_monitoring()
            if asyncio.iscoroutine(result):
                await result

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info("%s intelligence adapter stopping", self._engine_type.value)

        # Stop monitoring
        if hasattr(self._engine, "stop_monitoring"):
            result = self._engine.stop_monitoring()
            if asyncio.iscoroutine(result):
                await result

        # Cleanup engine
        for method_name in ("cleanup", "close", "shutdown", "stop"):
            if hasattr(self._engine, method_name):
                result = getattr(self._engine, method_name)()
                if asyncio.iscoroutine(result):
                    await result
                break

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """Execute an intelligence task.

        Args:
            payload: Task payload with 'action' and 'input' keys

        Returns:
            Task result from the intelligence engine
        """
        action = payload.get("action", "")
        input_data = payload.get("input", {})

        logger.debug(
            "Executing intelligence task: %s on %s",
            action,
            self._engine_type.value,
        )

        # Find handler
        handler = self._task_handlers.get(action)
        if not handler:
            # Try to find method on engine directly
            if hasattr(self._engine, action):
                handler = self._create_engine_handler(action)
            else:
                raise ValueError(
                    f"Unknown action '{action}' for {self._engine_type.value}"
                )

        # Execute handler
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(input_data)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, handler, input_data)

            # Store insight in knowledge graph
            if result and self.knowledge_graph:
                await self._store_insight(action, input_data, result)

            return result

        except Exception as e:
            logger.error(
                "Error executing %s on %s: %s",
                action,
                self._engine_type.value,
                e,
            )
            raise

    def _create_engine_handler(self, method_name: str) -> Callable:
        """Create a handler that delegates to engine method."""
        method = getattr(self._engine, method_name)

        async def handler(input_data: Dict[str, Any]) -> Any:
            if asyncio.iscoroutinefunction(method):
                return await method(**input_data)
            else:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: method(**input_data)
                )

        return handler

    async def _store_insight(
        self,
        action: str,
        input_data: Dict[str, Any],
        result: Any,
    ) -> None:
        """Store task result as knowledge insight."""
        try:
            await self.add_knowledge(
                knowledge_type=KnowledgeType.INSIGHT,
                data={
                    "engine_type": self._engine_type.value,
                    "action": action,
                    "input_summary": str(input_data)[:200],
                    "result_summary": str(result)[:500] if result else None,
                    "timestamp": datetime.utcnow().isoformat(),
                },
                tags={
                    "intelligence",
                    self._engine_type.value,
                    action,
                },
                confidence=0.8,
                ttl_seconds=3600,  # 1 hour cache
            )
        except Exception as e:
            logger.debug("Failed to store insight: %s", e)

    # =========================================================================
    # Task Handlers
    # =========================================================================

    async def _handle_get_status(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get engine status."""
        status = {
            "engine_type": self._engine_type.value,
            "agent_name": self.agent_name,
            "capabilities": list(self.capabilities),
            "running": self._running,
        }

        # Get engine-specific status
        if hasattr(self._engine, "get_status"):
            engine_status = self._engine.get_status()
            if asyncio.iscoroutine(engine_status):
                engine_status = await engine_status
            status["engine_status"] = engine_status

        return status

    async def _handle_get_insights(
        self,
        input_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Get recent insights from knowledge graph."""
        query = input_data.get("query", "insights")
        limit = input_data.get("limit", 10)

        if self.knowledge_graph:
            insights = await self.query_knowledge(
                query=f"{self._engine_type.value} {query}",
                knowledge_types=[KnowledgeType.INSIGHT],
                limit=limit,
            )
            return [
                {
                    "id": i.id,
                    "data": i.data,
                    "confidence": i.confidence,
                    "created_at": i.created_at.isoformat(),
                }
                for i in insights
            ]
        return []

    async def _handle_analyze_workspace(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Analyze workspace using spatial intelligence."""
        space_id = input_data.get("space_id")
        include_windows = input_data.get("include_windows", True)

        # Check cache
        import time
        now = time.time()
        cache_key = f"workspace_{space_id}"
        if (
            self._last_analysis
            and self._last_analysis.get("cache_key") == cache_key
            and (now - self._last_analysis_time) < self._analysis_cache_ttl
        ):
            return self._last_analysis.get("result", {})

        # Perform analysis
        result = {}

        if hasattr(self._engine, "analyze_workspace"):
            analysis = self._engine.analyze_workspace(
                space_id=space_id,
                include_windows=include_windows,
            )
            if asyncio.iscoroutine(analysis):
                analysis = await analysis
            result = analysis or {}

        elif hasattr(self._engine, "get_workspace_state"):
            state = self._engine.get_workspace_state(space_id)
            if asyncio.iscoroutine(state):
                state = await state
            result = {"workspace_state": state}

        # Cache result
        self._last_analysis = {
            "cache_key": cache_key,
            "result": result,
        }
        self._last_analysis_time = now

        # Share with other agents
        await self.broadcast(
            message_type=MessageType.KNOWLEDGE_SHARED,
            payload={
                "source": self.agent_name,
                "knowledge_type": "workspace_analysis",
                "space_id": space_id,
                "summary": result.get("summary", ""),
            },
        )

        return result

    async def _handle_spatial_context(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get spatial context for current workspace."""
        if hasattr(self._engine, "get_spatial_context"):
            context = self._engine.get_spatial_context()
            if asyncio.iscoroutine(context):
                context = await context
            return context or {}

        if hasattr(self._engine, "get_context"):
            context = self._engine.get_context()
            if asyncio.iscoroutine(context):
                context = await context
            return {"context": context}

        return {}

    async def _handle_suggest_organization(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Suggest workspace organization improvements."""
        space_id = input_data.get("space_id")

        suggestions = []

        if hasattr(self._engine, "suggest_organization"):
            result = self._engine.suggest_organization(space_id=space_id)
            if asyncio.iscoroutine(result):
                result = await result
            suggestions = result or []

        elif hasattr(self._engine, "get_suggestions"):
            result = self._engine.get_suggestions()
            if asyncio.iscoroutine(result):
                result = await result
            suggestions = result or []

        return {
            "suggestions": suggestions,
            "space_id": space_id,
        }

    async def _handle_process_query(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Process a natural language query through UAE."""
        query = input_data.get("query", "")
        context = input_data.get("context", {})

        if hasattr(self._engine, "process_query"):
            result = self._engine.process_query(query, context=context)
            if asyncio.iscoroutine(result):
                result = await result
            return {"response": result}

        if hasattr(self._engine, "process"):
            result = self._engine.process(query)
            if asyncio.iscoroutine(result):
                result = await result
            return {"response": result}

        return {"response": None, "error": "No query processor available"}

    async def _handle_awareness_state(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get current awareness state from UAE."""
        if hasattr(self._engine, "get_awareness_state"):
            state = self._engine.get_awareness_state()
            if asyncio.iscoroutine(state):
                state = await state
            return state or {}

        if hasattr(self._engine, "state"):
            return {"state": self._engine.state}

        return {}

    async def _handle_update_context(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update UAE context."""
        context_update = input_data.get("context", {})

        if hasattr(self._engine, "update_context"):
            result = self._engine.update_context(context_update)
            if asyncio.iscoroutine(result):
                result = await result
            return {"success": True, "result": result}

        return {"success": False, "error": "No context updater available"}

    async def _handle_reason(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Perform chain-of-thought reasoning."""
        problem = input_data.get("problem", "")
        steps = input_data.get("max_steps", 10)

        if hasattr(self._engine, "reason"):
            result = self._engine.reason(problem, max_steps=steps)
            if asyncio.iscoroutine(result):
                result = await result
            return {"reasoning": result}

        if hasattr(self._engine, "think"):
            result = self._engine.think(problem)
            if asyncio.iscoroutine(result):
                result = await result
            return {"reasoning": result}

        return {"reasoning": None, "error": "No reasoning engine available"}

    async def _handle_explain(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Explain reasoning for a decision."""
        decision = input_data.get("decision", "")

        if hasattr(self._engine, "explain"):
            explanation = self._engine.explain(decision)
            if asyncio.iscoroutine(explanation):
                explanation = await explanation
            return {"explanation": explanation}

        return {"explanation": None}

    async def _handle_analyze_problem(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Analyze a problem using reasoning graph."""
        problem = input_data.get("problem", "")
        depth = input_data.get("depth", 3)

        if hasattr(self._engine, "analyze_problem"):
            analysis = self._engine.analyze_problem(problem, depth=depth)
            if asyncio.iscoroutine(analysis):
                analysis = await analysis
            return {"analysis": analysis}

        if hasattr(self._engine, "analyze"):
            analysis = self._engine.analyze(problem)
            if asyncio.iscoroutine(analysis):
                analysis = await analysis
            return {"analysis": analysis}

        return {"analysis": None}

    async def _handle_predict_needs(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Predict user needs proactively."""
        context = input_data.get("context", {})

        if hasattr(self._engine, "predict_needs"):
            predictions = self._engine.predict_needs(context)
            if asyncio.iscoroutine(predictions):
                predictions = await predictions
            return {"predictions": predictions}

        return {"predictions": []}

    async def _handle_suggest_actions(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Suggest proactive actions."""
        context = input_data.get("context", {})
        limit = input_data.get("limit", 5)

        if hasattr(self._engine, "suggest_actions"):
            suggestions = self._engine.suggest_actions(context, limit=limit)
            if asyncio.iscoroutine(suggestions):
                suggestions = await suggestions
            return {"suggestions": suggestions}

        if hasattr(self._engine, "get_proactive_suggestions"):
            suggestions = self._engine.get_proactive_suggestions()
            if asyncio.iscoroutine(suggestions):
                suggestions = await suggestions
            return {"suggestions": suggestions}

        return {"suggestions": []}

    # =========================================================================
    # Message Handlers
    # =========================================================================

    async def _handle_knowledge_shared(self, message: AgentMessage) -> None:
        """Handle knowledge shared by other agents."""
        source = message.payload.get("source", "")
        knowledge_type = message.payload.get("knowledge_type", "")

        # Skip our own messages
        if source == self.agent_name:
            return

        logger.debug(
            "%s received knowledge from %s: %s",
            self.agent_name,
            source,
            knowledge_type,
        )

        # Update engine context if relevant
        if hasattr(self._engine, "update_from_knowledge"):
            await self._call_engine_method(
                "update_from_knowledge",
                message.payload,
            )

    async def _handle_context_update(self, message: AgentMessage) -> None:
        """Handle context updates from other agents."""
        context = message.payload.get("context", {})

        if hasattr(self._engine, "update_context"):
            await self._call_engine_method("update_context", context)

    async def _handle_custom_message(self, message: AgentMessage) -> None:
        """Handle custom messages."""
        event = message.payload.get("event", "")

        # Handle specific events
        if event == "request_analysis":
            space_id = message.payload.get("space_id")
            result = await self._handle_analyze_workspace({"space_id": space_id})

            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={"analysis": result},
                    from_agent=self.agent_name,
                )

    async def _call_engine_method(
        self,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Safely call an engine method."""
        if not hasattr(self._engine, method_name):
            return None

        method = getattr(self._engine, method_name)
        result = method(*args, **kwargs)

        if asyncio.iscoroutine(result):
            return await result
        return result

    @property
    def engine(self) -> Any:
        """Access the wrapped engine directly."""
        return self._engine

    @property
    def engine_type(self) -> IntelligenceEngineType:
        """Get the engine type."""
        return self._engine_type


# =============================================================================
# Factory Functions
# =============================================================================

async def create_uae_adapter(
    engine: Optional[Any] = None,
    agent_name: str = "uae_adapter",
) -> IntelligenceEngineAdapter:
    """Create an adapter for the Unified Awareness Engine.

    Args:
        engine: Existing UAE instance (creates new if None)
        agent_name: Name for the adapter agent

    Returns:
        Configured IntelligenceEngineAdapter
    """
    if engine is None:
        try:
            from intelligence.unified_awareness_engine import (
                UnifiedAwarenessEngine,
            )
            engine = UnifiedAwarenessEngine()
        except ImportError:
            logger.warning("Could not import UnifiedAwarenessEngine")
            raise

    return IntelligenceEngineAdapter(
        engine=engine,
        engine_type=IntelligenceEngineType.UAE,
        agent_name=agent_name,
    )


async def create_sai_adapter(
    engine: Optional[Any] = None,
    agent_name: str = "sai_adapter",
) -> IntelligenceEngineAdapter:
    """Create an adapter for the Spatial Awareness Intelligence.

    Args:
        engine: Existing SAI instance (creates new if None)
        agent_name: Name for the adapter agent

    Returns:
        Configured IntelligenceEngineAdapter
    """
    if engine is None:
        try:
            from intelligence.yabai_spatial_intelligence import (
                YabaiSpatialIntelligence,
            )
            engine = YabaiSpatialIntelligence()
        except ImportError:
            logger.warning("Could not import YabaiSpatialIntelligence")
            raise

    return IntelligenceEngineAdapter(
        engine=engine,
        engine_type=IntelligenceEngineType.SAI,
        agent_name=agent_name,
    )


async def create_cot_adapter(
    engine: Optional[Any] = None,
    agent_name: str = "cot_adapter",
) -> IntelligenceEngineAdapter:
    """Create an adapter for the Chain of Thought Engine.

    Args:
        engine: Existing CoT engine instance (creates new if None)
        agent_name: Name for the adapter agent

    Returns:
        Configured IntelligenceEngineAdapter
    """
    if engine is None:
        try:
            from intelligence.chain_of_thought import ChainOfThoughtEngine
            engine = ChainOfThoughtEngine()
        except ImportError:
            logger.warning("Could not import ChainOfThoughtEngine")
            raise

    return IntelligenceEngineAdapter(
        engine=engine,
        engine_type=IntelligenceEngineType.COT,
        agent_name=agent_name,
    )


async def create_rge_adapter(
    engine: Optional[Any] = None,
    agent_name: str = "rge_adapter",
) -> IntelligenceEngineAdapter:
    """Create an adapter for the Reasoning Graph Engine.

    Args:
        engine: Existing RGE instance (creates new if None)
        agent_name: Name for the adapter agent

    Returns:
        Configured IntelligenceEngineAdapter
    """
    if engine is None:
        try:
            from intelligence.reasoning_graph_engine import (
                ReasoningGraphEngine,
            )
            engine = ReasoningGraphEngine()
        except ImportError:
            logger.warning("Could not import ReasoningGraphEngine")
            raise

    return IntelligenceEngineAdapter(
        engine=engine,
        engine_type=IntelligenceEngineType.RGE,
        agent_name=agent_name,
    )


async def create_pie_adapter(
    engine: Optional[Any] = None,
    agent_name: str = "pie_adapter",
) -> IntelligenceEngineAdapter:
    """Create an adapter for the Proactive Intelligence Engine.

    Args:
        engine: Existing PIE instance (creates new if None)
        agent_name: Name for the adapter agent

    Returns:
        Configured IntelligenceEngineAdapter
    """
    if engine is None:
        try:
            from intelligence.proactive_intelligence_engine import (
                ProactiveIntelligenceEngine,
            )
            engine = ProactiveIntelligenceEngine()
        except ImportError:
            logger.warning("Could not import ProactiveIntelligenceEngine")
            raise

    return IntelligenceEngineAdapter(
        engine=engine,
        engine_type=IntelligenceEngineType.PIE,
        agent_name=agent_name,
    )


async def create_cai_adapter(
    engine: Optional[Any] = None,
    agent_name: str = "cai_adapter",
) -> IntelligenceEngineAdapter:
    """Create an adapter for the Contextual Awareness Intelligence.

    The CAI provides deep contextual understanding including:
    - Emotional intelligence and user mood detection
    - Behavioral pattern analysis
    - Context clustering and prediction
    - Adaptive personality modeling

    Args:
        engine: Existing CAI instance (creates new if None)
        agent_name: Name for the adapter agent

    Returns:
        Configured IntelligenceEngineAdapter
    """
    if engine is None:
        try:
            # Try to import ContextualUnderstandingEngine
            import os
            from autonomy.contextual_understanding import (
                ContextualUnderstandingEngine,
            )
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            if api_key:
                engine = ContextualUnderstandingEngine(anthropic_api_key=api_key)
            else:
                logger.warning("ANTHROPIC_API_KEY not set for CAI")
                raise ImportError("API key required for CAI")
        except ImportError as e:
            logger.warning(f"Could not import ContextualUnderstandingEngine: {e}")
            raise

    return IntelligenceEngineAdapter(
        engine=engine,
        engine_type=IntelligenceEngineType.CAI,
        agent_name=agent_name,
    )
