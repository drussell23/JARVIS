"""RuntimeTaskOrchestrator — universal dispatcher for any user request.

The bridge between "user says something" and "agents figure it out."
Connects three systems that were previously independent:

1. Neural Mesh (runtime task execution) — browser, native apps, web search
2. Ouroboros Governance (code changes) — generate, validate, apply
3. Topology Package (capability synthesis) — create what doesn't exist

Decision flow for ANY request:
    User: "search YouTube for CS videos and play the first one"
      ↓
    PredictivePlanningAgent decomposes into steps
      ↓
    For each step, check: does an agent with this capability exist?
      ├─ YES → route to existing Neural Mesh agent
      ├─ NO, but simple tool needed → synthesize EPHEMERAL tool via J-Prime
      └─ NO, and it's a recurring need → synthesize PERSISTENT capability via Ouroboros
      ↓
    MultiAgentOrchestrator executes the workflow (parallel where possible)
      ↓
    ConsciousnessMemory records what worked for next time

Design:
    - Zero hardcoded agent-to-task mappings
    - AgentRegistry is the single source of truth for "what can I do?"
    - TopologyMap is the single source of truth for "what DON'T I know?"
    - ComplexityClassifier decides ephemeral vs persistent
    - All async, all parallel where dependencies allow
    - Trinity Consciousness feeds memory + prediction into every decision

This is the module that makes JARVIS universal — tell it anything,
it figures out how to do it.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class TaskResolution(str, Enum):
    """How a step was resolved."""
    EXISTING_AGENT = "existing_agent"      # found in AgentRegistry
    EPHEMERAL_TOOL = "ephemeral_tool"      # synthesized on-the-fly, discarded after
    GOVERNANCE_OP = "governance_op"        # routed to Ouroboros for code change
    PERSISTENT_PROPOSAL = "persistent"     # synthesized + proposed for permanent add
    UNRESOLVABLE = "unresolvable"          # no agent, no synthesis possible


@dataclass(frozen=True)
class StepResolution:
    """How a single step in a task plan was resolved."""
    step_goal: str
    resolution: TaskResolution
    agent_name: Optional[str]            # which agent handled it
    capability_used: Optional[str]       # which capability was matched
    synthesized: bool                    # was a new tool created for this?
    result: Optional[Any] = None
    error: Optional[str] = None
    elapsed_s: float = 0.0


@dataclass
class TaskResult:
    """Complete result of a runtime task."""
    task_id: str
    original_query: str
    steps: List[StepResolution]
    success: bool
    summary: str
    total_elapsed_s: float
    plan_reasoning: str


class RuntimeTaskOrchestrator:
    """Universal task dispatcher — handles any user request dynamically.

    Injected dependencies (all optional, graceful degradation):
        registry:       AgentRegistry for capability discovery
        orchestrator:   MultiAgentOrchestrator for workflow execution
        planner:        PredictivePlanningAgent for goal decomposition
        tier_router:    ExecutionTierRouter for API/native/browser decision
        topology:       TopologyMap for capability gap detection
        classifier:     OperationComplexityClassifier for ephemeral/persistent
        consciousness:  ConsciousnessBridge for memory + prediction
        gls:            GovernedLoopService for code change operations
        prime_client:   PrimeClient for on-the-fly tool synthesis
    """

    def __init__(
        self,
        registry: Any = None,
        orchestrator: Any = None,
        planner: Any = None,
        tier_router: Any = None,
        topology: Any = None,
        classifier: Any = None,
        consciousness: Any = None,
        gls: Any = None,
        prime_client: Any = None,
        telemetry_bus: Any = None,
    ) -> None:
        self._registry = registry
        self._orchestrator = orchestrator
        self._planner = planner
        self._tier_router = tier_router
        self._topology = topology
        self._classifier = classifier
        self._consciousness = consciousness
        self._gls = gls
        self._prime = prime_client
        self._bus = telemetry_bus
        self._live_agents: Dict[str, Any] = {}  # lazy agent instance cache

    async def execute(self, query: str, context: Optional[Dict[str, Any]] = None) -> TaskResult:
        """Execute any user request. The universal entry point.

        Args:
            query: Natural language request ("search YouTube for CS videos")
            context: Optional context (current app, screen state, etc.)

        Returns:
            TaskResult with step-by-step resolution and overall success.
        """
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        start = time.monotonic()
        ctx = context or {}

        logger.info("[RuntimeTask] %s: %s", task_id, query)

        # --- Step 1: Decompose the goal into executable steps ---
        plan_steps, plan_reasoning = await self._decompose(query, ctx)
        if not plan_steps:
            return TaskResult(
                task_id=task_id,
                original_query=query,
                steps=[],
                success=False,
                summary="Could not decompose request into actionable steps",
                total_elapsed_s=time.monotonic() - start,
                plan_reasoning=plan_reasoning,
            )

        logger.info("[RuntimeTask] %s: %d steps planned", task_id, len(plan_steps))

        # --- Step 2: Resolve each step to an agent or synthesis path ---
        resolutions: List[StepResolution] = []
        for step in plan_steps:
            resolution = await self._resolve_step(step, ctx)
            resolutions.append(resolution)

        # --- Step 3: Execute resolved steps (respecting dependencies) ---
        executed = await self._execute_steps(resolutions, plan_steps, ctx)

        # --- Step 4: Record outcome in consciousness memory ---
        await self._record_outcome(task_id, query, executed)

        success = all(s.error is None for s in executed)
        elapsed = time.monotonic() - start

        summary = self._build_summary(query, executed)
        logger.info("[RuntimeTask] %s: %s (%.1fs)", task_id, "SUCCESS" if success else "PARTIAL", elapsed)

        result = TaskResult(
            task_id=task_id,
            original_query=query,
            steps=executed,
            success=success,
            summary=summary,
            total_elapsed_s=elapsed,
            plan_reasoning=plan_reasoning,
        )

        # --- Step 5: Emit telemetry for Trinity-wide observability ---
        # This feeds: EliteDashboard ticker, LifecycleVoiceNarrator, ConsciousnessBridge
        self._emit_task_telemetry(result)

        return result

    # -----------------------------------------------------------------------
    # Step 1: Goal Decomposition
    # -----------------------------------------------------------------------

    async def _decompose(
        self, query: str, ctx: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Decompose a natural language goal into executable steps.

        Uses PredictivePlanningAgent if available, falls back to
        single-step execution for simple commands.
        """
        if self._planner is not None:
            try:
                prediction = await self._planner.expand_intent(query)
                steps = []
                for task in prediction.expanded_tasks:
                    steps.append({
                        "goal": task.goal,
                        "priority": task.priority,
                        "target_app": getattr(task, "target_app", None),
                        "workspace_service": getattr(task, "workspace_service", None),
                        "dependencies": getattr(task, "dependencies", []),
                        "category": str(getattr(task, "category", "")),
                    })
                return steps, prediction.reasoning
            except Exception as exc:
                logger.warning("[RuntimeTask] PredictivePlanner failed: %s — using single step", exc)

        # Fallback: treat entire query as one step
        return [{"goal": query, "priority": 1, "dependencies": []}], "single-step fallback"

    # -----------------------------------------------------------------------
    # Step 2: Capability Resolution
    # -----------------------------------------------------------------------

    async def _resolve_step(
        self, step: Dict[str, Any], ctx: Dict[str, Any],
    ) -> StepResolution:
        """Resolve a single step to an agent, synthesis path, or failure."""
        goal = step["goal"]
        start = time.monotonic()

        # Check 1: AgentRegistry — does an existing agent handle this?
        agent_name, capability = await self._find_agent(goal, step)
        if agent_name:
            return StepResolution(
                step_goal=goal,
                resolution=TaskResolution.EXISTING_AGENT,
                agent_name=agent_name,
                capability_used=capability,
                synthesized=False,
                elapsed_s=time.monotonic() - start,
            )

        # Check 2: Is this a code change? Route to Ouroboros governance.
        if self._is_code_change(goal):
            return StepResolution(
                step_goal=goal,
                resolution=TaskResolution.GOVERNANCE_OP,
                agent_name="ouroboros_governance",
                capability_used="code_generation",
                synthesized=False,
                elapsed_s=time.monotonic() - start,
            )

        # Check 3: TopologyMap — is this a known gap we can synthesize?
        if self._topology is not None:
            matched_gap = self._find_topology_gap(goal)
            if matched_gap:
                # Classify: ephemeral or persistent?
                persistence = self._classify_persistence(goal)
                resolution = (
                    TaskResolution.PERSISTENT_PROPOSAL
                    if persistence == "persistent"
                    else TaskResolution.EPHEMERAL_TOOL
                )
                return StepResolution(
                    step_goal=goal,
                    resolution=resolution,
                    agent_name=None,
                    capability_used=matched_gap,
                    synthesized=True,
                    elapsed_s=time.monotonic() - start,
                )

        # Check 4: Can we synthesize an ephemeral tool via J-Prime?
        if self._prime is not None:
            return StepResolution(
                step_goal=goal,
                resolution=TaskResolution.EPHEMERAL_TOOL,
                agent_name=None,
                capability_used=None,
                synthesized=True,
                elapsed_s=time.monotonic() - start,
            )

        # Nothing matched
        return StepResolution(
            step_goal=goal,
            resolution=TaskResolution.UNRESOLVABLE,
            agent_name=None,
            capability_used=None,
            synthesized=False,
            error=f"No agent or synthesis path for: {goal}",
            elapsed_s=time.monotonic() - start,
        )

    async def _find_agent(
        self, goal: str, step: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[str]]:
        """Find an existing agent that can handle this goal."""
        if self._registry is None:
            return None, None

        # Strategy 1: ExecutionTierRouter decides the tier
        tier = None
        if self._tier_router is not None:
            try:
                tier = self._tier_router.decide_tier(
                    goal=goal,
                    workspace_service=step.get("workspace_service"),
                    target_app=step.get("target_app"),
                )
            except Exception:
                pass

        # Strategy 2: Search by capability keywords from the goal
        goal_lower = goal.lower()
        capability_keywords = self._extract_capability_keywords(goal_lower)

        for keyword in capability_keywords:
            try:
                agents = await self._registry.find_by_capability(keyword)
                if agents:
                    # Pick the healthiest, lowest-load agent
                    best = min(agents, key=lambda a: getattr(a, "load", 0))
                    return best.agent_name, keyword
            except Exception:
                continue

        # Strategy 3: Tier-based fallback
        if tier is not None:
            tier_to_agent = {
                "browser": "visual_browser_agent",
                "native_app": "native_app_control_agent",
                "api": "google_workspace_agent",
            }
            agent_name = tier_to_agent.get(str(tier).lower())
            if agent_name:
                try:
                    info = await self._registry.get_agent(agent_name)
                    if info:
                        return agent_name, str(tier)
                except Exception:
                    pass

        return None, None

    @staticmethod
    def _extract_capability_keywords(goal_lower: str) -> List[str]:
        """Extract capability keywords from a goal string."""
        keywords = []
        # Browser/web patterns
        if any(w in goal_lower for w in ("browse", "website", "url", "youtube", "google", "search online")):
            keywords.extend(["visual_browser", "web_browsing", "browser"])
        # Search patterns
        if any(w in goal_lower for w in ("search", "find", "look up", "research")):
            keywords.extend(["web_search", "search", "research"])
        # App control patterns
        if any(w in goal_lower for w in ("open", "launch", "switch to", "close app")):
            keywords.extend(["native_app_control", "app_control", "open_app"])
        # Email/calendar
        if any(w in goal_lower for w in ("email", "gmail", "send mail", "calendar", "meeting", "schedule")):
            keywords.extend(["google_workspace", "email", "calendar"])
        # Code patterns
        if any(w in goal_lower for w in ("code", "fix bug", "implement", "refactor", "write function")):
            keywords.extend(["code_generation", "code_edit"])
        # Shell/terminal patterns
        if any(w in goal_lower for w in ("run", "terminal", "command", "shell", "npm", "pip", "git")):
            keywords.extend(["bash", "shell_execution", "terminal"])
        # Vision patterns
        if any(w in goal_lower for w in ("screen", "screenshot", "what's on", "look at")):
            keywords.extend(["vision", "screen_capture", "visual_monitor"])
        # Memory patterns
        if any(w in goal_lower for w in ("remember", "recall", "what did", "history")):
            keywords.extend(["memory", "knowledge_retrieval"])
        # General — add the goal words themselves as capabilities
        for word in goal_lower.split():
            if len(word) > 3 and word.isalpha():
                keywords.append(word)
        return keywords

    @staticmethod
    def _is_code_change(goal: str) -> bool:
        """Detect if this is a code change request (route to Ouroboros)."""
        code_signals = [
            "fix bug", "implement", "refactor", "add feature", "update code",
            "write function", "create module", "modify file", "change the code",
            "add test", "fix the", "patch", "debug",
        ]
        goal_lower = goal.lower()
        return any(signal in goal_lower for signal in code_signals)

    def _find_topology_gap(self, goal: str) -> Optional[str]:
        """Check TopologyMap for known inactive capabilities matching the goal."""
        if self._topology is None:
            return None
        goal_lower = goal.lower()
        for name, node in self._topology.nodes.items():
            if not node.active:
                name_words = name.replace("_", " ")
                if name_words in goal_lower or node.domain in goal_lower:
                    return name
        return None

    def _classify_persistence(self, goal: str) -> str:
        """Classify whether a synthesized tool should be ephemeral or persistent."""
        if self._classifier is not None:
            try:
                result = self._classifier.classify(
                    description=goal,
                    target_files=[],
                )
                return result.persistence.value
            except Exception:
                pass
        return "ephemeral"  # safe default

    # -----------------------------------------------------------------------
    # Step 3: Execution
    # -----------------------------------------------------------------------

    async def _execute_steps(
        self,
        resolutions: List[StepResolution],
        plan_steps: List[Dict[str, Any]],
        ctx: Dict[str, Any],
    ) -> List[StepResolution]:
        """Execute all resolved steps using the DAG execution engine.

        Primary: DAGPlanner builds a dependency graph → DAGExecutor runs it
        with event-driven concurrency (independent nodes run in parallel).
        Fallback: MultiAgentOrchestrator or sequential execution.
        """
        # Primary: DAG execution engine (event-driven concurrency)
        try:
            from backend.core.dag_execution_engine import DAGPlanner, DAGExecutor, NodeState
            dag_plan = DAGPlanner.build(plan_steps, self._make_dag_node_executor(resolutions))
            dag_executor = DAGExecutor(max_concurrency=8)
            dag_result = await dag_executor.execute(dag_plan, workflow_context=ctx)

            # Map DAG results back to StepResolutions
            executed = []
            for i, (resolution, step) in enumerate(zip(resolutions, plan_steps)):
                node = dag_result.nodes.get(f"node-{i}")
                if node and node.state == NodeState.COMPLETED:
                    executed.append(StepResolution(
                        step_goal=step["goal"],
                        resolution=resolution.resolution,
                        agent_name=resolution.agent_name,
                        capability_used=resolution.capability_used,
                        synthesized=resolution.synthesized,
                        result=node.result,
                        elapsed_s=node.elapsed_s,
                    ))
                elif node and node.state in (NodeState.FAILED, NodeState.BLOCKED):
                    executed.append(StepResolution(
                        step_goal=step["goal"],
                        resolution=resolution.resolution,
                        agent_name=resolution.agent_name,
                        capability_used=resolution.capability_used,
                        synthesized=resolution.synthesized,
                        error=node.error,
                        elapsed_s=node.elapsed_s,
                    ))
                else:
                    executed.append(resolution)
            return executed

        except Exception as exc:
            logger.warning("[RuntimeTask] DAG execution failed: %s — falling back", exc)

        # Fallback 1: MultiAgentOrchestrator
        if self._orchestrator is not None and self._planner is not None:
            try:
                return await self._execute_via_orchestrator(resolutions, plan_steps)
            except Exception as exc:
                logger.warning("[RuntimeTask] Orchestrator fallback failed: %s", exc)

        # Fallback 2: sequential execution
        executed: List[StepResolution] = []
        for resolution, step in zip(resolutions, plan_steps):
            result = await self._execute_single(resolution, step, ctx)
            executed.append(result)

        return executed

    def _make_dag_node_executor(
        self, resolutions: List[StepResolution],
    ) -> Any:
        """Create a DAG node executor that routes to the right dispatch method.

        Returns an async callable(node, context) -> result that the DAGExecutor
        calls for each node. The executor captures `self` and `resolutions` in
        closure so each node is dispatched according to its resolution type.
        """
        resolution_map = {f"node-{i}": r for i, r in enumerate(resolutions)}
        orchestrator = self

        async def _executor(node: Any, context: Dict[str, Any]) -> Any:
            resolution = resolution_map.get(node.node_id)
            if resolution is None:
                return {"error": f"No resolution for {node.node_id}"}

            step = context  # DAG passes metadata as context

            if resolution.resolution == TaskResolution.EXISTING_AGENT:
                return await orchestrator._dispatch_to_agent(
                    resolution.agent_name or "", node.goal, step,
                )
            elif resolution.resolution == TaskResolution.GOVERNANCE_OP:
                return await orchestrator._dispatch_to_governance(node.goal, step)
            elif resolution.resolution in (
                TaskResolution.EPHEMERAL_TOOL,
                TaskResolution.PERSISTENT_PROPOSAL,
            ):
                return await orchestrator._synthesize_and_execute(
                    node.goal, step,
                    ephemeral=(resolution.resolution == TaskResolution.EPHEMERAL_TOOL),
                )
            else:
                return {"error": f"Unresolvable: {node.goal}"}

        return _executor

    async def _execute_via_orchestrator(
        self,
        resolutions: List[StepResolution],
        plan_steps: List[Dict[str, Any]],
    ) -> List[StepResolution]:
        """Execute steps via MultiAgentOrchestrator for parallel scheduling."""
        try:
            from backend.neural_mesh.data_models import WorkflowTask, MessagePriority
        except ImportError:
            raise RuntimeError("Neural Mesh data_models not available")

        workflow_tasks = []
        for i, (resolution, step) in enumerate(zip(resolutions, plan_steps)):
            if resolution.resolution == TaskResolution.UNRESOLVABLE:
                continue
            task = WorkflowTask(
                task_id=f"step-{i}",
                name=step["goal"][:50],
                description=step["goal"],
                required_capability=resolution.capability_used or "general",
                input_data={"goal": step["goal"], **step},
                dependencies=[f"step-{j}" for j, dep in enumerate(step.get("dependencies", [])) if dep],
                timeout_seconds=120.0,
                priority=MessagePriority.HIGH,
                assigned_agent=resolution.agent_name,
            )
            workflow_tasks.append(task)

        if not workflow_tasks:
            return list(resolutions)

        from backend.neural_mesh.orchestration.multi_agent_orchestrator import ExecutionStrategy
        workflow_result = await self._orchestrator.execute_workflow(
            name=f"runtime_task_{int(time.time())}",
            tasks=workflow_tasks,
            strategy=ExecutionStrategy.HYBRID,
        )

        # Map results back to StepResolutions
        executed = []
        task_map = {t.task_id: t for t in workflow_result.tasks}
        for i, (resolution, step) in enumerate(zip(resolutions, plan_steps)):
            task = task_map.get(f"step-{i}")
            if task and task.status == "completed":
                executed.append(StepResolution(
                    step_goal=step["goal"],
                    resolution=resolution.resolution,
                    agent_name=task.assigned_agent or resolution.agent_name,
                    capability_used=resolution.capability_used,
                    synthesized=resolution.synthesized,
                    result=task.result,
                    elapsed_s=(task.completed_at - task.started_at).total_seconds() if task.completed_at and task.started_at else 0,
                ))
            elif task and task.error:
                executed.append(StepResolution(
                    step_goal=step["goal"],
                    resolution=resolution.resolution,
                    agent_name=resolution.agent_name,
                    capability_used=resolution.capability_used,
                    synthesized=resolution.synthesized,
                    error=task.error,
                ))
            else:
                executed.append(resolution)

        return executed

    async def _execute_single(
        self,
        resolution: StepResolution,
        step: Dict[str, Any],
        ctx: Dict[str, Any],
    ) -> StepResolution:
        """Execute a single step based on its resolution type."""
        start = time.monotonic()
        goal = step["goal"]

        try:
            if resolution.resolution == TaskResolution.EXISTING_AGENT:
                result = await self._dispatch_to_agent(resolution.agent_name, goal, step)
                return StepResolution(
                    step_goal=goal,
                    resolution=resolution.resolution,
                    agent_name=resolution.agent_name,
                    capability_used=resolution.capability_used,
                    synthesized=False,
                    result=result,
                    elapsed_s=time.monotonic() - start,
                )

            elif resolution.resolution == TaskResolution.GOVERNANCE_OP:
                result = await self._dispatch_to_governance(goal, step)
                return StepResolution(
                    step_goal=goal,
                    resolution=resolution.resolution,
                    agent_name="ouroboros_governance",
                    capability_used="code_generation",
                    synthesized=False,
                    result=result,
                    elapsed_s=time.monotonic() - start,
                )

            elif resolution.resolution == TaskResolution.EPHEMERAL_TOOL:
                result = await self._synthesize_and_execute(goal, step, ephemeral=True)
                return StepResolution(
                    step_goal=goal,
                    resolution=resolution.resolution,
                    agent_name="ephemeral_synthesized",
                    capability_used=resolution.capability_used,
                    synthesized=True,
                    result=result,
                    elapsed_s=time.monotonic() - start,
                )

            elif resolution.resolution == TaskResolution.PERSISTENT_PROPOSAL:
                result = await self._synthesize_and_execute(goal, step, ephemeral=False)
                return StepResolution(
                    step_goal=goal,
                    resolution=resolution.resolution,
                    agent_name="persistent_synthesized",
                    capability_used=resolution.capability_used,
                    synthesized=True,
                    result=result,
                    elapsed_s=time.monotonic() - start,
                )

            else:
                return resolution  # UNRESOLVABLE — return as-is

        except Exception as exc:
            return StepResolution(
                step_goal=goal,
                resolution=resolution.resolution,
                agent_name=resolution.agent_name,
                capability_used=resolution.capability_used,
                synthesized=resolution.synthesized,
                error=str(exc),
                elapsed_s=time.monotonic() - start,
            )

    # Lazy-instantiated agent cache — maps agent_name → live agent instance
    _live_agents: Dict[str, Any] = {}

    # Agent class registry — maps agent_name → (module_path, class_name)
    _AGENT_CLASSES: Dict[str, Tuple[str, str]] = {
        "visual_browser_agent": ("backend.neural_mesh.agents.visual_browser_agent", "VisualBrowserAgent"),
        "web_search_agent": ("backend.neural_mesh.agents.web_search_agent", "WebSearchAgent"),
        "native_app_control_agent": ("backend.neural_mesh.agents.native_app_control_agent", "NativeAppControlAgent"),
        "google_workspace_agent": ("backend.neural_mesh.agents.google_workspace_agent", "GoogleWorkspaceAgent"),
        "spatial_awareness_agent": ("backend.neural_mesh.agents.spatial_awareness_agent", "SpatialAwarenessAgent"),
    }

    async def _get_live_agent(self, agent_name: str) -> Any:
        """Get or lazily create a live agent instance for dispatch."""
        if agent_name in self._live_agents:
            return self._live_agents[agent_name]

        entry = self._AGENT_CLASSES.get(agent_name)
        if entry is None:
            return None

        module_path, class_name = entry
        try:
            import importlib
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            instance = cls()
            self._live_agents[agent_name] = instance
            logger.info("[RuntimeTask] Lazy-instantiated live agent: %s", agent_name)
            return instance
        except Exception as exc:
            logger.warning("[RuntimeTask] Failed to instantiate %s: %s", agent_name, exc)
            return None

    async def _dispatch_to_agent(
        self, agent_name: str, goal: str, step: Dict[str, Any],
    ) -> Any:
        """Dispatch a task to an existing Neural Mesh agent."""
        if self._registry is None:
            raise RuntimeError("AgentRegistry not available")

        # Build payload for the agent — do NOT set "action" key;
        # each agent's execute_task() decides its own default action.
        payload = {
            "goal": goal,
        }
        if step.get("target_app"):
            payload["app_name"] = step["target_app"]
        if step.get("workspace_service"):
            payload["service"] = step["workspace_service"]
        if "url" in goal.lower() or "youtube" in goal.lower() or "browse" in goal.lower():
            payload["url"] = self._extract_url(goal)

        # --- Primary: Get a live agent instance and call execute_task() ---
        live_agent = await self._get_live_agent(agent_name)
        if live_agent is not None and hasattr(live_agent, "execute_task"):
            logger.info("[RuntimeTask] Dispatching to live agent %s: %s", agent_name, goal[:80])
            return await live_agent.execute_task(payload)

        # --- Fallback: Try coordinator delegation ---
        try:
            coordinator_info = await self._registry.get_agent("coordinator_agent")
            if coordinator_info and hasattr(coordinator_info, "request"):
                return await coordinator_info.request(
                    to_agent=agent_name,
                    payload=payload,
                    timeout=60.0,
                )
        except Exception:
            pass

        # --- Fallback: Direct dispatch if registry entry has execute_task ---
        agent_info = await self._registry.get_agent(agent_name)
        if agent_info and hasattr(agent_info, "execute_task"):
            return await agent_info.execute_task(payload)

        raise RuntimeError(
            f"Agent '{agent_name}' has no live instance and cannot be instantiated. "
            f"Goal: {goal}"
        )

    async def _dispatch_to_governance(self, goal: str, step: Dict[str, Any]) -> Any:
        """Route a code change request to Ouroboros governance pipeline."""
        if self._gls is None:
            return {"status": "error", "error": "Governance pipeline not available"}

        try:
            from backend.core.ouroboros.governance.op_context import OperationContext
            ctx = OperationContext.create(
                target_files=tuple(step.get("target_files", [])),
                description=goal,
                primary_repo="jarvis",
            )
            result = await self._gls.submit(ctx, trigger_source="runtime_task_orchestrator")
            return {"status": "submitted", "op_id": ctx.op_id, "result": str(result)}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def _synthesize_and_execute(
        self, goal: str, step: Dict[str, Any], ephemeral: bool,
    ) -> Any:
        """Synthesize a new tool via J-Prime and execute it.

        If ephemeral=True, the tool is discarded after execution.
        If ephemeral=False, an ArchitecturalProposal is created.
        """
        if self._prime is None:
            return {"status": "error", "error": "PrimeClient not available for synthesis"}

        # Ask J-Prime to generate a one-shot tool
        prompt = (
            f"Generate a Python async function that accomplishes this goal:\n"
            f"Goal: {goal}\n\n"
            f"Requirements:\n"
            f"- Single async function named 'execute'\n"
            f"- Takes no arguments (or a dict context)\n"
            f"- Returns a dict with 'success' bool and 'result' string\n"
            f"- Uses only standard library + aiohttp if needed\n"
            f"- Must be safe to run in a sandbox\n"
        )

        try:
            response = await self._prime.generate(
                prompt=prompt,
                system_prompt="Generate a single Python async function. Output only the code, no explanation.",
                max_tokens=2048,
                temperature=0.3,
                model_name=None,
                task_profile=None,
            )
            logger.info(
                "[RuntimeTask] J-Prime synthesized %s tool for: %s (%d tokens)",
                "ephemeral" if ephemeral else "persistent",
                goal[:50],
                response.tokens_used,
            )

            # Execute the synthesized code in the SandboxedExecutor blast chamber
            from backend.core.topology.sandboxed_executor import SandboxedExecutor
            executor = SandboxedExecutor(
                reactor_client=None,  # ReactorCoreClient injected when available
                telemetry_bus=self._bus,
            )
            exec_result = await executor.execute(
                code=response.content,
                goal=goal,
                context=step,
                ephemeral=ephemeral,
            )

            logger.info(
                "[RuntimeTask] Execution result: %s (mode=%s, %.1fs)",
                exec_result.outcome.value,
                exec_result.mode.value,
                exec_result.elapsed_seconds,
            )

            # Record usage for graduation tracking
            if exec_result.outcome.value == "success":
                _tracker = getattr(self, "_graduation_tracker", None)
                if _tracker is not None:
                    _gcid = await _tracker.record_usage(
                        goal=goal,
                        code_hash=exec_result.code_hash,
                        outcome=exec_result.outcome.value,
                        elapsed_s=exec_result.elapsed_seconds,
                    )
                    # Threshold reached — trigger graduation in background
                    if _gcid is not None:
                        _grad = getattr(self, "_graduation_orchestrator", None)
                        if _grad is not None:
                            _records = _tracker.get_records(_gcid)
                            asyncio.create_task(
                                _grad.evaluate_graduation(_gcid, _records),
                                name=f"graduation_{_gcid}",
                            )

            return {
                "status": exec_result.outcome.value,
                "ephemeral": ephemeral,
                "goal": goal,
                "return_value": exec_result.return_value,
                "stdout": exec_result.stdout[:500] if exec_result.stdout else "",
                "code_hash": exec_result.code_hash,
                "execution_mode": exec_result.mode.value,
                "error": exec_result.error_message,
            }
        except Exception as exc:
            return {"status": "error", "error": f"Synthesis failed: {exc}"}

    @staticmethod
    def _extract_url(goal: str) -> str:
        """Extract a URL from a goal string, or construct a search URL."""
        import re
        from urllib.parse import quote_plus

        url_match = re.search(r'https?://\S+', goal)
        if url_match:
            return url_match.group(0)

        goal_lower = goal.lower()

        # YouTube search — extract the search term from the goal
        if "youtube" in goal_lower:
            # Remove common command prefixes to isolate the search query
            search_term = re.sub(
                r"^(search|find|look up|browse|go to|open|play)\s+",
                "", goal_lower,
            )
            search_term = re.sub(
                r"\b(on|for|in|at|from)\s+(youtube|yt)\b",
                "", search_term,
            )
            search_term = re.sub(r"\byoutube\b", "", search_term).strip()
            if search_term:
                return f"https://www.youtube.com/results?search_query={quote_plus(search_term)}"
            return "https://www.youtube.com"

        if "google" in goal_lower:
            search_term = re.sub(
                r"^(search|find|look up|browse)\s+",
                "", goal_lower,
            )
            search_term = re.sub(
                r"\b(on|for|in|at|from)\s+google\b",
                "", search_term,
            )
            search_term = re.sub(r"\bgoogle\b", "", search_term).strip()
            if search_term:
                return f"https://www.google.com/search?q={quote_plus(search_term)}"
            return "https://www.google.com"

        return ""

    # -----------------------------------------------------------------------
    # Step 4: Memory & Summary
    # -----------------------------------------------------------------------

    async def _record_outcome(
        self, task_id: str, query: str, steps: List[StepResolution],
    ) -> None:
        """Record the task outcome in consciousness memory."""
        if self._consciousness is None:
            return
        try:
            success = all(s.error is None for s in steps)
            agents_used = [s.agent_name for s in steps if s.agent_name]
            await self._consciousness.record_operation_outcome(
                op_id=task_id,
                files_changed=agents_used,
                success=success,
                failure_reason=next((s.error for s in steps if s.error), None),
            )
        except Exception:
            pass

    @staticmethod
    def _build_summary(query: str, steps: List[StepResolution]) -> str:
        """Build a human-readable summary of what happened."""
        total = len(steps)
        succeeded = sum(1 for s in steps if s.error is None)
        failed = total - succeeded
        synthesized = sum(1 for s in steps if s.synthesized)

        parts = [f"{succeeded}/{total} steps completed"]
        if synthesized:
            parts.append(f"{synthesized} tool(s) synthesized on-the-fly")
        if failed:
            errors = [s.error for s in steps if s.error]
            parts.append(f"{failed} failed: {'; '.join(errors[:3])}")

        return " | ".join(parts)

    # -----------------------------------------------------------------------
    # Step 5: Telemetry — Trinity-wide observability
    # -----------------------------------------------------------------------

    def _emit_task_telemetry(self, result: TaskResult) -> None:
        """Emit a TelemetryEnvelope so all Trinity systems see action outcomes.

        Feeds: EliteDashboard ticker, LifecycleVoiceNarrator, ConsciousnessBridge,
        MacOSHostObserver, and any TelemetryBus subscriber.
        """
        if self._bus is None:
            return
        try:
            from backend.core.telemetry_contract import TelemetryEnvelope
            agents_used = [s.agent_name for s in result.steps if s.agent_name]
            envelope = TelemetryEnvelope.create(
                event_schema="reasoning.decision@1.0.0",
                source="runtime_task_orchestrator",
                trace_id=result.task_id,
                span_id="execution_complete",
                partition_key="reasoning",
                payload={
                    "command": result.original_query[:200],
                    "success": result.success,
                    "steps_total": len(result.steps),
                    "steps_succeeded": sum(1 for s in result.steps if s.error is None),
                    "agents_used": agents_used,
                    "synthesized_count": sum(1 for s in result.steps if s.synthesized),
                    "elapsed_s": round(result.total_elapsed_s, 2),
                    "plan_reasoning": result.plan_reasoning[:200],
                },
            )
            self._bus.emit(envelope)
        except Exception as exc:
            logger.debug("[RuntimeTask] Telemetry emit failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_rto_instance: Optional[RuntimeTaskOrchestrator] = None


def get_runtime_task_orchestrator() -> Optional[RuntimeTaskOrchestrator]:
    """Get the singleton RuntimeTaskOrchestrator (set by Zone 6.12 at boot).

    Returns None if the supervisor hasn't initialized it yet.
    Use ``set_runtime_task_orchestrator()`` to register the instance.
    """
    return _rto_instance


def set_runtime_task_orchestrator(instance: RuntimeTaskOrchestrator) -> None:
    """Register the singleton RuntimeTaskOrchestrator (called by Zone 6.12)."""
    global _rto_instance
    _rto_instance = instance
    logger.info("[RuntimeTask] Singleton registered")
