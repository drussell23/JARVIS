"""
JARVIS Neural Mesh - Multi-Agent Orchestrator

Workflow coordination for complex multi-agent tasks with:
- Task decomposition (break complex into simple)
- Capability-based agent selection
- Execution strategies (sequential, parallel, hybrid, adaptive)
- Retry logic with exponential backoff
- Timeout management
- Result aggregation

Performance Target: 3-step workflow in <500ms
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
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

from ..data_models import (
    AgentMessage,
    ExecutionStrategy,
    MessagePriority,
    MessageType,
    WorkflowResult,
    WorkflowTask,
)
from ..communication.agent_communication_bus import AgentCommunicationBus
from ..registry.agent_registry import AgentRegistry
from ..knowledge.shared_knowledge_graph import SharedKnowledgeGraph
from ..config import OrchestratorConfig, get_config

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorMetrics:
    """Metrics for the orchestrator."""

    workflows_started: int = 0
    workflows_completed: int = 0
    workflows_failed: int = 0
    tasks_executed: int = 0
    tasks_succeeded: int = 0
    tasks_failed: int = 0
    tasks_retried: int = 0
    average_workflow_time_ms: float = 0.0
    total_workflow_time_ms: float = 0.0


@dataclass
class ActiveWorkflow:
    """State for an active workflow."""

    workflow_id: str
    name: str
    tasks: List[WorkflowTask]
    strategy: ExecutionStrategy
    completed_tasks: Set[str] = field(default_factory=set)
    failed_tasks: Set[str] = field(default_factory=set)
    task_results: Dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=datetime.now)
    timeout_seconds: float = 600.0


class MultiAgentOrchestrator:
    """
    Workflow coordinator for multi-agent tasks.

    Features:
    - Task decomposition for complex queries
    - Automatic agent selection based on capabilities
    - Multiple execution strategies
    - Retry logic with backoff
    - Result aggregation

    Usage:
        orchestrator = MultiAgentOrchestrator(bus, registry, knowledge)
        await orchestrator.start()

        # Define workflow tasks
        tasks = [
            WorkflowTask(
                name="Capture screen",
                required_capability="screen_capture",
            ),
            WorkflowTask(
                name="Analyze errors",
                required_capability="error_detection",
                dependencies=["task_1"],
            ),
        ]

        # Execute workflow
        result = await orchestrator.execute_workflow(
            name="Debug Space 3",
            tasks=tasks,
            strategy=ExecutionStrategy.HYBRID,
        )
    """

    def __init__(
        self,
        communication_bus: AgentCommunicationBus,
        agent_registry: AgentRegistry,
        knowledge_graph: Optional[SharedKnowledgeGraph] = None,
        config: Optional[OrchestratorConfig] = None,
    ) -> None:
        """Initialize the orchestrator.

        Args:
            communication_bus: Message bus for agent communication
            agent_registry: Registry for agent discovery
            knowledge_graph: Knowledge graph for context (optional)
            config: Orchestrator configuration
        """
        self.bus = communication_bus
        self.registry = agent_registry
        self.knowledge = knowledge_graph
        self.config = config or get_config().orchestrator

        # Active workflows: {workflow_id: ActiveWorkflow}
        self._active_workflows: Dict[str, ActiveWorkflow] = {}

        # Metrics
        self._metrics = OrchestratorMetrics()

        # State
        self._running = False

        # Locks
        self._workflow_lock = asyncio.Lock()

        logger.info("MultiAgentOrchestrator initialized")

    async def start(self) -> None:
        """Start the orchestrator."""
        if self._running:
            return

        self._running = True

        # Wire 4: Subscribe to proactive intents from background agents
        try:
            await self.bus.subscribe(
                agent_name="orchestrator",
                message_type=MessageType.PROACTIVE_INTENT,
                callback=self._handle_proactive_intent,
            )
            logger.info("Subscribed to PROACTIVE_INTENT messages")
        except Exception as e:
            logger.debug("Could not subscribe to PROACTIVE_INTENT: %s", e)

        logger.info("MultiAgentOrchestrator started")

    async def stop(self) -> None:
        """Stop the orchestrator gracefully."""
        if not self._running:
            return

        self._running = False

        # Cancel active workflows
        async with self._workflow_lock:
            for workflow in self._active_workflows.values():
                logger.warning(
                    "Cancelling workflow %s on shutdown",
                    workflow.workflow_id[:8],
                )

            self._active_workflows.clear()

        logger.info("MultiAgentOrchestrator stopped")

    async def execute_workflow(
        self,
        name: str,
        tasks: List[WorkflowTask],
        strategy: ExecutionStrategy = ExecutionStrategy.HYBRID,
        timeout_seconds: Optional[float] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> WorkflowResult:
        """
        Execute a multi-agent workflow.

        Args:
            name: Workflow name
            tasks: List of tasks to execute
            strategy: Execution strategy
            timeout_seconds: Overall workflow timeout
            context: Additional context data passed to tasks

        Returns:
            Workflow result with all task results
        """
        if not self._running:
            raise RuntimeError("Orchestrator is not running")

        workflow_id = str(uuid.uuid4())
        timeout = timeout_seconds or self.config.workflow_timeout_seconds

        workflow = ActiveWorkflow(
            workflow_id=workflow_id,
            name=name,
            tasks=tasks,
            strategy=strategy,
            timeout_seconds=timeout,
        )

        async with self._workflow_lock:
            if len(self._active_workflows) >= self.config.max_concurrent_workflows:
                raise RuntimeError(
                    f"Maximum concurrent workflows ({self.config.max_concurrent_workflows}) reached"
                )
            self._active_workflows[workflow_id] = workflow

        self._metrics.workflows_started += 1

        logger.info(
            "Starting workflow %s: %s (%d tasks, strategy=%s)",
            workflow_id[:8],
            name,
            len(tasks),
            strategy.value,
        )

        try:
            # Execute based on strategy
            if strategy == ExecutionStrategy.SEQUENTIAL:
                await self._execute_sequential(workflow, context)
            elif strategy == ExecutionStrategy.PARALLEL:
                await self._execute_parallel(workflow, context)
            elif strategy == ExecutionStrategy.HYBRID:
                await self._execute_hybrid(workflow, context)
            else:  # ADAPTIVE
                await self._execute_adaptive(workflow, context)

            # Build result
            result = WorkflowResult(
                workflow_id=workflow_id,
                name=name,
                status="completed" if not workflow.failed_tasks else "partial",
                tasks=tasks,
                started_at=workflow.started_at,
                completed_at=datetime.now(),
                successful_tasks=len(workflow.completed_tasks),
                failed_tasks=len(workflow.failed_tasks),
                metadata={"context": context} if context else {},
            )

            result.total_execution_time_seconds = (
                result.completed_at - result.started_at
            ).total_seconds()

            # Update metrics
            self._metrics.workflows_completed += 1
            workflow_time_ms = result.total_execution_time_seconds * 1000
            self._metrics.total_workflow_time_ms += workflow_time_ms
            self._metrics.average_workflow_time_ms = (
                self._metrics.total_workflow_time_ms
                / self._metrics.workflows_completed
            )

            logger.info(
                "Workflow %s completed in %.2fms (%d/%d tasks succeeded)",
                workflow_id[:8],
                workflow_time_ms,
                len(workflow.completed_tasks),
                len(tasks),
            )

            # Wire 3: Emit experience to Trinity for Reactor learning.
            # Fire-and-forget — never blocks workflow completion.
            asyncio.create_task(
                self._emit_trinity_experience(result, context),
                name=f"trinity_exp_{workflow_id[:8]}",
            )

            return result

        except asyncio.TimeoutError:
            self._metrics.workflows_failed += 1
            logger.error("Workflow %s timed out", workflow_id[:8])

            return WorkflowResult(
                workflow_id=workflow_id,
                name=name,
                status="timeout",
                tasks=tasks,
                started_at=workflow.started_at,
                completed_at=datetime.now(),
                successful_tasks=len(workflow.completed_tasks),
                failed_tasks=len(tasks) - len(workflow.completed_tasks),
            )

        except Exception as e:
            self._metrics.workflows_failed += 1
            logger.exception("Workflow %s failed: %s", workflow_id[:8], e)

            return WorkflowResult(
                workflow_id=workflow_id,
                name=name,
                status="failed",
                tasks=tasks,
                started_at=workflow.started_at,
                completed_at=datetime.now(),
                metadata={"error": str(e)},
            )

        finally:
            async with self._workflow_lock:
                self._active_workflows.pop(workflow_id, None)

    async def _execute_sequential(
        self,
        workflow: ActiveWorkflow,
        context: Optional[Dict[str, Any]],
    ) -> None:
        """Execute tasks one after another."""
        for task in workflow.tasks:
            await self._execute_task(workflow, task, context)

    async def _execute_parallel(
        self,
        workflow: ActiveWorkflow,
        context: Optional[Dict[str, Any]],
    ) -> None:
        """Execute all tasks simultaneously."""
        await asyncio.gather(
            *[
                self._execute_task(workflow, task, context)
                for task in workflow.tasks
            ],
            return_exceptions=True,
        )

    async def _execute_hybrid(
        self,
        workflow: ActiveWorkflow,
        context: Optional[Dict[str, Any]],
    ) -> None:
        """Execute tasks based on dependencies."""
        pending = set(task.task_id for task in workflow.tasks)
        task_map = {task.task_id: task for task in workflow.tasks}

        while pending:
            # Find tasks ready to execute (all dependencies met)
            ready = [
                task_map[tid]
                for tid in pending
                if task_map[tid].is_ready(workflow.completed_tasks)
            ]

            if not ready:
                # No tasks ready but some pending - dependency issue
                if pending:
                    logger.error(
                        "Workflow %s: No ready tasks but %d pending",
                        workflow.workflow_id[:8],
                        len(pending),
                    )
                break

            # Execute ready tasks in parallel
            await asyncio.gather(
                *[
                    self._execute_task(workflow, task, context)
                    for task in ready
                ],
                return_exceptions=True,
            )

            # Remove executed tasks from pending
            for task in ready:
                pending.discard(task.task_id)

    async def _execute_adaptive(
        self,
        workflow: ActiveWorkflow,
        context: Optional[Dict[str, Any]],
    ) -> None:
        """Adaptively choose execution strategy based on system state."""
        # Check system load
        agents = await self.registry.get_all_agents()
        avg_load = sum(a.load for a in agents) / len(agents) if agents else 0

        if avg_load > 0.7:
            # High load - use sequential to avoid overload
            logger.debug(
                "Adaptive: Using sequential (avg load %.2f)",
                avg_load,
            )
            await self._execute_sequential(workflow, context)
        else:
            # Normal load - use hybrid
            logger.debug(
                "Adaptive: Using hybrid (avg load %.2f)",
                avg_load,
            )
            await self._execute_hybrid(workflow, context)

    async def _execute_task(
        self,
        workflow: ActiveWorkflow,
        task: WorkflowTask,
        context: Optional[Dict[str, Any]],
    ) -> Any:
        """Execute a single task with retry logic."""
        self._metrics.tasks_executed += 1
        task.started_at = datetime.now()
        task.status = "running"

        # Validate action BEFORE dispatch — empty action causes ValueError in adapters
        action = task.required_capability
        if not action or not action.strip():
            task.status = "failed"
            task.error = (
                f"Task '{task.name or task.task_id}' has empty required_capability — "
                f"cannot dispatch without a valid action"
            )
            workflow.failed_tasks.add(task.task_id)
            self._metrics.tasks_failed += 1
            logger.error("Task %s rejected: empty required_capability", task.task_id[:8])
            return None

        # Find capable agent — 3-tier resolution:
        # 1. Primary capability
        # 2. Fallback capability (if set)
        # 3. ComputerUseAgent as universal last resort
        agent = await self.registry.get_best_agent(action)

        if not agent and task.fallback_capability:
            agent = await self.registry.get_best_agent(task.fallback_capability)
            if agent:
                logger.info(
                    "Task %s: primary '%s' unavailable, using fallback '%s'",
                    task.task_id[:8],
                    action,
                    task.fallback_capability,
                )

        if not agent and action != "computer_use":
            # Wire 5: ComputerUseAgent as universal fallback —
            # anything an agent can do via API, ComputerUse can do visually
            agent = await self.registry.get_best_agent("computer_use")
            if agent:
                logger.info(
                    "Task %s: no agent for '%s', falling back to ComputerUseAgent",
                    task.task_id[:8],
                    action,
                )

        if not agent:
            task.status = "failed"
            task.error = f"No agent found for capability: {action}"
            workflow.failed_tasks.add(task.task_id)
            self._metrics.tasks_failed += 1
            logger.error(
                "Task %s failed: %s",
                task.task_id[:8],
                task.error,
            )
            return None

        task.assigned_agent = agent.agent_name

        # Prepare task payload
        payload = {
            "task_id": task.task_id,
            "action": action,
            "input": task.input_data,
            "context": context or {},
            "workflow_id": workflow.workflow_id,
        }

        # Add results from dependencies
        for dep_id in task.dependencies:
            if dep_id in workflow.task_results:
                payload[f"dep_{dep_id}"] = workflow.task_results[dep_id]

        # Execute with retry
        retry_count = 0
        retry_delay = task.retry_delay_seconds

        while retry_count <= task.retry_count:
            try:
                # Send task to agent
                message = AgentMessage(
                    from_agent="orchestrator",
                    to_agent=agent.agent_name,
                    message_type=MessageType.TASK_ASSIGNED,
                    payload=payload,
                    priority=task.priority,
                )

                result = await self.bus.request(
                    message,
                    timeout=task.timeout_seconds,
                )

                # Success
                task.status = "completed"
                task.result = result
                task.completed_at = datetime.now()
                workflow.completed_tasks.add(task.task_id)
                workflow.task_results[task.task_id] = result

                self._metrics.tasks_succeeded += 1

                logger.debug(
                    "Task %s completed by %s in %.2fms",
                    task.task_id[:8],
                    agent.agent_name,
                    (task.execution_time_seconds() or 0) * 1000,
                )

                return result

            except asyncio.TimeoutError:
                retry_count += 1
                self._metrics.tasks_retried += 1
                logger.warning(
                    "Task %s timed out (attempt %d/%d)",
                    task.task_id[:8],
                    retry_count,
                    task.retry_count + 1,
                )

            except Exception as e:
                retry_count += 1
                self._metrics.tasks_retried += 1
                logger.warning(
                    "Task %s failed (attempt %d/%d): %s",
                    task.task_id[:8],
                    retry_count,
                    task.retry_count + 1,
                    e,
                )

            if retry_count <= task.retry_count:
                await asyncio.sleep(retry_delay)
                retry_delay *= self.config.retry_backoff_multiplier

        # All retries exhausted with primary agent.
        # Wire 5: Try ComputerUseAgent as visual fallback before giving up.
        if agent.agent_name != "computer_use_agent":
            cu_agent = await self.registry.get_best_agent("computer_use")
            if cu_agent:
                logger.info(
                    "Task %s: primary agent '%s' exhausted retries, "
                    "attempting ComputerUseAgent visual fallback",
                    task.task_id[:8],
                    agent.agent_name,
                )
                try:
                    cu_message = AgentMessage(
                        from_agent="orchestrator",
                        to_agent=cu_agent.agent_name,
                        message_type=MessageType.TASK_ASSIGNED,
                        payload=payload,
                        priority=task.priority,
                    )
                    result = await self.bus.request(
                        cu_message,
                        timeout=task.timeout_seconds,
                    )

                    # Visual fallback succeeded
                    task.status = "completed"
                    task.result = result
                    task.completed_at = datetime.now()
                    workflow.completed_tasks.add(task.task_id)
                    workflow.task_results[task.task_id] = result
                    self._metrics.tasks_succeeded += 1

                    logger.info(
                        "Task %s recovered via ComputerUseAgent visual fallback",
                        task.task_id[:8],
                    )
                    return result

                except Exception as cu_err:
                    logger.warning(
                        "Task %s: ComputerUseAgent fallback also failed: %s",
                        task.task_id[:8],
                        cu_err,
                    )

        # Truly exhausted — all paths failed
        task.status = "failed"
        task.error = f"Failed after {retry_count} attempts"
        task.completed_at = datetime.now()
        workflow.failed_tasks.add(task.task_id)
        self._metrics.tasks_failed += 1

        logger.error(
            "Task %s failed after %d attempts",
            task.task_id[:8],
            retry_count,
        )

        return None

    async def create_workflow_from_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[WorkflowTask]:
        """
        Create workflow tasks from a natural language query.

        This implementation uses intelligent routing based on query intent:
        - Workspace queries → GoogleWorkspaceAgent
        - Debug queries → Error detection workflow
        - Analysis queries → Context + code analysis workflow

        Args:
            query: Natural language query
            context: Additional context

        Returns:
            List of workflow tasks
        """
        tasks: List[WorkflowTask] = []
        query_lower = query.lower()

        # =================================================================
        # WORKSPACE ROUTING (Calendar, Email, Contacts)
        # =================================================================
        workspace_keywords = [
            # Calendar
            "calendar", "schedule", "meetings", "events", "agenda",
            "what's on my calendar", "what meetings", "my schedule",
            "busy", "free time", "availability", "appointments",
            # Email
            "email", "inbox", "mail", "unread", "messages", "draft",
            "send email", "check email", "reply to",
            # Contacts
            "contact", "phone number", "email address for",
            # Briefing
            "briefing", "summary", "catch me up", "what's happening",
        ]

        is_workspace_query = any(kw in query_lower for kw in workspace_keywords)

        if is_workspace_query:
            # Route to Google Workspace Agent
            tasks.append(WorkflowTask(
                name="Handle workspace query",
                description=f"Process workspace request: {query[:50]}...",
                required_capability="handle_workspace_query",
                input_data={"query": query},
                priority=MessagePriority.HIGH,  # User-facing, prioritize
            ))
            return tasks

        # =================================================================
        # DEBUG / ERROR WORKFLOW
        # =================================================================
        if "debug" in query_lower or "error" in query_lower:
            tasks.append(WorkflowTask(
                name="Capture current state",
                description="Capture screen or relevant context",
                required_capability="screen_capture",
            ))
            tasks.append(WorkflowTask(
                name="Detect errors",
                description="Analyze for errors or issues",
                required_capability="error_detection",
                dependencies=[tasks[0].task_id] if tasks else [],
            ))
            tasks.append(WorkflowTask(
                name="Query solutions",
                description="Search knowledge for solutions",
                required_capability="knowledge_query",
                dependencies=[tasks[1].task_id] if len(tasks) > 1 else [],
            ))

        # =================================================================
        # ANALYSIS WORKFLOW
        # =================================================================
        elif "analyze" in query_lower or "review" in query_lower:
            tasks.append(WorkflowTask(
                name="Gather context",
                description="Collect relevant information",
                required_capability="context_analysis",
            ))
            tasks.append(WorkflowTask(
                name="Perform analysis",
                description="Analyze the gathered context",
                required_capability="code_analysis",
                dependencies=[tasks[0].task_id] if tasks else [],
            ))

        # =================================================================
        # SCREEN CAPTURE
        # =================================================================
        elif "capture" in query_lower or "screenshot" in query_lower:
            tasks.append(WorkflowTask(
                name="Capture screen",
                description="Capture screen content",
                required_capability="screen_capture",
            ))

        # =================================================================
        # GENERIC FALLBACK
        # =================================================================
        else:
            tasks.append(WorkflowTask(
                name="Process query",
                description=query,
                required_capability="general_processing",
            ))

        return tasks

    async def route_natural_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Intelligently route a natural language query to the right agent.

        This is a convenience method that:
        1. Detects the query intent
        2. Creates appropriate workflow tasks
        3. Executes the workflow
        4. Returns the result

        Args:
            query: Natural language query from user
            context: Optional additional context

        Returns:
            Result from the appropriate agent
        """
        # Create workflow from query
        tasks = await self.create_workflow_from_query(query, context)

        if not tasks:
            return {
                "status": "error",
                "error": "Could not determine how to handle this query",
            }

        # Execute as single-step or multi-step workflow
        if len(tasks) == 1:
            # Single task - execute directly
            task = tasks[0]
            action = task.required_capability
            if not action or not action.strip():
                return {
                    "status": "error",
                    "error": f"Task '{task.name or task.task_id}' has empty required_capability",
                }

            agent = await self.registry.get_best_agent(action)

            if not agent:
                return {
                    "status": "error",
                    "error": f"No agent found with capability: {action}",
                }

            # Send task
            message = AgentMessage(
                from_agent="orchestrator",
                to_agent=agent.agent_name,
                message_type=MessageType.TASK_ASSIGNED,
                payload={
                    "action": action,
                    **task.input_data,
                },
                priority=task.priority,
            )

            try:
                result = await self.bus.request(message, timeout=task.timeout_seconds)
                return {
                    "status": "success",
                    "agent": agent.agent_name,
                    "result": result,
                }
            except Exception as e:
                return {
                    "status": "error",
                    "agent": agent.agent_name,
                    "error": str(e),
                }
        else:
            # Multi-step workflow
            result = await self.execute_workflow(
                name=f"Query: {query[:30]}...",
                tasks=tasks,
                strategy=ExecutionStrategy.HYBRID,
                context=context,
            )
            return result.to_dict()

    # =========================================================================
    # Wire 2: Intent → Plan → Execute (via PredictivePlanningAgent)
    # =========================================================================

    async def plan_and_execute(
        self,
        query: str,
        strategy: ExecutionStrategy = ExecutionStrategy.HYBRID,
        timeout_seconds: Optional[float] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> WorkflowResult:
        """
        End-to-end: expand a user intent into tasks and execute them.

        This is the primary entry point for turning natural language
        commands into multi-agent workflows. It bridges:

        PredictivePlanningAgent (Psychic Brain) → MultiAgentOrchestrator (Parallel Muscle)

        Flow:
        1. PredictivePlanningAgent.expand_intent(query) → PredictionResult
        2. PredictivePlanningAgent.to_workflow_tasks(result) → List[WorkflowTask]
        3. MultiAgentOrchestrator.execute_workflow(tasks) → WorkflowResult

        Falls back to create_workflow_from_query() if the planning agent
        is unavailable (graceful degradation).

        Args:
            query: Natural language command (e.g., "Start my day")
            strategy: Execution strategy (default HYBRID for dependency-aware)
            timeout_seconds: Overall workflow timeout
            context: Additional context passed to all tasks

        Returns:
            WorkflowResult with all task outcomes
        """
        if not self._running:
            raise RuntimeError("Orchestrator is not running")

        # Try to get the PredictivePlanningAgent via registry
        planning_agent = await self.registry.get_best_agent("expand_intent")

        if planning_agent:
            try:
                # Single call: plan_to_workflow expands intent AND converts
                # to WorkflowTask objects in one round-trip
                plan_message = AgentMessage(
                    from_agent="orchestrator",
                    to_agent=planning_agent.agent_name,
                    message_type=MessageType.TASK_ASSIGNED,
                    payload={
                        "action": "plan_to_workflow",
                        "query": query,
                    },
                    priority=MessagePriority.HIGH,
                )

                planning_timeout = getattr(
                    self.config, "planning_timeout_seconds", 15.0
                )
                plan_result = await self.bus.request(
                    plan_message,
                    timeout=planning_timeout,
                )

                # Extract workflow tasks and prediction metadata.
                # Defensive: if the bus serialized WorkflowTask objects to
                # dicts (e.g. JSON transport), reconstruct them here.
                workflow_tasks: List[WorkflowTask] = []
                prediction_data: Optional[Dict[str, Any]] = None

                if isinstance(plan_result, dict):
                    raw_tasks = plan_result.get("workflow_tasks", [])
                    prediction_data = plan_result.get("prediction")

                    for item in raw_tasks:
                        if isinstance(item, WorkflowTask):
                            workflow_tasks.append(item)
                        elif isinstance(item, dict):
                            # Reconstruct from serialized dict
                            try:
                                pri = item.get("priority", MessagePriority.NORMAL)
                                if isinstance(pri, str):
                                    pri = MessagePriority[pri.upper()]
                                elif isinstance(pri, int):
                                    pri = MessagePriority(pri)

                                workflow_tasks.append(WorkflowTask(
                                    task_id=item.get("task_id", str(uuid.uuid4())),
                                    name=item.get("name", ""),
                                    description=item.get("description", ""),
                                    required_capability=item.get("required_capability", "general_processing"),
                                    input_data=item.get("input_data", {}),
                                    dependencies=item.get("dependencies", []),
                                    timeout_seconds=item.get("timeout_seconds", 30.0),
                                    retry_count=item.get("retry_count", 3),
                                    retry_delay_seconds=item.get("retry_delay_seconds", 1.0),
                                    fallback_capability=item.get("fallback_capability"),
                                    priority=pri,
                                ))
                            except Exception as deser_err:
                                logger.warning(
                                    "plan_and_execute: failed to deserialize task dict: %s",
                                    deser_err,
                                )

                if workflow_tasks:
                    # Enrich context with prediction metadata
                    enriched_context = dict(context or {})
                    enriched_context["prediction"] = {
                        "original_query": query,
                        "source": planning_agent.agent_name,
                    }
                    if prediction_data:
                        enriched_context["prediction"]["intent"] = prediction_data.get(
                            "detected_intent", "unknown"
                        )
                        enriched_context["prediction"]["confidence"] = prediction_data.get(
                            "confidence", 0.0
                        )

                    logger.info(
                        "plan_and_execute: expanded '%s' into %d tasks via %s",
                        query[:40],
                        len(workflow_tasks),
                        planning_agent.agent_name,
                    )

                    return await self.execute_workflow(
                        name=f"Plan: {query[:50]}",
                        tasks=workflow_tasks,
                        strategy=strategy,
                        timeout_seconds=timeout_seconds,
                        context=enriched_context,
                    )

                logger.warning(
                    "plan_and_execute: planning agent returned empty tasks, "
                    "falling back to create_workflow_from_query"
                )

            except asyncio.TimeoutError:
                logger.warning(
                    "plan_and_execute: planning agent timed out, "
                    "falling back to create_workflow_from_query"
                )
            except Exception as e:
                logger.warning(
                    "plan_and_execute: planning agent failed (%s), "
                    "falling back to create_workflow_from_query",
                    e,
                )

        else:
            logger.info(
                "plan_and_execute: no planning agent registered, "
                "using create_workflow_from_query"
            )

        # Graceful degradation: fall back to keyword-based task creation
        tasks = await self.create_workflow_from_query(query, context)
        if not tasks:
            return WorkflowResult(
                workflow_id=str(uuid.uuid4()),
                name=f"Plan: {query[:50]}",
                status="failed",
                tasks=[],
                metadata={"error": "Could not create tasks from query"},
            )

        return await self.execute_workflow(
            name=f"Plan: {query[:50]}",
            tasks=tasks,
            strategy=strategy,
            timeout_seconds=timeout_seconds,
            context=context,
        )

    # =========================================================================
    # Wire 3: Trinity Experience Pipeline Integration
    # =========================================================================

    async def _emit_trinity_experience(
        self,
        result: WorkflowResult,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit a completed workflow as a Trinity experience for Reactor learning.

        Fire-and-forget — exceptions are logged, never propagated.
        Uses the established Trinity event bus pattern (publish_raw) so
        Reactor-Core can:
        - Learn which task expansions succeed/fail
        - Improve agent selection over time
        - Detect patterns in multi-agent workflow outcomes
        """
        try:
            from core.trinity_event_bus import get_event_bus_if_exists

            bus = get_event_bus_if_exists()
            if bus is None:
                return

            # Build experience payload with full workflow trace
            task_traces = []
            for task in result.tasks:
                task_traces.append({
                    "task_id": task.task_id,
                    "name": task.name,
                    "capability": task.required_capability,
                    "fallback": task.fallback_capability,
                    "status": task.status,
                    "assigned_agent": task.assigned_agent,
                    "execution_time_s": task.execution_time_seconds(),
                    "error": task.error,
                })

            experience_payload: Dict[str, Any] = {
                "workflow_id": result.workflow_id,
                "workflow_name": result.name,
                "status": result.status,
                "tasks_total": len(result.tasks),
                "tasks_succeeded": result.successful_tasks,
                "tasks_failed": result.failed_tasks,
                "execution_time_s": result.total_execution_time_seconds,
                "task_traces": task_traces,
            }

            # Include prediction context if available
            if context and isinstance(context, dict):
                prediction = context.get("prediction")
                if prediction and isinstance(prediction, dict):
                    experience_payload["intent"] = prediction.get("intent")
                    experience_payload["confidence"] = prediction.get("confidence")
                    experience_payload["original_query"] = prediction.get(
                        "original_query"
                    )

            await bus.publish_raw(
                topic="workflow.completed",
                data=experience_payload,
            )

            logger.debug(
                "Trinity experience emitted for workflow %s (%s)",
                result.workflow_id[:8],
                result.status,
            )

        except ImportError:
            pass  # Trinity event bus not available in this environment
        except Exception as e:
            logger.debug(
                "Trinity experience emission failed for workflow %s: %s",
                result.workflow_id[:8],
                e,
            )

    # =========================================================================
    # Wire 4: Proactive Intent Handler
    # =========================================================================

    async def _handle_proactive_intent(self, message: AgentMessage) -> None:
        """Handle a proactive intent from a background perception agent.

        Background agents (VisualMonitor, ActivityRecognition, etc.) publish
        PROACTIVE_INTENT messages when they detect something actionable:
        - "Meeting in 5 minutes" → meeting prep workflow
        - "Error dialog on screen" → debug workflow
        - "User idle for 30 min" → end-of-day suggestions
        - "New Slack message from boss" → communication workflow

        The intent is routed through plan_and_execute() for full multi-agent
        expansion, or directly to a specific agent if the payload specifies one.

        Payload format:
            {
                "intent": str,           # Natural language intent description
                "source_agent": str,     # Which agent detected this
                "confidence": float,     # 0.0-1.0
                "urgency": str,          # "low", "normal", "high", "critical"
                "context": dict,         # Agent-specific context
            }
        """
        if not self._running:
            return

        payload = message.payload
        intent = payload.get("intent", "")
        source = payload.get("source_agent", message.from_agent)
        confidence = payload.get("confidence", 0.5)
        urgency = payload.get("urgency", "normal")

        if not intent:
            logger.debug("Ignoring empty proactive intent from %s", source)
            return

        # Confidence gate — don't act on low-confidence background detections
        min_confidence = float(
            getattr(self.config, "proactive_min_confidence", 0.7)
        )
        if confidence < min_confidence:
            logger.debug(
                "Proactive intent from %s below threshold (%.2f < %.2f): %s",
                source,
                confidence,
                min_confidence,
                intent[:60],
            )
            return

        logger.info(
            "Proactive intent from %s (confidence=%.2f, urgency=%s): %s",
            source,
            confidence,
            urgency,
            intent[:80],
        )

        # Map urgency to execution strategy
        strategy = ExecutionStrategy.HYBRID
        if urgency == "critical":
            strategy = ExecutionStrategy.PARALLEL  # Fastest possible

        try:
            result = await self.plan_and_execute(
                query=intent,
                strategy=strategy,
                context={
                    "source": "proactive_intent",
                    "source_agent": source,
                    "confidence": confidence,
                    "urgency": urgency,
                    **(payload.get("context") or {}),
                },
            )

            logger.info(
                "Proactive workflow completed: %s (%d/%d tasks, %.1fs)",
                result.status,
                result.successful_tasks,
                len(result.tasks),
                result.total_execution_time_seconds,
            )

            # Narrate result to user if high urgency
            if urgency in ("high", "critical"):
                try:
                    from backend.core.supervisor.unified_voice_orchestrator import (
                        safe_say,
                    )

                    task_count = result.successful_tasks
                    await safe_say(
                        f"I noticed something and handled it — "
                        f"completed {task_count} tasks proactively.",
                        source="proactive_narration",
                    )
                except Exception:
                    pass  # Narration is best-effort

        except Exception as e:
            logger.warning(
                "Proactive intent execution failed for '%s': %s",
                intent[:60],
                e,
            )

    def get_metrics(self) -> OrchestratorMetrics:
        """Get current orchestrator metrics."""
        return self._metrics

    def get_active_workflows(self) -> List[Dict[str, Any]]:
        """Get info about active workflows."""
        return [
            {
                "workflow_id": w.workflow_id,
                "name": w.name,
                "tasks": len(w.tasks),
                "completed": len(w.completed_tasks),
                "failed": len(w.failed_tasks),
                "started_at": w.started_at.isoformat(),
            }
            for w in self._active_workflows.values()
        ]

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"MultiAgentOrchestrator("
            f"active={len(self._active_workflows)}, "
            f"completed={self._metrics.workflows_completed}, "
            f"failed={self._metrics.workflows_failed}"
            f")"
        )
