"""
JARVIS Neural Mesh - Autonomy Engine Adapter

Adapts the JARVIS Autonomy components (AutonomousAgent, LangGraphReasoningEngine,
ToolOrchestrator, MemoryManager) for seamless integration with Neural Mesh.

This adapter enables:
- Distributed autonomous task execution across agents
- Shared reasoning state via knowledge graph
- Coordinated tool orchestration
- Cross-agent memory sharing and learning

Usage:
    from autonomy.autonomous_agent import AutonomousAgent

    agent = AutonomousAgent()
    await agent.initialize()

    adapted = AutonomyEngineAdapter(
        autonomy_component=agent,
        component_type="agent",
    )

    await coordinator.register_agent(adapted)
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
    WorkflowTask,
)

logger = logging.getLogger(__name__)


class AutonomyComponentType(str, Enum):
    """Types of autonomy components in JARVIS."""
    AGENT = "agent"  # AutonomousAgent - main orchestrator
    REASONING = "reasoning"  # LangGraphReasoningEngine
    TOOLS = "tools"  # ToolOrchestrator
    MEMORY = "memory"  # MemoryManager
    INTEGRATION = "integration"  # JARVISIntegrationManager


@dataclass
class AutonomyCapabilities:
    """Capabilities matrix for autonomy components."""
    autonomous_execution: bool = False
    reasoning: bool = False
    tool_orchestration: bool = False
    memory_management: bool = False
    learning: bool = False
    checkpointing: bool = False
    parallel_execution: bool = False
    goal_tracking: bool = False
    reflection: bool = False

    def to_set(self) -> Set[str]:
        """Convert capabilities to set of strings."""
        caps = set()
        if self.autonomous_execution:
            caps.add("autonomous_execution")
        if self.reasoning:
            caps.add("reasoning")
        if self.tool_orchestration:
            caps.add("tool_orchestration")
        if self.memory_management:
            caps.add("memory_management")
        if self.learning:
            caps.add("learning")
        if self.checkpointing:
            caps.add("checkpointing")
        if self.parallel_execution:
            caps.add("parallel_execution")
        if self.goal_tracking:
            caps.add("goal_tracking")
        if self.reflection:
            caps.add("reflection")
        return caps


# Capability mappings for each component type
COMPONENT_CAPABILITIES: Dict[AutonomyComponentType, AutonomyCapabilities] = {
    AutonomyComponentType.AGENT: AutonomyCapabilities(
        autonomous_execution=True,
        reasoning=True,
        tool_orchestration=True,
        memory_management=True,
        learning=True,
        checkpointing=True,
        parallel_execution=True,
        goal_tracking=True,
        reflection=True,
    ),
    AutonomyComponentType.REASONING: AutonomyCapabilities(
        reasoning=True,
        reflection=True,
        goal_tracking=True,
    ),
    AutonomyComponentType.TOOLS: AutonomyCapabilities(
        tool_orchestration=True,
        parallel_execution=True,
    ),
    AutonomyComponentType.MEMORY: AutonomyCapabilities(
        memory_management=True,
        learning=True,
        checkpointing=True,
    ),
    AutonomyComponentType.INTEGRATION: AutonomyCapabilities(
        autonomous_execution=True,
        tool_orchestration=True,
    ),
}


class AutonomyEngineAdapter(BaseNeuralMeshAgent):
    """
    Adapter for JARVIS Autonomy components to work with Neural Mesh.

    This adapter wraps AutonomousAgent, LangGraphReasoningEngine, ToolOrchestrator,
    and MemoryManager, exposing their capabilities through Neural Mesh.

    Key Features:
    - Full async support with LangGraph integration
    - Distributed task execution via message bus
    - Shared memory through knowledge graph
    - Cross-agent tool coordination
    - Checkpoint sharing for recovery

    Example - Wrapping AutonomousAgent:
        from autonomy.autonomous_agent import AutonomousAgent

        agent = AutonomousAgent()
        await agent.initialize()

        adapter = AutonomyEngineAdapter(
            autonomy_component=agent,
            component_type=AutonomyComponentType.AGENT,
        )
        await coordinator.register_agent(adapter)

        # Execute autonomous task
        result = await adapter.execute_task({
            "action": "run",
            "input": {"goal": "Organize workspace and prepare for meeting"}
        })

    Example - Coordinated tool execution:
        result = await adapter.execute_task({
            "action": "orchestrate_tools",
            "input": {
                "tasks": [
                    {"tool": "file_search", "args": {"pattern": "*.py"}},
                    {"tool": "code_analysis", "args": {"file": "main.py"}},
                ],
                "strategy": "parallel"
            }
        })
    """

    def __init__(
        self,
        autonomy_component: Any,
        component_type: Union[AutonomyComponentType, str],
        agent_name: Optional[str] = None,
        additional_capabilities: Optional[Set[str]] = None,
        version: str = "1.0.0",
    ) -> None:
        """Initialize the autonomy engine adapter.

        Args:
            autonomy_component: The autonomy component to wrap
            component_type: Type of component (agent, reasoning, tools, etc.)
            agent_name: Optional custom name
            additional_capabilities: Extra capabilities beyond defaults
            version: Adapter version
        """
        # Normalize component type
        if isinstance(component_type, str):
            component_type = AutonomyComponentType(component_type.lower())

        self._component_type = component_type
        self._component = autonomy_component

        # Get capabilities for this component type
        caps = COMPONENT_CAPABILITIES.get(
            component_type,
            AutonomyCapabilities()
        )
        capabilities = caps.to_set()

        # Add any additional capabilities
        if additional_capabilities:
            capabilities.update(additional_capabilities)

        # Set agent name
        name = agent_name or f"autonomy_{component_type.value}"

        super().__init__(
            agent_name=name,
            agent_type="autonomy",
            capabilities=capabilities,
            backend="local",
            version=version,
        )

        # Task handlers
        self._task_handlers: Dict[str, Callable] = {}
        self._setup_handlers()

        # Execution state
        self._active_sessions: Dict[str, Dict[str, Any]] = {}
        self._execution_history: List[Dict[str, Any]] = []
        self._max_history = 100

        # Coordination state
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._shared_checkpoints: Dict[str, Any] = {}

    def _setup_handlers(self) -> None:
        """Setup action handlers based on component type."""
        # Common handlers
        self._task_handlers["get_status"] = self._handle_get_status
        self._task_handlers["get_metrics"] = self._handle_get_metrics

        # Agent handlers
        if self._component_type == AutonomyComponentType.AGENT:
            self._task_handlers["run"] = self._handle_run
            self._task_handlers["chat"] = self._handle_chat
            self._task_handlers["start_session"] = self._handle_start_session
            self._task_handlers["end_session"] = self._handle_end_session
            self._task_handlers["get_session_status"] = self._handle_session_status

        # Reasoning handlers
        if self._component_type in (
            AutonomyComponentType.AGENT,
            AutonomyComponentType.REASONING,
        ):
            self._task_handlers["reason"] = self._handle_reason
            self._task_handlers["plan"] = self._handle_plan
            self._task_handlers["reflect"] = self._handle_reflect
            self._task_handlers["get_reasoning_state"] = self._handle_reasoning_state

        # Tool handlers
        if self._component_type in (
            AutonomyComponentType.AGENT,
            AutonomyComponentType.TOOLS,
        ):
            self._task_handlers["execute_tool"] = self._handle_execute_tool
            self._task_handlers["orchestrate_tools"] = self._handle_orchestrate_tools
            self._task_handlers["get_available_tools"] = self._handle_available_tools

        # Memory handlers
        if self._component_type in (
            AutonomyComponentType.AGENT,
            AutonomyComponentType.MEMORY,
        ):
            self._task_handlers["store_memory"] = self._handle_store_memory
            self._task_handlers["recall_memory"] = self._handle_recall_memory
            self._task_handlers["checkpoint"] = self._handle_checkpoint
            self._task_handlers["restore_checkpoint"] = self._handle_restore_checkpoint

    async def on_initialize(self) -> None:
        """Initialize the adapter and underlying component."""
        logger.info(
            "Initializing AutonomyEngineAdapter for %s",
            self._component_type.value,
        )

        # Initialize component if needed
        if hasattr(self._component, "initialize"):
            initialized = self._component._initialized if hasattr(
                self._component, "_initialized"
            ) else False

            if not initialized:
                result = self._component.initialize()
                if asyncio.iscoroutine(result):
                    await result

        # Subscribe to relevant messages
        await self.subscribe(
            MessageType.TASK_ASSIGNED,
            self._handle_task_assigned,
        )
        await self.subscribe(
            MessageType.KNOWLEDGE_SHARED,
            self._handle_knowledge_shared,
        )
        await self.subscribe(
            MessageType.CUSTOM,
            self._handle_custom_message,
        )

        # Load prior execution context
        if self.knowledge_graph:
            context = await self.query_knowledge(
                query=f"autonomy {self._component_type.value} execution context",
                knowledge_types=[
                    KnowledgeType.INSIGHT,
                    KnowledgeType.PATTERN,
                ],
                limit=10,
            )
            if context:
                logger.info(
                    "Loaded %d prior contexts for %s",
                    len(context),
                    self._component_type.value,
                )

        logger.info(
            "AutonomyEngineAdapter initialized: %s with capabilities %s",
            self.agent_name,
            self.capabilities,
        )

    async def on_start(self) -> None:
        """Called when agent starts processing."""
        logger.info("%s autonomy adapter started", self._component_type.value)

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info("%s autonomy adapter stopping", self._component_type.value)

        # End any active sessions
        for session_id in list(self._active_sessions.keys()):
            await self._end_session(session_id)

        # Cancel pending requests
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()

        # Cleanup component
        for method_name in ("cleanup", "close", "shutdown", "stop"):
            if hasattr(self._component, method_name):
                result = getattr(self._component, method_name)()
                if asyncio.iscoroutine(result):
                    await result
                break

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """Execute an autonomy task.

        Args:
            payload: Task payload with 'action' and 'input' keys

        Returns:
            Task result
        """
        action = payload.get("action", "")
        input_data = payload.get("input", {})

        logger.debug(
            "Executing autonomy task: %s on %s",
            action,
            self._component_type.value,
        )

        # Find handler
        handler = self._task_handlers.get(action)
        if not handler:
            # Try direct method on component
            if hasattr(self._component, action):
                handler = self._create_component_handler(action)
            else:
                raise ValueError(
                    f"Unknown action '{action}' for {self._component_type.value}"
                )

        # Execute
        start_time = datetime.utcnow()
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(input_data)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, handler, input_data)

            # Record execution
            self._record_execution(action, input_data, result, start_time, True)

            return result

        except Exception as e:
            self._record_execution(action, input_data, None, start_time, False, str(e))
            raise

    def _create_component_handler(self, method_name: str) -> Callable:
        """Create handler delegating to component method."""
        method = getattr(self._component, method_name)

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

    def _record_execution(
        self,
        action: str,
        input_data: Dict[str, Any],
        result: Any,
        start_time: datetime,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Record execution in history."""
        record = {
            "action": action,
            "input_summary": str(input_data)[:200],
            "success": success,
            "error": error,
            "started_at": start_time.isoformat(),
            "duration_ms": (datetime.utcnow() - start_time).total_seconds() * 1000,
        }

        self._execution_history.append(record)
        if len(self._execution_history) > self._max_history:
            self._execution_history.pop(0)

    # =========================================================================
    # Agent Handlers
    # =========================================================================

    async def _handle_get_status(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get component status."""
        status = {
            "component_type": self._component_type.value,
            "agent_name": self.agent_name,
            "capabilities": list(self.capabilities),
            "running": self._running,
            "active_sessions": len(self._active_sessions),
            "executions": len(self._execution_history),
        }

        if hasattr(self._component, "get_status"):
            component_status = self._component.get_status()
            if asyncio.iscoroutine(component_status):
                component_status = await component_status
            status["component_status"] = component_status

        return status

    async def _handle_get_metrics(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get execution metrics."""
        if hasattr(self._component, "_metrics"):
            metrics = self._component._metrics
            if hasattr(metrics, "__dict__"):
                return metrics.__dict__
            return {"metrics": metrics}

        # Calculate from history
        total = len(self._execution_history)
        successful = sum(1 for e in self._execution_history if e.get("success"))

        return {
            "total_executions": total,
            "successful": successful,
            "failed": total - successful,
            "success_rate": successful / total if total > 0 else 0,
        }

    async def _handle_run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run autonomous task."""
        goal = input_data.get("goal", "")
        context = input_data.get("context", {})

        if hasattr(self._component, "run"):
            result = self._component.run(goal, context=context)
            if asyncio.iscoroutine(result):
                result = await result

            # Share learning with other agents
            if self.knowledge_graph:
                await self.add_knowledge(
                    knowledge_type=KnowledgeType.PATTERN,
                    data={
                        "type": "autonomous_execution",
                        "goal": goal,
                        "success": result.get("success", True) if isinstance(result, dict) else True,
                        "summary": str(result)[:500],
                    },
                    tags={"autonomy", "execution", "learning"},
                    confidence=0.8,
                )

            return {"result": result}

        return {"error": "No run method available"}

    async def _handle_chat(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Interactive chat with agent."""
        message = input_data.get("message", "")
        context = input_data.get("context", {})

        if hasattr(self._component, "chat"):
            response = self._component.chat(message, context=context)
            if asyncio.iscoroutine(response):
                response = await response
            return {"response": response}

        return {"response": None, "error": "No chat method available"}

    async def _handle_start_session(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Start a new autonomous session."""
        session_id = str(uuid4())
        goal = input_data.get("goal", "")
        mode = input_data.get("mode", "supervised")

        session = {
            "session_id": session_id,
            "goal": goal,
            "mode": mode,
            "started_at": datetime.utcnow().isoformat(),
            "status": "active",
            "action_count": 0,
        }

        self._active_sessions[session_id] = session

        # Notify other agents
        await self.broadcast(
            message_type=MessageType.CUSTOM,
            payload={
                "event": "session_started",
                "session_id": session_id,
                "goal": goal,
            },
        )

        return session

    async def _handle_end_session(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """End an autonomous session."""
        session_id = input_data.get("session_id", "")
        return await self._end_session(session_id)

    async def _end_session(self, session_id: str) -> Dict[str, Any]:
        """Internal session end."""
        if session_id not in self._active_sessions:
            return {"error": f"Session {session_id} not found"}

        session = self._active_sessions.pop(session_id)
        session["status"] = "completed"
        session["completed_at"] = datetime.utcnow().isoformat()

        # Store session insights
        if self.knowledge_graph:
            await self.add_knowledge(
                knowledge_type=KnowledgeType.INSIGHT,
                data={
                    "type": "session_summary",
                    "session": session,
                },
                tags={"autonomy", "session"},
            )

        return session

    async def _handle_session_status(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get session status."""
        session_id = input_data.get("session_id")

        if session_id:
            session = self._active_sessions.get(session_id)
            return {"session": session} if session else {"error": "Session not found"}

        return {"active_sessions": list(self._active_sessions.values())}

    # =========================================================================
    # Reasoning Handlers
    # =========================================================================

    async def _handle_reason(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute reasoning process."""
        problem = input_data.get("problem", "")
        context = input_data.get("context", {})
        max_iterations = input_data.get("max_iterations", 10)

        # Get reasoning engine
        engine = self._get_reasoning_engine()
        if not engine:
            return {"error": "No reasoning engine available"}

        if hasattr(engine, "reason"):
            result = engine.reason(
                problem,
                context=context,
                max_iterations=max_iterations,
            )
            if asyncio.iscoroutine(result):
                result = await result
            return {"reasoning": result}

        if hasattr(engine, "invoke"):
            state = {"input": problem, "context": context}
            result = engine.invoke(state)
            if asyncio.iscoroutine(result):
                result = await result
            return {"reasoning": result}

        return {"error": "No reasoning method available"}

    async def _handle_plan(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create execution plan."""
        goal = input_data.get("goal", "")
        constraints = input_data.get("constraints", [])

        engine = self._get_reasoning_engine()
        if not engine:
            return {"error": "No reasoning engine available"}

        if hasattr(engine, "plan"):
            plan = engine.plan(goal, constraints=constraints)
            if asyncio.iscoroutine(plan):
                plan = await plan
            return {"plan": plan}

        if hasattr(engine, "create_plan"):
            plan = engine.create_plan(goal)
            if asyncio.iscoroutine(plan):
                plan = await plan
            return {"plan": plan}

        return {"plan": None}

    async def _handle_reflect(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Reflect on execution results."""
        execution_result = input_data.get("result", {})
        context = input_data.get("context", {})

        engine = self._get_reasoning_engine()
        if not engine:
            return {"error": "No reasoning engine available"}

        if hasattr(engine, "reflect"):
            reflection = engine.reflect(execution_result, context=context)
            if asyncio.iscoroutine(reflection):
                reflection = await reflection

            # Share reflection insights
            if self.knowledge_graph and reflection:
                await self.add_knowledge(
                    knowledge_type=KnowledgeType.INSIGHT,
                    data={
                        "type": "reflection",
                        "reflection": reflection,
                        "context_summary": str(context)[:200],
                    },
                    tags={"autonomy", "reflection", "learning"},
                )

            return {"reflection": reflection}

        return {"reflection": None}

    async def _handle_reasoning_state(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get current reasoning state."""
        engine = self._get_reasoning_engine()
        if not engine:
            return {"state": None}

        if hasattr(engine, "get_state"):
            state = engine.get_state()
            if asyncio.iscoroutine(state):
                state = await state
            return {"state": state}

        return {"state": None}

    def _get_reasoning_engine(self) -> Optional[Any]:
        """Get reasoning engine from component."""
        if self._component_type == AutonomyComponentType.REASONING:
            return self._component

        if hasattr(self._component, "reasoning_engine"):
            return self._component.reasoning_engine

        if hasattr(self._component, "_reasoning_engine"):
            return self._component._reasoning_engine

        return None

    # =========================================================================
    # Tool Handlers
    # =========================================================================

    async def _handle_execute_tool(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a single tool."""
        tool_name = input_data.get("tool", "")
        arguments = input_data.get("args", {})
        timeout = input_data.get("timeout", 30.0)

        orchestrator = self._get_tool_orchestrator()
        if not orchestrator:
            return {"error": "No tool orchestrator available"}

        if hasattr(orchestrator, "execute_tool"):
            result = orchestrator.execute_tool(
                tool_name,
                arguments,
                timeout=timeout,
            )
            if asyncio.iscoroutine(result):
                result = await result
            return {"result": result}

        if hasattr(orchestrator, "execute"):
            result = orchestrator.execute(tool_name, arguments)
            if asyncio.iscoroutine(result):
                result = await result
            return {"result": result}

        return {"error": f"Cannot execute tool {tool_name}"}

    async def _handle_orchestrate_tools(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Orchestrate multiple tools."""
        tasks = input_data.get("tasks", [])
        strategy = input_data.get("strategy", "sequential")

        orchestrator = self._get_tool_orchestrator()
        if not orchestrator:
            return {"error": "No tool orchestrator available"}

        if hasattr(orchestrator, "execute_batch"):
            results = orchestrator.execute_batch(tasks, strategy=strategy)
            if asyncio.iscoroutine(results):
                results = await results
            return {"results": results}

        # Fallback to sequential execution
        results = []
        for task in tasks:
            result = await self._handle_execute_tool({
                "tool": task.get("tool"),
                "args": task.get("args", {}),
            })
            results.append(result)

        return {"results": results}

    async def _handle_available_tools(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get available tools."""
        orchestrator = self._get_tool_orchestrator()

        tools = []
        if orchestrator:
            if hasattr(orchestrator, "get_available_tools"):
                tools = orchestrator.get_available_tools()
                if asyncio.iscoroutine(tools):
                    tools = await tools
            elif hasattr(orchestrator, "tool_registry"):
                registry = orchestrator.tool_registry
                if hasattr(registry, "list_tools"):
                    tools = registry.list_tools()

        return {"tools": tools}

    def _get_tool_orchestrator(self) -> Optional[Any]:
        """Get tool orchestrator from component."""
        if self._component_type == AutonomyComponentType.TOOLS:
            return self._component

        if hasattr(self._component, "tool_orchestrator"):
            return self._component.tool_orchestrator

        if hasattr(self._component, "_tool_orchestrator"):
            return self._component._tool_orchestrator

        return None

    # =========================================================================
    # Memory Handlers
    # =========================================================================

    async def _handle_store_memory(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Store in memory."""
        content = input_data.get("content", "")
        memory_type = input_data.get("type", "working")
        priority = input_data.get("priority", "normal")

        memory_manager = self._get_memory_manager()
        if not memory_manager:
            # Use knowledge graph as fallback
            if self.knowledge_graph:
                await self.add_knowledge(
                    knowledge_type=KnowledgeType.OBSERVATION,
                    data={"content": content, "memory_type": memory_type},
                    tags={"memory", memory_type},
                )
                return {"success": True, "stored_in": "knowledge_graph"}
            return {"error": "No memory manager available"}

        if hasattr(memory_manager, "store"):
            result = memory_manager.store(content, type=memory_type, priority=priority)
            if asyncio.iscoroutine(result):
                result = await result
            return {"success": True, "result": result}

        return {"success": False}

    async def _handle_recall_memory(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Recall from memory."""
        query = input_data.get("query", "")
        memory_type = input_data.get("type", "all")
        limit = input_data.get("limit", 10)

        memory_manager = self._get_memory_manager()

        memories = []
        if memory_manager and hasattr(memory_manager, "recall"):
            memories = memory_manager.recall(query, type=memory_type, limit=limit)
            if asyncio.iscoroutine(memories):
                memories = await memories

        # Also check knowledge graph
        if self.knowledge_graph:
            kg_memories = await self.query_knowledge(
                query=query,
                knowledge_types=[KnowledgeType.OBSERVATION, KnowledgeType.INSIGHT],
                limit=limit,
            )
            for mem in kg_memories:
                memories.append({
                    "source": "knowledge_graph",
                    "content": mem.data,
                    "confidence": mem.confidence,
                })

        return {"memories": memories}

    async def _handle_checkpoint(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create checkpoint."""
        checkpoint_id = input_data.get("id", str(uuid4()))

        memory_manager = self._get_memory_manager()

        if memory_manager and hasattr(memory_manager, "checkpoint"):
            result = memory_manager.checkpoint(checkpoint_id)
            if asyncio.iscoroutine(result):
                result = await result

            # Share checkpoint info
            self._shared_checkpoints[checkpoint_id] = {
                "created_at": datetime.utcnow().isoformat(),
                "agent": self.agent_name,
            }

            return {"checkpoint_id": checkpoint_id, "success": True}

        # Use component's checkpointer if available
        if hasattr(self._component, "checkpointer"):
            checkpointer = self._component.checkpointer
            if hasattr(checkpointer, "save"):
                checkpointer.save(checkpoint_id)
                return {"checkpoint_id": checkpoint_id, "success": True}

        return {"success": False, "error": "No checkpointing available"}

    async def _handle_restore_checkpoint(
        self,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Restore from checkpoint."""
        checkpoint_id = input_data.get("id", "")

        memory_manager = self._get_memory_manager()

        if memory_manager and hasattr(memory_manager, "restore"):
            result = memory_manager.restore(checkpoint_id)
            if asyncio.iscoroutine(result):
                result = await result
            return {"success": True, "result": result}

        if hasattr(self._component, "checkpointer"):
            checkpointer = self._component.checkpointer
            if hasattr(checkpointer, "load"):
                state = checkpointer.load(checkpoint_id)
                return {"success": True, "state": state}

        return {"success": False, "error": "Checkpoint not found"}

    def _get_memory_manager(self) -> Optional[Any]:
        """Get memory manager from component."""
        if self._component_type == AutonomyComponentType.MEMORY:
            return self._component

        if hasattr(self._component, "memory_manager"):
            return self._component.memory_manager

        if hasattr(self._component, "_memory_manager"):
            return self._component._memory_manager

        return None

    # =========================================================================
    # Message Handlers
    # =========================================================================

    async def _handle_task_assigned(self, message: AgentMessage) -> None:
        """Handle task assignment from orchestrator."""
        task_data = message.payload

        logger.debug(
            "%s received task assignment: %s",
            self.agent_name,
            task_data.get("action"),
        )

        # Execute the task
        try:
            result = await self.execute_task(task_data)

            # Send response
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={"success": True, "result": result},
                    from_agent=self.agent_name,
                )

        except Exception as e:
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={"success": False, "error": str(e)},
                    from_agent=self.agent_name,
                )

    async def _handle_knowledge_shared(self, message: AgentMessage) -> None:
        """Handle knowledge shared by other agents."""
        source = message.payload.get("source", "")
        if source == self.agent_name:
            return

        knowledge_type = message.payload.get("knowledge_type", "")

        # Update component context if relevant
        if knowledge_type in ("execution_result", "learning", "reflection"):
            if hasattr(self._component, "update_context"):
                await self._call_component_method(
                    "update_context",
                    message.payload,
                )

    async def _handle_custom_message(self, message: AgentMessage) -> None:
        """Handle custom messages."""
        event = message.payload.get("event", "")

        if event == "request_tool_execution":
            tool = message.payload.get("tool")
            args = message.payload.get("args", {})

            result = await self._handle_execute_tool({"tool": tool, "args": args})

            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload=result,
                    from_agent=self.agent_name,
                )

        elif event == "request_reasoning":
            problem = message.payload.get("problem")
            result = await self._handle_reason({"problem": problem})

            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload=result,
                    from_agent=self.agent_name,
                )

    async def _call_component_method(
        self,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Safely call component method."""
        if not hasattr(self._component, method_name):
            return None

        method = getattr(self._component, method_name)
        result = method(*args, **kwargs)

        if asyncio.iscoroutine(result):
            return await result
        return result

    @property
    def component(self) -> Any:
        """Access wrapped component."""
        return self._component

    @property
    def component_type(self) -> AutonomyComponentType:
        """Get component type."""
        return self._component_type


# =============================================================================
# Factory Functions
# =============================================================================

async def create_autonomous_agent_adapter(
    agent: Optional[Any] = None,
    agent_name: str = "autonomous_agent",
    **config: Any,
) -> AutonomyEngineAdapter:
    """Create adapter for AutonomousAgent.

    Args:
        agent: Existing agent instance (creates new if None)
        agent_name: Name for the adapter
        **config: Configuration passed to AutonomousAgent

    Returns:
        Configured AutonomyEngineAdapter
    """
    if agent is None:
        try:
            from autonomy.autonomous_agent import AutonomousAgent, AgentConfig

            agent_config = AgentConfig(**config) if config else AgentConfig()
            agent = AutonomousAgent(config=agent_config)
        except ImportError:
            logger.warning("Could not import AutonomousAgent")
            raise

    return AutonomyEngineAdapter(
        autonomy_component=agent,
        component_type=AutonomyComponentType.AGENT,
        agent_name=agent_name,
    )


async def create_reasoning_adapter(
    engine: Optional[Any] = None,
    agent_name: str = "reasoning_engine",
) -> AutonomyEngineAdapter:
    """Create adapter for LangGraphReasoningEngine.

    Args:
        engine: Existing engine instance (creates new if None)
        agent_name: Name for the adapter

    Returns:
        Configured AutonomyEngineAdapter
    """
    if engine is None:
        try:
            from autonomy.langgraph_engine import create_reasoning_engine
            engine = create_reasoning_engine()
        except ImportError:
            logger.warning("Could not import LangGraphReasoningEngine")
            raise

    return AutonomyEngineAdapter(
        autonomy_component=engine,
        component_type=AutonomyComponentType.REASONING,
        agent_name=agent_name,
    )


async def create_tool_orchestrator_adapter(
    orchestrator: Optional[Any] = None,
    agent_name: str = "tool_orchestrator",
) -> AutonomyEngineAdapter:
    """Create adapter for ToolOrchestrator.

    Args:
        orchestrator: Existing orchestrator (creates new if None)
        agent_name: Name for the adapter

    Returns:
        Configured AutonomyEngineAdapter
    """
    if orchestrator is None:
        try:
            from autonomy.tool_orchestrator import create_orchestrator
            orchestrator = create_orchestrator()
        except ImportError:
            logger.warning("Could not import ToolOrchestrator")
            raise

    return AutonomyEngineAdapter(
        autonomy_component=orchestrator,
        component_type=AutonomyComponentType.TOOLS,
        agent_name=agent_name,
    )


async def create_memory_adapter(
    memory_manager: Optional[Any] = None,
    agent_name: str = "memory_manager",
) -> AutonomyEngineAdapter:
    """Create adapter for MemoryManager.

    Args:
        memory_manager: Existing manager (creates new if None)
        agent_name: Name for the adapter

    Returns:
        Configured AutonomyEngineAdapter
    """
    if memory_manager is None:
        try:
            from autonomy.memory_integration import create_memory_manager
            memory_manager = create_memory_manager()
        except ImportError:
            logger.warning("Could not import MemoryManager")
            raise

    return AutonomyEngineAdapter(
        autonomy_component=memory_manager,
        component_type=AutonomyComponentType.MEMORY,
        agent_name=agent_name,
    )


async def create_dual_agent_adapter(
    autonomy_component: Optional[Any] = None,
    agent_name: str = "ouroboros_dual_agent",
) -> AutonomyEngineAdapter:
    """Create an adapter for the Ouroboros DualAgentSystem.

    The DualAgentSystem provides an architect/reviewer pattern for
    code improvement — useful for other mesh agents to invoke for
    code quality analysis and improvement before deployment.

    Args:
        autonomy_component: Existing DualAgentSystem (creates new if None)
        agent_name: Name for the adapter

    Returns:
        Configured AutonomyEngineAdapter
    """
    if autonomy_component is None:
        try:
            from core.ouroboros.native_integration import DualAgentSystem
            autonomy_component = DualAgentSystem()
        except ImportError:
            logger.warning("Could not import DualAgentSystem")
            raise

    return AutonomyEngineAdapter(
        autonomy_component=autonomy_component,
        component_type=AutonomyComponentType.REASONING,
        agent_name=agent_name,
    )


async def create_watchdog_adapter(
    autonomy_component: Optional[Any] = None,
    agent_name: str = "agentic_watchdog",
) -> Optional[AutonomyEngineAdapter]:
    """Create an adapter for the AgenticWatchdog safety layer.

    Exposes watchdog status, operating mode, and kill-switch state
    to the mesh so other agents can query safety constraints before
    taking autonomous actions.

    Returns None (not raise) if the watchdog hasn't been started yet,
    enabling v93.1 graceful degradation in the bridge.

    Args:
        autonomy_component: Existing watchdog instance (discovers if None)
        agent_name: Name for the adapter

    Returns:
        Configured AutonomyEngineAdapter, or None if watchdog not started
    """
    if autonomy_component is None:
        try:
            from core.agentic_watchdog import _watchdog_instance
            if _watchdog_instance is None:
                logger.info("AgenticWatchdog not yet started, skipping adapter")
                return None
            autonomy_component = _watchdog_instance
        except ImportError:
            logger.warning("Could not import AgenticWatchdog")
            raise

    return AutonomyEngineAdapter(
        autonomy_component=autonomy_component,
        component_type=AutonomyComponentType.INTEGRATION,
        agent_name=agent_name,
    )
