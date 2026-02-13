"""
Unified LangGraph-Based Autonomous Agent for JARVIS

This module provides the main autonomous agent that combines:
- LangGraph reasoning engine
- LangChain tool registry
- Tool orchestration
- Memory management
- JARVIS system integration

The agent provides:
- Multi-step autonomous reasoning
- Dynamic tool selection and execution
- Context-aware decision making
- Learning from experience
- Seamless JARVIS integration

This is the primary entry point for autonomous operations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    Any, Awaitable, Callable, Dict, List, Optional, Set, Type, Union
)
from uuid import uuid4

try:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    LANGCHAIN_MODELS_AVAILABLE = True
except ImportError:
    LANGCHAIN_MODELS_AVAILABLE = False
    BaseChatModel = None

try:
    from anthropic import AsyncAnthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    AsyncAnthropic = None

from pydantic import BaseModel, Field

# Import our modules
from .langgraph_engine import (
    LangGraphReasoningEngine,
    GraphState,
    ReasoningPhase,
    create_reasoning_engine
)
from .langchain_tools import (
    ToolRegistry,
    JARVISTool,
    ToolCategory,
    ToolRiskLevel,
    register_builtin_tools,
    auto_discover_tools
)
from .tool_orchestrator import (
    ToolOrchestrator,
    ExecutionTask,
    ExecutionStrategy,
    ExecutionPriority,
    create_orchestrator
)
from .memory_integration import (
    MemoryManager,
    ConversationMemory,
    EpisodicMemory,
    MemoryType,
    MemoryPriority,
    JARVISCheckpointer,
    create_memory_manager,
    create_checkpointer
)
from .jarvis_integration import (
    JARVISIntegrationManager,
    IntegrationConfig,
    get_integration_manager,
    configure_integration
)

logger = logging.getLogger(__name__)


# ============================================================================
# Agent Configuration
# ============================================================================

class AgentMode(str, Enum):
    """Operating modes for the agent."""
    AUTONOMOUS = "autonomous"       # Fully autonomous decision making
    SUPERVISED = "supervised"       # Requires approval for actions
    INTERACTIVE = "interactive"     # Conversational with user
    BACKGROUND = "background"       # Silent background operations


class AgentPersonality(str, Enum):
    """Personality presets for the agent."""
    EFFICIENT = "efficient"         # Minimal interaction, maximum efficiency
    HELPFUL = "helpful"             # Detailed explanations, proactive help
    CAUTIOUS = "cautious"           # Conservative, asks for confirmation
    PROACTIVE = "proactive"         # Anticipates needs, suggests actions


@dataclass
class AgentConfig:
    """Configuration for the autonomous agent."""
    # Core settings
    name: str = "JARVIS"
    mode: AgentMode = AgentMode.SUPERVISED
    personality: AgentPersonality = AgentPersonality.HELPFUL

    # LLM settings
    model_name: str = "claude-3-5-sonnet-20241022"
    temperature: float = 0.7
    max_tokens: int = 4096

    # Reasoning settings
    max_reasoning_iterations: int = 10
    min_confidence_threshold: float = 0.4
    enable_reflection: bool = True
    enable_learning: bool = True

    # Tool settings
    enable_tool_discovery: bool = True
    max_concurrent_tools: int = 5
    tool_timeout_seconds: float = 30.0
    allowed_risk_levels: List[ToolRiskLevel] = field(
        default_factory=lambda: [ToolRiskLevel.SAFE, ToolRiskLevel.LOW, ToolRiskLevel.MEDIUM]
    )

    # Memory settings
    enable_memory: bool = True
    working_memory_size: int = 100
    conversation_history_size: int = 50
    enable_episodic_memory: bool = True

    # Checkpointing
    enable_checkpointing: bool = True
    checkpoint_path: str = ".jarvis_cache/checkpoints"

    # Safety settings
    require_permission_for_high_risk: bool = True
    max_actions_per_session: int = 100
    enable_rollback: bool = True


# ============================================================================
# Agent State
# ============================================================================

@dataclass
class AgentSession:
    """Represents an agent session."""
    session_id: str
    started_at: datetime
    mode: AgentMode
    goal: Optional[str] = None
    status: str = "active"
    action_count: int = 0
    error_count: int = 0
    completed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentMetrics:
    """Metrics for agent performance."""
    total_sessions: int = 0
    successful_sessions: int = 0
    total_actions: int = 0
    successful_actions: int = 0
    average_reasoning_time_ms: float = 0
    average_execution_time_ms: float = 0
    tool_usage: Dict[str, int] = field(default_factory=dict)


# ============================================================================
# Main Autonomous Agent
# ============================================================================

class AutonomousAgent:
    """
    Unified LangGraph-based autonomous agent for JARVIS.

    This agent combines reasoning, tool execution, memory, and learning
    into a cohesive autonomous system that integrates seamlessly with
    existing JARVIS components.

    Example usage:
        ```python
        # Create agent with custom config
        agent = AutonomousAgent(config=AgentConfig(
            mode=AgentMode.SUPERVISED,
            personality=AgentPersonality.HELPFUL
        ))

        # Initialize (discovers tools, sets up memory, etc.)
        await agent.initialize()

        # Run autonomous task
        result = await agent.run("Organize my workspace and prepare for tomorrow's meeting")

        # Interactive conversation
        response = await agent.chat("What did you learn from organizing my workspace?")

        # Check status
        status = agent.get_status()
        ```
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        llm_client: Optional[Any] = None,
        integration_manager: Optional[JARVISIntegrationManager] = None
    ):
        self.config = config or AgentConfig()
        self.llm_client = llm_client
        self.integration_manager = integration_manager

        # Core components (initialized in initialize())
        self.reasoning_engine: Optional[LangGraphReasoningEngine] = None
        self.tool_registry: Optional[ToolRegistry] = None
        self.tool_orchestrator: Optional[ToolOrchestrator] = None
        self.memory_manager: Optional[MemoryManager] = None
        self.conversation_memory: Optional[ConversationMemory] = None
        self.episodic_memory: Optional[EpisodicMemory] = None
        self.checkpointer: Optional[JARVISCheckpointer] = None

        # State
        self._initialized = False
        self._current_session: Optional[AgentSession] = None
        self._sessions: Dict[str, AgentSession] = {}
        self._metrics = AgentMetrics()
        self._runtime = None  # Set by UnifiedAgentRuntime during startup

        # Callbacks
        self._callbacks: Dict[str, List[Callable]] = {
            "session_started": [],
            "session_completed": [],
            "action_executed": [],
            "reasoning_step": [],
            "error": []
        }

        self.logger = logging.getLogger(f"{__name__}.{self.config.name}")

    async def initialize(self) -> bool:
        """
        Initialize the agent and all its components.

        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True

        self.logger.info(f"Initializing {self.config.name} agent...")

        try:
            # Initialize tool registry
            self.tool_registry = ToolRegistry.get_instance()
            register_builtin_tools(self.tool_registry)

            if self.config.enable_tool_discovery:
                discovered = auto_discover_tools(registry=self.tool_registry)
                self.logger.info(f"Discovered {discovered} tools")

            # Initialize tool orchestrator
            self.tool_orchestrator = create_orchestrator(
                tool_registry=self.tool_registry,
                max_concurrent=self.config.max_concurrent_tools,
                default_timeout=self.config.tool_timeout_seconds
            )

            # Initialize integration manager
            if self.integration_manager is None:
                self.integration_manager = get_integration_manager()

            # Initialize memory
            if self.config.enable_memory:
                self.memory_manager = create_memory_manager(
                    working_memory_size=self.config.working_memory_size
                )
                await self.memory_manager.start()

                self.conversation_memory = ConversationMemory(
                    max_turns=self.config.conversation_history_size
                )
                await self.conversation_memory.initialize()

                if self.config.enable_episodic_memory:
                    self.episodic_memory = EpisodicMemory()

            # Initialize checkpointer
            if self.config.enable_checkpointing:
                self.checkpointer = create_checkpointer(
                    storage_path=self.config.checkpoint_path
                )

            # Initialize reasoning engine
            self.reasoning_engine = create_reasoning_engine(
                permission_manager=self.integration_manager.permission_adapter if self.integration_manager else None,
                action_executor=self.integration_manager.executor_adapter if self.integration_manager else None,
                tool_orchestrator=self.tool_orchestrator,
                learning_db=self.integration_manager.learning_adapter if self.integration_manager else None,
                llm_client=self.llm_client,
                enable_checkpointing=self.config.enable_checkpointing
            )

            # Initialize LLM client if needed
            if self.llm_client is None:
                self.llm_client = await self._create_llm_client()

            self._initialized = True
            self.logger.info(f"{self.config.name} agent initialized successfully")
            return True

        except Exception as e:
            self.logger.error(f"Failed to initialize agent: {e}")
            return False

    async def _create_llm_client(self) -> Optional[Any]:
        """Create LLM client based on configuration."""
        if ANTHROPIC_AVAILABLE:
            import os
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if api_key:
                return AsyncAnthropic(api_key=api_key)

        return None

    async def run(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        mode_override: Optional[AgentMode] = None
    ) -> Dict[str, Any]:
        """
        Run the agent to achieve a goal.

        Args:
            goal: The goal to achieve
            context: Additional context
            mode_override: Override the default mode

        Returns:
            Result dictionary with outcome, actions taken, etc.
        """
        if not self._initialized:
            await self.initialize()

        mode = mode_override or self.config.mode
        session = self._start_session(goal, mode)

        try:
            # Store in conversation memory
            if self.conversation_memory:
                self.conversation_memory.add_turn("user", goal)

            # Get full context
            full_context = await self._build_context(context)

            # Check for similar past experiences
            if self.episodic_memory:
                past_experiences = await self.episodic_memory.find_similar_episodes(
                    goal, success_only=True, limit=3
                )
                if past_experiences:
                    full_context["past_experiences"] = [
                        {
                            "goal": exp.goal,
                            "actions_count": len(exp.actions),
                            "outcome": exp.outcome,
                            "lessons": exp.lessons_learned
                        }
                        for exp in past_experiences
                    ]

            # Run reasoning engine
            self._emit("reasoning_step", {"phase": "started", "goal": goal})

            start_time = time.time()
            state = await self.reasoning_engine.run(
                query=goal,
                context=full_context
            )
            reasoning_time = (time.time() - start_time) * 1000

            # Process result
            result = self._process_state(state, session)
            result["reasoning_time_ms"] = reasoning_time

            # Update metrics
            self._update_metrics(session, result, reasoning_time)

            # Store response in conversation memory
            if self.conversation_memory and result.get("response"):
                self.conversation_memory.add_turn("assistant", result["response"])

            # Record episode
            if self.episodic_memory and self.config.enable_learning:
                await self.episodic_memory.record_episode(
                    session_id=session.session_id,
                    goal=goal,
                    actions=[a for a in state.execution_results] if state.execution_results else [],
                    outcome=result.get("response", ""),
                    success=result.get("success", False),
                    context=full_context,
                    lessons=state.reflection_notes if hasattr(state, 'reflection_notes') else []
                )

            # Complete session
            self._complete_session(session, result)

            return result

        except Exception as e:
            self.logger.error(f"Agent run failed: {e}")
            session.error_count += 1
            self._emit("error", {"session_id": session.session_id, "error": str(e)})

            result = {
                "success": False,
                "error": str(e),
                "session_id": session.session_id
            }
            self._complete_session(session, result)
            return result

    async def chat(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Have a conversational interaction.

        Args:
            message: User message
            context: Additional context

        Returns:
            Agent response
        """
        if not self._initialized:
            await self.initialize()

        # Add to conversation
        if self.conversation_memory:
            self.conversation_memory.add_turn("user", message)

        # Determine if this requires action or is just conversation
        requires_action = await self._analyze_message_intent(message)

        if requires_action:
            # Run full autonomous loop
            result = await self.run(message, context, mode_override=AgentMode.INTERACTIVE)
            response = result.get("response", "I completed the task.")
        else:
            # Simple conversational response
            response = await self._generate_response(message, context)

        if self.conversation_memory:
            self.conversation_memory.add_turn("assistant", response)

        return response

    async def _analyze_message_intent(self, message: str) -> bool:
        """Analyze if a message requires action."""
        # Simple heuristic - can be enhanced with LLM
        action_indicators = [
            "do", "make", "create", "delete", "run", "execute", "open",
            "close", "start", "stop", "find", "search", "organize",
            "send", "schedule", "remind", "set", "change", "update"
        ]

        message_lower = message.lower()
        return any(indicator in message_lower for indicator in action_indicators)

    async def _generate_response(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Generate a conversational response."""
        if self.llm_client and ANTHROPIC_AVAILABLE:
            try:
                # Build messages
                messages = []

                # Add conversation history
                if self.conversation_memory:
                    history = self.conversation_memory.get_context_messages(max_tokens=2000)
                    for msg in history[:-1]:  # Exclude the just-added user message
                        messages.append(msg)

                messages.append({"role": "user", "content": message})

                # Call LLM
                response = await self.llm_client.messages.create(
                    model=self.config.model_name,
                    max_tokens=self.config.max_tokens,
                    system=self._get_system_prompt(),
                    messages=messages
                )

                return response.content[0].text

            except Exception as e:
                self.logger.warning(f"LLM call failed: {e}")

        # Fallback response
        return f"I understand you said: '{message}'. How can I assist you further?"

    def _get_system_prompt(self) -> str:
        """Get system prompt based on personality."""
        base_prompt = f"You are {self.config.name}, an intelligent AI assistant."

        personality_prompts = {
            AgentPersonality.EFFICIENT: "Be concise and efficient. Focus on completing tasks quickly with minimal conversation.",
            AgentPersonality.HELPFUL: "Be thorough and helpful. Explain your actions and offer additional suggestions when appropriate.",
            AgentPersonality.CAUTIOUS: "Be careful and thorough. Always ask for confirmation before taking significant actions.",
            AgentPersonality.PROACTIVE: "Be proactive and anticipate user needs. Suggest helpful actions before being asked."
        }

        return f"{base_prompt} {personality_prompts.get(self.config.personality, '')}"

    async def _build_context(
        self,
        additional_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Build comprehensive context for reasoning."""
        context = additional_context or {}

        # Add integration context
        if self.integration_manager:
            try:
                jarvis_context = await self.integration_manager.get_full_context()
                context["jarvis"] = jarvis_context
            except Exception as e:
                self.logger.warning(f"Failed to get JARVIS context: {e}")

        # Add memory context
        if self.memory_manager:
            try:
                memory_context = await self.memory_manager.get_context_summary()
                context["memory"] = memory_context
            except Exception as e:
                self.logger.warning(f"Failed to get memory context: {e}")

        # Add conversation context
        if self.conversation_memory:
            context["conversation_turns"] = len(self.conversation_memory._turns)

        # Add agent config context
        context["agent_config"] = {
            "mode": self.config.mode.value,
            "personality": self.config.personality.value,
            "confidence_threshold": self.config.min_confidence_threshold
        }

        return context

    def _start_session(self, goal: str, mode: AgentMode) -> AgentSession:
        """Start a new agent session."""
        session = AgentSession(
            session_id=str(uuid4()),
            started_at=datetime.utcnow(),
            mode=mode,
            goal=goal
        )

        self._current_session = session
        self._sessions[session.session_id] = session
        self._metrics.total_sessions += 1

        self._emit("session_started", {
            "session_id": session.session_id,
            "goal": goal,
            "mode": mode.value
        })

        return session

    def _complete_session(
        self,
        session: AgentSession,
        result: Dict[str, Any]
    ) -> None:
        """Complete an agent session."""
        session.completed_at = datetime.utcnow()
        session.status = "completed" if result.get("success") else "failed"

        if result.get("success"):
            self._metrics.successful_sessions += 1

        self._emit("session_completed", {
            "session_id": session.session_id,
            "success": result.get("success"),
            "duration_ms": (session.completed_at - session.started_at).total_seconds() * 1000
        })

        if self._current_session and self._current_session.session_id == session.session_id:
            self._current_session = None

    def _process_state(
        self,
        state: GraphState,
        session: AgentSession
    ) -> Dict[str, Any]:
        """Process the final state into a result."""
        # Count actions
        action_count = len(state.execution_results) if state.execution_results else 0
        session.action_count = action_count

        # Calculate success rate
        if state.execution_results:
            from .langgraph_engine import ActionOutcome
            successful = sum(
                1 for r in state.execution_results
                if r.get("outcome") == ActionOutcome.SUCCESS.value
            )
            success_rate = successful / len(state.execution_results)
        else:
            success_rate = 1.0 if state.phase == ReasoningPhase.COMPLETED else 0.0

        return {
            "success": state.phase == ReasoningPhase.COMPLETED and success_rate >= 0.5,
            "response": state.final_response,
            "session_id": session.session_id,
            "phase": state.phase.value,
            "confidence": state.confidence,
            "action_count": action_count,
            "success_rate": success_rate,
            "reflection_notes": state.reflection_notes,
            "reasoning_trace": state.reasoning_trace[-5:] if state.reasoning_trace else []
        }

    def _update_metrics(
        self,
        session: AgentSession,
        result: Dict[str, Any],
        reasoning_time_ms: float
    ) -> None:
        """Update agent metrics."""
        self._metrics.total_actions += session.action_count

        if result.get("success"):
            self._metrics.successful_actions += session.action_count

        # Update average times
        n = self._metrics.total_sessions
        self._metrics.average_reasoning_time_ms = (
            (self._metrics.average_reasoning_time_ms * (n - 1) + reasoning_time_ms) / n
        )

    def on(self, event: str, callback: Callable) -> None:
        """Register event callback."""
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    def _emit(self, event: str, data: Any) -> None:
        """Emit an event."""
        for callback in self._callbacks.get(event, []):
            try:
                if asyncio.iscoroutinefunction(callback):
                    asyncio.create_task(callback(data))
                else:
                    callback(data)
            except Exception as e:
                self.logger.warning(f"Callback error: {e}")

    # ============================================================================
    # Tool Management
    # ============================================================================

    def register_tool(self, tool: JARVISTool) -> None:
        """Register a new tool."""
        if self.tool_registry:
            self.tool_registry.register(tool)

    def get_available_tools(self) -> List[str]:
        """Get list of available tools."""
        if self.tool_registry:
            return [t.name for t in self.tool_registry.get_all()]
        return []

    async def execute_tool(
        self,
        tool_name: str,
        **kwargs
    ) -> Any:
        """Execute a specific tool."""
        if not self.tool_orchestrator:
            raise RuntimeError("Agent not initialized")

        return await self.tool_orchestrator.execute(
            action_type=tool_name,
            target=kwargs.get("target", ""),
            parameters=kwargs
        )

    # ============================================================================
    # Memory Operations
    # ============================================================================

    async def remember(
        self,
        content: Any,
        memory_type: MemoryType = MemoryType.SHORT_TERM,
        **kwargs
    ) -> str:
        """Store something in memory."""
        if self.memory_manager:
            return await self.memory_manager.remember(content, memory_type, **kwargs)
        return ""

    async def recall(self, query: str, **kwargs) -> List[Any]:
        """Recall from memory."""
        if self.memory_manager:
            entries = await self.memory_manager.recall(query, **kwargs)
            return [e.content for e in entries]
        return []

    # ============================================================================
    # Status and Control
    # ============================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get agent status."""
        return {
            "name": self.config.name,
            "initialized": self._initialized,
            "mode": self.config.mode.value,
            "personality": self.config.personality.value,
            "current_session": self._current_session.session_id if self._current_session else None,
            "metrics": {
                "total_sessions": self._metrics.total_sessions,
                "successful_sessions": self._metrics.successful_sessions,
                "total_actions": self._metrics.total_actions,
                "successful_actions": self._metrics.successful_actions,
                "average_reasoning_time_ms": self._metrics.average_reasoning_time_ms
            },
            "components": {
                "tool_count": len(self.get_available_tools()),
                "memory_enabled": self.config.enable_memory,
                "checkpointing_enabled": self.config.enable_checkpointing,
                "integration_available": self.integration_manager is not None
            }
        }

    def get_session_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent session history."""
        sessions = sorted(
            self._sessions.values(),
            key=lambda s: s.started_at,
            reverse=True
        )[:limit]

        return [
            {
                "session_id": s.session_id,
                "goal": s.goal,
                "status": s.status,
                "action_count": s.action_count,
                "started_at": s.started_at.isoformat(),
                "completed_at": s.completed_at.isoformat() if s.completed_at else None
            }
            for s in sessions
        ]

    async def shutdown(self) -> None:
        """Shutdown the agent gracefully."""
        self.logger.info(f"Shutting down {self.config.name} agent...")

        if self.conversation_memory:
            await self.conversation_memory.shutdown()

        if self.memory_manager:
            await self.memory_manager.stop()

        if self.tool_orchestrator:
            await self.tool_orchestrator.shutdown()

        self._initialized = False
        self.logger.info(f"{self.config.name} agent shut down")

    # ========================================================================
    # Agent Runtime Integration — Goal Pursuit API
    # ========================================================================

    async def pursue_goal(
        self,
        description: str,
        priority: str = "normal",
        context: Optional[Dict[str, Any]] = None,
        needs_vision: bool = False,
    ) -> str:
        """Submit a goal for multi-step autonomous pursuit.

        This delegates to the UnifiedAgentRuntime which provides:
        - SENSE→THINK→ACT→VERIFY→REFLECT loop per goal
        - Concurrent goal execution with ScreenLease
        - Checkpoint persistence for crash recovery
        - Per-step dynamic escalation

        Args:
            description: What the agent should accomplish
            priority: "background", "normal", "high", or "critical"
            context: Additional context for the goal
            needs_vision: Whether this goal requires screen access

        Returns:
            goal_id string for tracking

        Raises:
            RuntimeError: If agent runtime is not initialized
        """
        if self._runtime is None:
            raise RuntimeError(
                "Agent runtime not initialized. "
                "Submit goals after runtime startup completes."
            )
        from backend.autonomy.agent_runtime_models import GoalPriority
        return await self._runtime.submit_goal(
            description=description,
            priority=GoalPriority[priority.upper()],
            source="user",
            context=context,
            needs_vision=needs_vision,
        )

    async def cancel_goal(self, goal_id: str, reason: str = "user_cancelled"):
        """Cancel a specific goal by ID."""
        if self._runtime is not None:
            await self._runtime.cancel_goal(goal_id, reason)

    async def cancel_all_goals(self, reason: str = "user_cancelled"):
        """Cancel all active goals. Called on 'JARVIS stop'."""
        if self._runtime is not None:
            await self._runtime.cancel_active_goals(reason)

    async def get_goal_status(self, goal_id: str) -> Optional[Dict[str, Any]]:
        """Get current status of a goal."""
        if self._runtime is not None:
            return await self._runtime.get_goal_status(goal_id)
        return None

    async def get_all_goals(self) -> List[Dict[str, Any]]:
        """Get status of all active goals."""
        if self._runtime is not None:
            return await self._runtime.get_all_goals_status()
        return []

    async def approve_escalation(self, goal_id: str, approved: bool = True):
        """Approve or reject an escalated goal step."""
        if self._runtime is not None:
            await self._runtime.approve_escalation(goal_id, approved)

    async def get_deferred_resume_goals(self) -> List[Dict[str, Any]]:
        """Get cross-session goals awaiting resume review."""
        if self._runtime is not None and hasattr(self._runtime, "get_deferred_resume_goals"):
            return await self._runtime.get_deferred_resume_goals()
        return []

    async def resume_deferred_goals(
        self, goal_ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Resume cross-session deferred goals."""
        if self._runtime is not None and hasattr(self._runtime, "resume_deferred_goals"):
            return await self._runtime.resume_deferred_goals(goal_ids=goal_ids)
        return {"resumed_goal_ids": [], "skipped_goal_ids": [], "remaining_deferred": 0}

    async def defer_deferred_goals(
        self,
        goal_ids: Optional[List[str]] = None,
        reason: str = "resume_deferred_by_user",
    ) -> Dict[str, Any]:
        """Leave deferred goals paused and clear current boot review queue."""
        if self._runtime is not None and hasattr(self._runtime, "defer_deferred_goals"):
            return await self._runtime.defer_deferred_goals(goal_ids=goal_ids, reason=reason)
        return {"deferred_goal_ids": [], "remaining_deferred": 0}


# ============================================================================
# Factory Functions
# ============================================================================

def create_agent(
    name: str = "JARVIS",
    mode: AgentMode = AgentMode.SUPERVISED,
    personality: AgentPersonality = AgentPersonality.HELPFUL,
    **config_kwargs
) -> AutonomousAgent:
    """
    Create a configured autonomous agent.

    Args:
        name: Agent name
        mode: Operating mode
        personality: Agent personality
        **config_kwargs: Additional configuration

    Returns:
        Configured AutonomousAgent
    """
    config = AgentConfig(
        name=name,
        mode=mode,
        personality=personality,
        **config_kwargs
    )

    return AutonomousAgent(config=config)


async def create_and_initialize_agent(**kwargs) -> AutonomousAgent:
    """Create and initialize an agent in one step."""
    agent = create_agent(**kwargs)
    await agent.initialize()
    return agent


# ============================================================================
# Convenience Functions
# ============================================================================

_default_agent: Optional[AutonomousAgent] = None


def get_default_agent() -> AutonomousAgent:
    """Get or create default agent instance."""
    global _default_agent
    if _default_agent is None:
        _default_agent = create_agent()
    return _default_agent


async def run_autonomous(goal: str, **kwargs) -> Dict[str, Any]:
    """Run an autonomous task with the default agent."""
    agent = get_default_agent()
    if not agent._initialized:
        await agent.initialize()
    return await agent.run(goal, **kwargs)


async def chat(message: str, **kwargs) -> str:
    """Chat with the default agent."""
    agent = get_default_agent()
    if not agent._initialized:
        await agent.initialize()
    return await agent.chat(message, **kwargs)


# ============================================================================
# Agent Builder for Advanced Configuration
# ============================================================================

class AgentBuilder:
    """
    Builder pattern for creating highly customized agents.

    Example:
        ```python
        agent = (AgentBuilder()
            .with_name("CustomAgent")
            .with_mode(AgentMode.AUTONOMOUS)
            .with_personality(AgentPersonality.PROACTIVE)
            .with_tools(["calculator", "datetime"])
            .with_memory(working_size=200)
            .with_integration(permission_manager=my_pm)
            .build())
        ```
    """

    def __init__(self):
        self._config = AgentConfig()
        self._integration_config = IntegrationConfig()
        self._llm_client = None
        self._custom_tools: List[JARVISTool] = []

    def with_name(self, name: str) -> "AgentBuilder":
        """Set agent name."""
        self._config.name = name
        return self

    def with_mode(self, mode: AgentMode) -> "AgentBuilder":
        """Set operating mode."""
        self._config.mode = mode
        return self

    def with_personality(self, personality: AgentPersonality) -> "AgentBuilder":
        """Set personality."""
        self._config.personality = personality
        return self

    def with_model(
        self,
        model_name: str,
        temperature: float = 0.7,
        max_tokens: int = 4096
    ) -> "AgentBuilder":
        """Configure LLM model."""
        self._config.model_name = model_name
        self._config.temperature = temperature
        self._config.max_tokens = max_tokens
        return self

    def with_llm_client(self, client: Any) -> "AgentBuilder":
        """Set custom LLM client."""
        self._llm_client = client
        return self

    def with_reasoning(
        self,
        max_iterations: int = 10,
        min_confidence: float = 0.4,
        enable_reflection: bool = True,
        enable_learning: bool = True
    ) -> "AgentBuilder":
        """Configure reasoning parameters."""
        self._config.max_reasoning_iterations = max_iterations
        self._config.min_confidence_threshold = min_confidence
        self._config.enable_reflection = enable_reflection
        self._config.enable_learning = enable_learning
        return self

    def with_tools(
        self,
        tool_names: Optional[List[str]] = None,
        max_concurrent: int = 5,
        timeout: float = 30.0,
        risk_levels: Optional[List[ToolRiskLevel]] = None
    ) -> "AgentBuilder":
        """Configure tool settings."""
        self._config.max_concurrent_tools = max_concurrent
        self._config.tool_timeout_seconds = timeout
        if risk_levels:
            self._config.allowed_risk_levels = risk_levels
        return self

    def with_custom_tool(self, tool: JARVISTool) -> "AgentBuilder":
        """Add a custom tool."""
        self._custom_tools.append(tool)
        return self

    def with_memory(
        self,
        working_size: int = 100,
        conversation_size: int = 50,
        enable_episodic: bool = True
    ) -> "AgentBuilder":
        """Configure memory settings."""
        self._config.enable_memory = True
        self._config.working_memory_size = working_size
        self._config.conversation_history_size = conversation_size
        self._config.enable_episodic_memory = enable_episodic
        return self

    def without_memory(self) -> "AgentBuilder":
        """Disable memory."""
        self._config.enable_memory = False
        return self

    def with_checkpointing(self, path: str = ".jarvis_cache/checkpoints") -> "AgentBuilder":
        """Enable checkpointing."""
        self._config.enable_checkpointing = True
        self._config.checkpoint_path = path
        return self

    def without_checkpointing(self) -> "AgentBuilder":
        """Disable checkpointing."""
        self._config.enable_checkpointing = False
        return self

    def with_integration(
        self,
        permission_manager: Optional[Any] = None,
        action_queue: Optional[Any] = None,
        action_executor: Optional[Any] = None,
        context_engine: Optional[Any] = None,
        learning_db: Optional[Any] = None
    ) -> "AgentBuilder":
        """Configure JARVIS integration."""
        self._integration_config = IntegrationConfig(
            permission_manager=permission_manager,
            action_queue=action_queue,
            action_executor=action_executor,
            context_engine=context_engine,
            learning_db=learning_db
        )
        return self

    def with_safety(
        self,
        require_permission_for_high_risk: bool = True,
        max_actions: int = 100,
        enable_rollback: bool = True
    ) -> "AgentBuilder":
        """Configure safety settings."""
        self._config.require_permission_for_high_risk = require_permission_for_high_risk
        self._config.max_actions_per_session = max_actions
        self._config.enable_rollback = enable_rollback
        return self

    def build(self) -> AutonomousAgent:
        """Build the agent."""
        # Create integration manager if configured
        integration_manager = None
        if any([
            self._integration_config.permission_manager,
            self._integration_config.action_queue,
            self._integration_config.action_executor,
            self._integration_config.context_engine,
            self._integration_config.learning_db
        ]):
            integration_manager = JARVISIntegrationManager(self._integration_config)

        # Create agent
        agent = AutonomousAgent(
            config=self._config,
            llm_client=self._llm_client,
            integration_manager=integration_manager
        )

        # Store custom tools for registration after initialization
        agent._pending_custom_tools = self._custom_tools

        return agent

    async def build_and_initialize(self) -> AutonomousAgent:
        """Build and initialize the agent."""
        agent = self.build()
        await agent.initialize()

        # Register custom tools
        if hasattr(agent, '_pending_custom_tools'):
            for tool in agent._pending_custom_tools:
                agent.register_tool(tool)
            delattr(agent, '_pending_custom_tools')

        return agent
