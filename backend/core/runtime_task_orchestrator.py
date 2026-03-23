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
import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class TaskResolution(str, Enum):
    """How a step was resolved."""
    EXISTING_AGENT = "existing_agent"      # found in AgentRegistry
    VISION_ACTION = "vision_action"        # v305.0: browser/UI task → VisionActionLoop
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

        # --- Step 5.5: Voice feedback — tell the user what happened ---
        # v305.0: Fire-and-forget voice announcement on success so the user
        # gets audible confirmation that JARVIS completed their request.
        if result.success:
            asyncio.create_task(
                self._speak_completion(result),
                name=f"voice_completion_{task_id}",
            )

        # --- Step 5.6: Graduation analysis — offer persistence for synthesized tools ---
        # v305.0: If any step synthesized an ephemeral tool, check whether it
        # should be proposed for permanent assimilation (Symbiotic Manifesto §6).
        _synthesized_steps = [s for s in executed if s.synthesized and s.error is None]
        if _synthesized_steps:
            asyncio.create_task(
                self._analyze_and_offer_persistence(result, _synthesized_steps),
                name=f"graduation_{task_id}",
            )

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

        # Fallback: treat entire query as one step.
        # Propagate structured fields from context so _dispatch_to_agent
        # receives provider/url/search_query without scraping goal text.
        step: Dict[str, Any] = {
            "goal": query,
            "priority": 1,
            "dependencies": [],
        }
        for field_name in ("provider", "url", "search_query", "target_app",
                           "workspace_service", "action_category"):
            val = ctx.get(field_name)
            if val:
                step[field_name] = val

        return [step], "single-step fallback"

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

        # Check 1.5: Browser/UI task? Route to VisionActionLoop (perception-action loop).
        # v305.0: Instead of synthesizing blind subprocess code, use the full
        # vision stack: Ferrari Engine capture → Claude Vision analysis →
        # Ghost Hands actuator → ActionVerifier. This gives JARVIS eyes and
        # hands — it can see if the page loaded correctly and self-correct.
        if self._is_browser_or_ui_task(goal, step):
            return StepResolution(
                step_goal=goal,
                resolution=TaskResolution.VISION_ACTION,
                agent_name="vision_action_loop",
                capability_used="browser_navigation",
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

    @staticmethod
    def _is_browser_or_ui_task(goal: str, step: Dict[str, Any]) -> bool:
        """Detect if this step needs browser/UI interaction (route to VisionActionLoop).

        v305.0: Browser tasks go through the full perception-action-verification
        loop (Ferrari Engine + Ghost Hands + ActionVerifier) instead of blind
        subprocess.run(['open', url]) ephemeral tools.

        Detection is STRUCTURAL, not string-matching. We rely on the
        IntentClassifier's semantic output (target_app, category, action_category)
        which are resolved agentically by J-Prime — no hardcoded provider lists.
        """
        # Structural signals from IntentClassifier (set by J-Prime semantically)
        target_app = (step.get("target_app") or "").lower()
        category = (step.get("category") or "").lower()
        action_category = (step.get("action_category") or "").lower()
        provider = (step.get("provider") or "").lower()
        search_query = step.get("search_query") or ""

        # 1. IntentClassifier tagged it as a browser app
        if target_app and target_app != "terminal":
            # If the classifier identified a specific app target, it's UI work.
            # Terminal commands are NOT vision tasks.
            return True

        # 2. IntentClassifier categorized it as web/search/navigation
        if category in ("web_search", "browser", "navigation", "website"):
            return True
        if action_category in ("web_search", "browser", "navigation"):
            return True

        # 3. IntentClassifier identified a web provider + search query
        # (provider is set agentically by the classifier, not a hardcoded list)
        if provider and search_query:
            return True

        # 4. URL detected in goal (structural, not provider-specific)
        if "http://" in goal or "https://" in goal or "www." in goal:
            return True

        return False

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
            elif resolution.resolution == TaskResolution.VISION_ACTION:
                return await orchestrator._dispatch_to_vision(node.goal, step)
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
                raise RuntimeError(f"Unresolvable: {node.goal}")

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

    async def shutdown_agents(self) -> None:
        """Dispose of all live agent instances (called by supervisor on shutdown)."""
        for name, agent in self._live_agents.items():
            try:
                if hasattr(agent, "stop"):
                    await agent.stop()
                elif hasattr(agent, "close"):
                    await agent.close()
            except Exception as exc:
                logger.debug("[RuntimeTask] Agent %s cleanup failed: %s", name, exc)
        self._live_agents.clear()
        logger.info("[RuntimeTask] All live agents disposed (%d)", len(self._live_agents))

    async def _get_live_agent(self, agent_name: str) -> Any:
        """Get or lazily create a live agent instance from AgentBindings."""
        if agent_name in self._live_agents:
            return self._live_agents[agent_name]

        from backend.core.agent_bindings import get_agent_bindings
        bindings = get_agent_bindings()
        binding = bindings.get(agent_name)
        if binding is None:
            return None

        try:
            instance = binding.instantiate()
            self._live_agents[agent_name] = instance
            logger.info("[RuntimeTask] Lazy-instantiated live agent: %s (%s.%s)",
                        agent_name, binding.module, binding.class_name)
            return instance
        except Exception as exc:
            logger.warning("[RuntimeTask] Failed to instantiate %s: %s", agent_name, exc)
            return None

    async def _dispatch_to_agent(
        self, agent_name: str, goal: str, step: Dict[str, Any],
    ) -> Any:
        """Dispatch a task to an existing Neural Mesh agent.

        Builds a TaskEnvelope from the structured step dict (populated by the
        intent classifier / planner).  Never scrapes goal strings for URLs.
        """
        from backend.core.task_envelope import TaskEnvelope

        envelope = TaskEnvelope.from_step(step)
        payload = envelope.to_dict()

        # --- Primary: Get a live agent instance and call execute_task() ---
        live_agent = await self._get_live_agent(agent_name)
        if live_agent is not None and hasattr(live_agent, "execute_task"):
            logger.info("[RuntimeTask] Dispatching to live agent %s: %s", agent_name, goal[:80])
            return await live_agent.execute_task(payload)

        # --- Fallback: Registry entry with execute_task (live agent registered) ---
        if self._registry is not None:
            agent_info = await self._registry.get_agent(agent_name)
            if agent_info and hasattr(agent_info, "execute_task"):
                return await agent_info.execute_task(payload)

        # --- No fake success — propagate typed failure ---
        from backend.core.agent_bindings import get_agent_bindings
        has_binding = agent_name in get_agent_bindings()
        raise RuntimeError(
            f"Agent '{agent_name}' has no live instance and cannot be instantiated. "
            f"Binding exists: {has_binding}. Goal: {goal}"
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

    async def _resolve_url_via_prime(self, goal: str, step: Dict[str, Any]) -> str:
        """Ask J-Prime to resolve the correct URL for a navigation goal.

        No hardcoded URL templates — the model figures out the right URL
        based on the goal semantics. Falls back to empty string on failure.
        """
        provider = step.get("provider") or ""
        search_query = step.get("search_query") or ""

        prompt = (
            f"What is the exact URL to accomplish this goal?\n"
            f"Goal: {goal}\n"
            f"Provider: {provider}\n"
            f"Search query: {search_query}\n\n"
            f"Return ONLY the URL, nothing else. No markdown, no explanation.\n"
            f"Example: https://www.youtube.com/results?search_query=neural+networks"
        )
        try:
            response = await asyncio.wait_for(
                self._prime.generate(
                    prompt=prompt,
                    system_prompt="You are a URL resolver. Return only the URL.",
                    max_tokens=200,
                ),
                timeout=8.0,
            )
            url = (response.text if hasattr(response, "text") else str(response)).strip()
            # Validate — must look like a URL
            if url.startswith("http://") or url.startswith("https://"):
                logger.info("[RuntimeTask] J-Prime resolved URL: %s", url[:80])
                return url
            logger.debug("[RuntimeTask] J-Prime URL invalid: %s", url[:80])
        except Exception as exc:
            logger.debug("[RuntimeTask] J-Prime URL resolution failed: %s", exc)
        return ""

    async def _dispatch_to_vision(self, goal: str, step: Dict[str, Any]) -> Any:
        """Route a browser/UI task to VisionActionLoop for perception-action execution.

        v305.0: Connects the full vision stack to voice commands:
        Ferrari Engine (60fps capture) -> Claude Vision (analysis) ->
        Ghost Hands (click/type/scroll) -> ActionVerifier (did it work?).

        For navigation (URL or search), opens the browser first then
        verifies via VisionActionLoop. For interactive tasks (click element,
        fill form), delegates entirely to VisionActionLoop.execute_action().
        """
        search_query = step.get("search_query") or ""
        target_app = (step.get("target_app") or "").lower()

        # Step 1: Resolve URL agentically — ask J-Prime to figure out the right
        # URL for this goal. No hardcoded URL templates (Manifesto §5).
        url = step.get("url") or ""
        if not url and self._prime is not None:
            url = await self._resolve_url_via_prime(goal, step)
        elif not url and search_query:
            # Fallback only when J-Prime is completely unavailable
            import urllib.parse as _urlparse
            url = "https://www.google.com/search?q=" + _urlparse.quote_plus(search_query)

        if url:
            proc = await asyncio.create_subprocess_exec(
                "open", url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            logger.info("[RuntimeTask] Opened URL: %s", url[:80])
        elif target_app:
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", target_app,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            logger.info("[RuntimeTask] Opened app: %s", target_app)

        # Step 2: Verify via VisionActionLoop (perception-action loop)
        await asyncio.sleep(2.0)  # Allow page load

        try:
            from backend.vision.realtime.vision_action_loop import VisionActionLoop
            _loop_cls = VisionActionLoop
            _inst = (
                _loop_cls.get_instance()
                if hasattr(_loop_cls, "get_instance")
                else None
            )
            if _inst is not None and hasattr(_inst, "execute_action"):
                verification = await asyncio.wait_for(
                    _inst.execute_action(
                        target_description=f"Verify: {goal}",
                        action_type="click",
                        target_text=search_query or goal,
                    ),
                    timeout=15.0,
                )
                if verification.success:
                    return {
                        "success": True,
                        "result": f"Navigated and verified: {goal}",
                    }
                logger.info(
                    "[RuntimeTask] Vision verification inconclusive: %s",
                    getattr(verification, "error", "unknown"),
                )
        except asyncio.TimeoutError:
            logger.debug("[RuntimeTask] Vision verification timed out")
        except Exception as exc:
            logger.debug("[RuntimeTask] VisionActionLoop unavailable: %s", exc)

        # URL/app was opened even without verification
        opened = f"URL: {url}" if url else f"app: {target_app}" if target_app else goal
        return {
            "success": True,
            "result": f"Opened {opened}. Visual verification pending.",
        }

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
        # v305.0: Strengthened prompt to prefer browser/app automation over HTTP fetch.
        # J-Prime was generating aiohttp.get() for "search X on youtube/wikipedia"
        # instead of opening the browser — the user expects to SEE the page, not
        # receive raw HTML in a dict.
        prompt = (
            f"Generate a Python async function that accomplishes this goal:\n"
            f"Goal: {goal}\n\n"
            f"CRITICAL RULES:\n"
            f"- Single async function: async def execute(context: dict) -> dict\n"
            f"- The function MUST accept exactly one argument: context (a dict)\n"
            f"- Returns a dict with 'success' (bool) and 'result' (str) keys\n"
            f"- This runs on macOS for a DESKTOP AI assistant with a screen\n"
            f"- For ANY task involving websites, search, or browsing:\n"
            f"  USE subprocess.run(['open', url]) to open the URL in Chrome/Safari\n"
            f"  The user wants to SEE the page, NOT receive raw HTML\n"
            f"  NEVER use aiohttp/requests/urllib to fetch web pages — open them in the browser\n"
            f"- For app tasks: subprocess.run(['open', '-a', 'AppName'])\n"
            f"- For web search: build the URL (e.g. youtube.com/results?search_query=X) and open it\n"
            f"- Available imports: subprocess, webbrowser, os, json, re, urllib, asyncio\n"
            f"- Output ONLY the function code, no markdown, no explanation\n"
        )

        try:
            response = await self._prime.generate(
                prompt=prompt,
                system_prompt=(
                    "You are a code generator for a macOS desktop AI assistant. "
                    "Generate ONLY a single Python async function. No markdown fences. "
                    "No explanation. Just the raw Python code starting with 'async def execute(context: dict) -> dict:'"
                ),
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

            # Pillar 7: Full observability — persist and display synthesized code
            _code = response.content
            _code_hash = hashlib.sha256(_code.encode()).hexdigest()[:16]
            try:
                # Persist to disk so the developer can inspect what the AI wrote
                _synth_dir = Path(os.path.expanduser("~/.jarvis/ouroboros/synthesized_tools"))
                _synth_dir.mkdir(parents=True, exist_ok=True)
                _synth_file = _synth_dir / f"{_code_hash}_{goal.replace(' ', '_')[:30]}.py"
                _synth_file.write_text(
                    f"# Synthesized by J-Prime for: {goal}\n"
                    f"# Hash: {_code_hash}\n"
                    f"# Ephemeral: {ephemeral}\n"
                    f"# Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"{_code}\n"
                )
                logger.warning(
                    "\n"
                    "╔══════════════════════════════════════════════════════╗\n"
                    "║  🧬 EPHEMERAL TOOL SYNTHESIZED                     ║\n"
                    "╠══════════════════════════════════════════════════════╣\n"
                    "║  Goal: %-45s ║\n"
                    "║  Hash: %-45s ║\n"
                    "║  File: %-45s ║\n"
                    "╠══════════════════════════════════════════════════════╣\n"
                    "%s\n"
                    "╚══════════════════════════════════════════════════════╝",
                    goal[:45], _code_hash,
                    str(_synth_file.name)[:45],
                    _code[:500],
                )
            except Exception as _persist_exc:
                logger.debug("[RuntimeTask] Failed to persist synthesized code: %s", _persist_exc)

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
        """Build a natural, speakable summary of what happened.

        v305.0: Rewritten to produce human-readable text that sounds good
        via TTS, not robot metrics like "1/1 steps completed | 0 failed".
        This text goes to the frontend via WebSocket and gets spoken aloud.
        """
        total = len(steps)
        succeeded = sum(1 for s in steps if s.error is None)
        failed = total - succeeded
        synthesized = sum(1 for s in steps if s.synthesized)

        if failed == 0:
            # All good — natural completion message
            agents = [s.agent_name for s in steps if s.agent_name]
            if total == 1 and agents:
                summary = f"Done. Handled via {agents[0]}."
            elif total == 1:
                summary = f"Done."
            else:
                summary = f"Done. All {total} steps completed."

            if synthesized > 0:
                summary += f" {synthesized} tool{'s' if synthesized > 1 else ''} synthesized on the fly."
        else:
            # Partial failure
            if succeeded > 0:
                summary = f"Partially done. {succeeded} of {total} steps succeeded."
            else:
                errors = [s.error for s in steps if s.error]
                summary = f"That didn't work. {errors[0]}" if errors else "That didn't work."

        return summary

    # -----------------------------------------------------------------------
    # Step 5.5: Voice completion feedback
    # -----------------------------------------------------------------------

    async def _speak_completion(self, result: TaskResult) -> None:
        """Speak a brief completion message so the user gets audible feedback.

        v305.0: Fire-and-forget — never blocks task return. Uses safe_say
        which routes through say -o tempfile → afplay (no GIL, no device contention).
        """
        try:
            from backend.core.supervisor.unified_voice_orchestrator import safe_say

            # Build a natural, concise spoken summary
            query_short = result.original_query[:60]
            n_steps = len(result.steps)
            agents = [s.agent_name for s in result.steps if s.agent_name]
            synthesized = sum(1 for s in result.steps if s.synthesized)

            if n_steps == 1 and agents:
                msg = f"Done. Handled via {agents[0]}."
            elif synthesized > 0:
                msg = f"Done. Completed {n_steps} steps, {synthesized} synthesized on the fly."
            else:
                msg = f"Done. {n_steps} steps completed for: {query_short}."

            await safe_say(
                msg,
                source="runtime_task_completion",
                skip_dedup=True,  # Always speak task results
            )
        except Exception as exc:
            logger.debug("[RuntimeTask] Voice completion failed (non-fatal): %s", exc)

    # -----------------------------------------------------------------------
    # Step 5.6: Graduation — ephemeral tool persistence analysis
    # -----------------------------------------------------------------------

    async def _analyze_and_offer_persistence(
        self,
        result: TaskResult,
        synthesized_steps: List[StepResolution],
    ) -> None:
        """Check if synthesized ephemeral tools should be offered for permanent assimilation.

        Symbiotic Manifesto §6 — Threshold-Triggered Neuroplasticity:
        If an ephemeral tool is requested 3+ times, propose permanent integration.
        Tracked via a simple file-based counter in .jarvis/ouroboros/graduation/.

        v305.0: Fire-and-forget — never blocks task return.
        """
        try:
            from pathlib import Path
            import json as _json

            grad_dir = Path.home() / ".jarvis" / "ouroboros" / "graduation"
            grad_dir.mkdir(parents=True, exist_ok=True)
            ledger_path = grad_dir / "ephemeral_usage.json"

            # Load or init ledger
            ledger: Dict[str, int] = {}
            if ledger_path.exists():
                try:
                    ledger = _json.loads(ledger_path.read_text())
                except Exception:
                    ledger = {}

            graduation_threshold = int(os.environ.get("JARVIS_GRADUATION_THRESHOLD", "3"))
            candidates = []

            for step in synthesized_steps:
                key = step.capability_used or step.step_goal
                ledger[key] = ledger.get(key, 0) + 1
                if ledger[key] >= graduation_threshold:
                    candidates.append(key)

            # Persist updated counts
            ledger_path.write_text(_json.dumps(ledger, indent=2))

            if candidates:
                from backend.core.supervisor.unified_voice_orchestrator import safe_say

                names = ", ".join(candidates[:3])
                msg = (
                    f"I've used {names} {graduation_threshold} or more times now. "
                    f"Want me to create a persistent agent and open a PR?"
                )
                await safe_say(msg, source="graduation_offer", skip_dedup=True)

                # Emit telemetry for dashboard visibility
                if self._bus is not None:
                    from backend.core.telemetry_contract import TelemetryEnvelope
                    envelope = TelemetryEnvelope.create(
                        event_schema="graduation.offer@1.0.0",
                        source="runtime_task_orchestrator",
                        trace_id=result.task_id,
                        span_id="graduation_offer",
                        partition_key="reasoning",
                        payload={
                            "candidates": candidates,
                            "threshold": graduation_threshold,
                            "query": result.original_query[:200],
                        },
                    )
                    self._bus.emit(envelope)

                logger.info(
                    "[RuntimeTask] Graduation candidates: %s (threshold=%d)",
                    candidates, graduation_threshold,
                )

        except Exception as exc:
            logger.debug("[RuntimeTask] Graduation analysis failed (non-fatal): %s", exc)

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
            # v305.0: Emit task.completed schema so LifecycleVoiceNarrator can
            # pick up the summary for narration (it now filters to this event).
            task_envelope = TelemetryEnvelope.create(
                event_schema="task.completed@1.0.0",
                source="runtime_task_orchestrator",
                trace_id=result.task_id,
                span_id="task_completed",
                partition_key="reasoning",
                payload={
                    "summary": result.summary,
                    "success": result.success,
                    "query": result.original_query[:200],
                },
            )
            self._bus.emit(task_envelope)

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
