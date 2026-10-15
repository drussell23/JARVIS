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
import json
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
    DESKTOP_ACTION = "desktop_action"      # a JARVIS route run by its own handler (close, look)
    EPHEMERAL_TOOL = "ephemeral_tool"      # synthesized on-the-fly, discarded after
    GOVERNANCE_OP = "governance_op"        # routed to Ouroboros for code change
    PERSISTENT_PROPOSAL = "persistent"     # synthesized + proposed for permanent add
    UNRESOLVABLE = "unresolvable"          # no agent, no synthesis possible


class ActionOutcome(str, Enum):
    """Outcome of a single action within the agentic vision loop."""
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class VerifyTier(str, Enum):
    """Which verification tier was used."""
    NONE = "none"
    EXECUTOR = "executor"
    FRAME_DELTA = "frame_delta"
    MODEL_VERIFY = "model_verify"


class StopReason(str, Enum):
    """Why the agentic vision loop terminated."""
    GOAL_SATISFIED = "goal_satisfied"
    USER_CONFIRMATION = "user_visible_confirmation"
    BEST_EFFORT = "best_effort"
    STAGNATION = "stagnation"
    MAX_TURNS = "max_turns"
    MODEL_REFUSAL = "model_refusal"
    ERROR = "error"


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
    # JARVIS's own voice is delivering the summary (set where it is scheduled),
    # so a client showing this result must not speak it a second time.
    spoken: bool = False

    @property
    def acted(self) -> bool:
        """Whether any step already changed the user's desktop (opened something).

        A caller must not re-run such a task through another route: the
        side effect would happen twice (the same browser opened twice).
        """
        return any(isinstance(s.result, dict) and s.result.get("opened") for s in self.steps)

    @property
    def handled(self) -> bool:
        """Whether something serving the request ran and reported back --
        an answer, or why it could not ("Google Workspace email is not
        connected"). Its report is the reply: a caller that routed such a
        task elsewhere had a model invent one ("I don't have access to your
        Gmail"). A task where nothing ran (no handler) is not handled."""
        return any(s.result is not None for s in self.steps)

    @property
    def facts(self) -> List[str]:
        """What each step that worked on the desktop reported doing, in its
        own evidence-worded terms ("Opened Firefox; its window ... is in
        front") or the error it hit -- the record a reply is phrased from."""
        facts = []
        for step in self.steps:
            if step.resolution not in (TaskResolution.VISION_ACTION, TaskResolution.DESKTOP_ACTION):
                continue
            fact = step.error or RuntimeTaskOrchestrator._step_message(step)
            if fact:
                facts.append(fact)
        return facts


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
        if self._worked_on_desktop(executed):
            # What JARVIS did, said the way a person would say it, from the
            # facts of what happened -- not the outcome's log line ("Opened
            # https://www.google.com/search?q=dogs", read out URL and all).
            summary = await self._phrase_for_user(query, summary)
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
            result.spoken = True
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

        IMPORTANT: Vision/UI tasks are NEVER decomposed. The lean vision
        loop handles multi-step goals internally via its see-think-act
        cycle. Decomposition breaks it by splitting the goal into sub-tasks
        that each claim success independently without full context.

        Detection is agentic (Manifesto §5): we ask the same classifier
        the facade uses to determine if this is an executor/vision task.
        No hardcoded app lists.
        """
        # A chain the Mind split into requests of their own ("close notepad and
        # open my downloads folder"): each is classified on its own -- its own
        # capability, app, query -- and runs after the one before it. Before
        # this, a command with one capability ran as one step and every
        # request after the first was dropped.
        chain = [s for s in (ctx.get("steps") or []) if isinstance(s, str) and s.strip()]
        if len(chain) >= 2:
            return await self._chain_steps(chain), f"chain of {len(chain)} requests"

        # Agentic check: ask the facade classifier if this is a vision/UI task.
        # If so, skip decomposition — the lean loop handles multi-step goals.
        if await self._is_vision_goal_agentic(query, ctx):
            logger.info(
                "[RuntimeTask] Vision/UI goal detected (agentic) — single-step: %s",
                query[:80],
            )
            return self._single_step(query, ctx), "single-step (vision goal)"

        # A request the Mind assigned to one agent capability is one goal for
        # that agent; splitting it would also drop the capability.
        if self._named_capability(ctx):
            return self._single_step(query, ctx), f"single-step ({self._named_capability(ctx)})"

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

        return self._single_step(query, ctx), "single-step fallback"

    async def _chain_steps(self, chain: List[str]) -> List[Dict[str, Any]]:
        """One step per request of a chain, each classified by the Mind on its
        own, each depending on the one before (they run in the order said)."""
        from backend.core.intent_classifier import get_intent_classifier

        classifier = get_intent_classifier()
        steps: List[Dict[str, Any]] = []
        for i, request in enumerate(chain):
            decision = await classifier.classify_async(request)
            step = self._single_step(request, decision.to_task_context())[0]
            step["dependencies"] = [str(i - 1)] if i else []
            steps.append(step)
            logger.info("[RuntimeTask] chain step %d/%d %r -> %s", i + 1, len(chain),
                        request[:60], step.get("capability") or "-")
        return steps

    @staticmethod
    def _single_step(query: str, ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Build a single-step plan from the query and context."""
        step: Dict[str, Any] = {
            "goal": query,
            "priority": 1,
            "dependencies": [],
        }
        # requires_vision is the classifier's PRIMARY browser/UI signal
        # (_is_browser_or_ui_task); it was dropped here, so only the
        # structural fallbacks ever saw a step.
        for field_name in ("provider", "url", "search_query", "target_app",
                           "workspace_service", "action_category", "requires_vision",
                           "capability", "classification_source"):
            val = ctx.get(field_name)
            if val:
                step[field_name] = val
        return [step]

    @staticmethod
    async def _is_vision_goal_agentic(query: str, ctx: Dict[str, Any]) -> bool:
        """Agentically determine if a goal is a vision/UI task.

        Uses the same classification pipeline as the Core Context facade
        (Manifesto §5: intelligence-driven routing, no hardcoded lists).
        Falls back to structural signals from the IntentClassifier output.
        """
        # 1. Structural signals from IntentClassifier (already agentic)
        if ctx.get("requires_vision"):
            return True
        if ctx.get("target_app"):
            return True

        # 2. Ask the facade's classifier (uses Claude or keyword fallback)
        try:
            from backend.core_contexts.facade import _classify_to_context
            context_name = await _classify_to_context(query)
            if context_name == "executor":
                return True
        except ImportError:
            pass
        except Exception:
            pass

        return False

    # -----------------------------------------------------------------------
    # Step 2: Capability Resolution
    # -----------------------------------------------------------------------

    async def _resolve_step(
        self, step: Dict[str, Any], ctx: Dict[str, Any],
    ) -> StepResolution:
        """Resolve a single step to an agent, synthesis path, or failure."""
        goal = step["goal"]
        start = time.monotonic()

        # Check -1: the capability the Mind chose (intent_classifier, from the
        # live agent catalogue) outranks every keyword route below.
        resolved = await self._resolve_named_capability(step, goal, start)
        if resolved is not None:
            return resolved
        mind_decided = step.get("classification_source") == "jprime"

        # Browser/UI task: open what it starts with, then the perception-action
        # loop (vision only when opening did not already accomplish the goal).
        if self._is_browser_or_ui_task(goal, step):
            return StepResolution(
                step_goal=goal,
                resolution=TaskResolution.VISION_ACTION,
                agent_name="vision_action_loop",
                capability_used="browser_navigation",
                synthesized=False,
                elapsed_s=time.monotonic() - start,
            )

        # Check 0: Core Context facade — new architecture (per-vertical flags)
        try:
            from backend.core_contexts.facade import dispatch as ctx_dispatch
            ctx_result = await ctx_dispatch(
                goal=goal,
                task_type=step.get("task_type", ""),
                command=goal,
                step=step,
            )
            if ctx_result is not None:
                return StepResolution(
                    step_goal=goal,
                    resolution=TaskResolution.VISION_ACTION,
                    agent_name=f"core_context",
                    capability_used="core_contexts_facade",
                    synthesized=False,
                    result=ctx_result,
                    error=None if ctx_result.get("success") else ctx_result.get("result"),
                    elapsed_s=time.monotonic() - start,
                )
        except ImportError:
            pass  # Facade not available — fall through to legacy
        except Exception as _ctx_exc:
            logger.debug("[RuntimeTask] Core Context facade error: %s", _ctx_exc)

        # Check 1: AgentRegistry by goal keywords (LEGACY) -- only for steps
        # the Mind did not decide (planner sub-steps, degraded mode). For a
        # Mind-decided step, "no capability" IS the decision; re-deciding by
        # keyword is what routed "look up the news" to the wrong place.
        agent_name, capability = (None, None) if mind_decided else await self._find_agent(goal, step)
        if agent_name:
            return StepResolution(
                step_goal=goal,
                resolution=TaskResolution.EXISTING_AGENT,
                agent_name=agent_name,
                capability_used=capability,
                synthesized=False,
                elapsed_s=time.monotonic() - start,
            )

        # (Browser/UI tasks are routed at Check -1, ahead of keyword matching.)

        # Check 2: Is this a code change? Route to Ouroboros governance.
        if not mind_decided and self._is_code_change(goal):
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
    def _named_capability(step: Dict[str, Any]) -> str:
        """The agent capability the Mind assigned to this request, if any."""
        return (step.get("capability") or "").strip().lower()

    async def _resolve_named_capability(
        self, step: Dict[str, Any], goal: str, start: float,
    ) -> Optional[StepResolution]:
        """Route to whatever serves the Mind's capability, or report the gap.

        Bindings first (what this orchestrator can instantiate), then the
        AgentRegistry, where synthesized and graduated agents register. When
        nothing serves it the gap goes to the GapSignalBus, whose consumers
        synthesize an agent for it, and the remaining routes still try.
        """
        capability = self._named_capability(step)
        if not capability:
            return None
        from backend.core.agent_bindings import agent_for_capability, route_for_capability

        route = route_for_capability(capability)
        if route in self._DESKTOP_ROUTES:
            # A route run by its own handler (close something, look and answer).
            return StepResolution(
                step_goal=goal,
                resolution=TaskResolution.DESKTOP_ACTION,
                agent_name=route,
                capability_used=capability,
                synthesized=False,
                elapsed_s=time.monotonic() - start,
            )
        if route:
            # JARVIS's own route (open something / look and act): open what
            # the goal starts with, then the eyes for whatever remains.
            return StepResolution(
                step_goal=goal,
                resolution=TaskResolution.VISION_ACTION,
                agent_name="vision_action_loop",
                capability_used=capability,
                synthesized=False,
                elapsed_s=time.monotonic() - start,
            )
        agent_name = agent_for_capability(capability)
        if agent_name is None and self._registry is not None:
            try:
                agents = await self._registry.find_by_capability(capability)
                if agents:
                    agent_name = min(agents, key=lambda a: getattr(a, "load", 0)).agent_name
            except Exception as exc:  # noqa: BLE001 -- registry absence is a gap, not a crash
                logger.debug("[RuntimeTask] registry lookup for %s failed: %s", capability, exc)
        if agent_name:
            return StepResolution(
                step_goal=goal,
                resolution=TaskResolution.EXISTING_AGENT,
                agent_name=agent_name,
                capability_used=capability,
                synthesized=False,
                elapsed_s=time.monotonic() - start,
            )
        try:
            from backend.neural_mesh.synthesis.gap_signal_bus import (
                CapabilityGapEvent, get_gap_signal_bus,
            )
            get_gap_signal_bus().emit(CapabilityGapEvent(
                goal=goal,
                task_type=capability,
                target_app=step.get("target_app") or "",
                source="runtime_task_orchestrator",
            ))
            logger.info("[RuntimeTask] capability gap reported for synthesis: %s", capability)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RuntimeTask] gap report failed for %s: %s", capability, exc)
        return None

    @staticmethod
    def _is_browser_or_ui_task(goal: str, step: Dict[str, Any]) -> bool:
        """Detect if this step needs browser/UI interaction (route to VisionActionLoop).

        v306.0: Primary signal is `requires_vision` from the IntentClassifier's
        agentic output (J-Prime decides, not keywords). Structural fallback
        signals apply only when `requires_vision` was not set (sync heuristic
        degraded mode — Progressive Awakening §2).

        Detection is STRUCTURAL, not string-matching. We rely on the
        IntentClassifier's semantic output which are resolved agentically
        by J-Prime — no hardcoded provider lists.
        """
        # 0. PRIMARY: J-Prime explicitly said this needs vision (§5-aligned)
        if step.get("requires_vision"):
            return True

        # --- Below: structural fallback for degraded/sync classification ---
        target_app = (step.get("target_app") or "").lower()
        category = (step.get("category") or "").lower()
        action_category = (step.get("action_category") or "").lower()
        provider = (step.get("provider") or "").lower()
        search_query = step.get("search_query") or ""

        # 1. IntentClassifier tagged a specific app target (not terminal)
        if target_app and target_app != "terminal":
            return True

        # 2. IntentClassifier categorized it as web/search/navigation
        if category in ("web_search", "browser", "navigation", "website"):
            return True
        if action_category in ("web_search", "browser", "navigation"):
            return True

        # 3. IntentClassifier identified a web provider + search query
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
                    # Check if the result dict signals a logical failure
                    # (e.g. vision loop returned success=False without raising)
                    _node_error = self._result_failure(node.result)
                    if _node_error:
                        logger.warning(
                            "[RuntimeTask] DAG node %d completed but result.success=False: %s",
                            i, _node_error[:120] if _node_error else "unknown",
                        )
                    executed.append(StepResolution(
                        step_goal=step["goal"],
                        resolution=resolution.resolution,
                        agent_name=resolution.agent_name,
                        capability_used=resolution.capability_used,
                        synthesized=resolution.synthesized,
                        result=node.result,
                        error=_node_error,
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
            elif resolution.resolution == TaskResolution.DESKTOP_ACTION:
                return await orchestrator._dispatch_desktop_route(
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
                _task_error = self._result_failure(task.result)
                executed.append(StepResolution(
                    step_goal=step["goal"],
                    resolution=resolution.resolution,
                    agent_name=task.assigned_agent or resolution.agent_name,
                    capability_used=resolution.capability_used,
                    synthesized=resolution.synthesized,
                    result=task.result,
                    error=_task_error,
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
                    error=self._result_failure(result),
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

            elif resolution.resolution == TaskResolution.DESKTOP_ACTION:
                result = await self._dispatch_desktop_route(resolution.agent_name or "", goal, step)
                return StepResolution(
                    step_goal=goal,
                    resolution=resolution.resolution,
                    agent_name=resolution.agent_name,
                    capability_used=resolution.capability_used,
                    synthesized=False,
                    result=result,
                    error=None if result.get("success") else result.get("result"),
                    elapsed_s=time.monotonic() - start,
                )

            elif resolution.resolution == TaskResolution.VISION_ACTION:
                # If the facade already executed (result is set), skip re-execution
                if resolution.result is not None:
                    return resolution

                result = await self._dispatch_to_vision(goal, step)
                # Check the success field — vision returns {success: bool, ...}
                vision_error = self._result_failure(result)
                return StepResolution(
                    step_goal=goal,
                    resolution=resolution.resolution,
                    agent_name="vision_action_loop",
                    capability_used="browser_navigation",
                    synthesized=False,
                    result=result,
                    error=vision_error,
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
        """The agent, ready to work: the Neural Mesh's running instance when
        it has one, else one created from its binding AND initialised.

        An instance used to be created and dispatched to uninitialised: an
        agent builds what it works with in initialize() (the workspace agent
        its Google client and executor), so every request through here got
        "No execution method available".
        """
        if agent_name in self._live_agents:
            return self._live_agents[agent_name]

        try:
            from backend.neural_mesh.integration import get_neural_mesh_coordinator
            coordinator = get_neural_mesh_coordinator()
            running = coordinator.get_agent(agent_name) if coordinator is not None else None
        except Exception:  # noqa: BLE001 -- no mesh: the binding serves
            running = None
        if running is not None and getattr(running, "_initialized", True):
            self._live_agents[agent_name] = running
            return running

        from backend.core.agent_bindings import get_agent_bindings
        bindings = get_agent_bindings()
        binding = bindings.get(agent_name)
        if binding is None:
            return None

        try:
            instance = binding.instantiate()
            if callable(getattr(instance, "initialize", None)) and not getattr(instance, "_initialized", True):
                await instance.initialize()
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
        from backend.core.agent_bindings import get_agent_bindings
        from backend.core.task_envelope import TaskEnvelope

        binding = get_agent_bindings().get(agent_name)
        if binding is not None:
            missing = binding.missing_requirements()
            if missing:
                # Not set up here: say what it needs (and list it among the
                # boot's missing extras) -- never a guess from elsewhere.
                from backend.core.optional_dependencies import note_missing
                for requirement in missing:
                    note_missing(requirement.split(".")[0], agent_name.replace("_", " "))
                return {"success": False,
                        "result": f"{agent_name.replace('_', ' ')} is not set up on this machine: "
                                  f"it needs {', '.join(missing)} installed"}

        envelope = TaskEnvelope.from_step(step)
        payload = envelope.to_dict()
        # What the agent is asked to do. Agents switch on payload["action"],
        # which the step never carried -- the Mind names a capability -- so a
        # dispatched agent got "" ("Unknown context action: ", 2026-10-14).
        # An agent with an entry takes the request in the user's words and
        # works it out; otherwise the capability the Mind chose is the action.
        if binding is not None and not payload.get("action"):
            capability = self._named_capability(step)
            if binding.entry:
                payload["action"] = binding.entry
                payload.setdefault("query", goal)
            elif capability in {c.lower() for c in binding.capabilities}:
                payload["action"] = capability

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

    async def _resolve_open_target(self, goal: str, step: Dict[str, Any]) -> Dict[str, Any]:
        """Ask J-Prime how this goal starts, and whether starting it finishes it.

        One agentic decision, no URL templates or app tables: what to open
        (a URL, an application, or a file/folder -- decided against the
        user's real folders, from the OS -- or nothing) and ``completes_goal`` -- e.g. a
        search is finished by opening its results page, while "play the first
        video" still needs the screen afterwards. Returns {} when the Mind is
        unavailable or answers unusably; the caller then opens nothing itself.
        """
        if self._prime is None:
            return {}
        from backend.core.desktop_open import (
            installed_browsers, looks_like_url, normalize_url, resolve_path,
        )
        from backend.core.host_platform import os_display_name, user_folders
        from backend.core.llm_json import parse_json_object

        fields = {k: step.get(k) for k in ("provider", "search_query", "target_app") if step.get(k)}
        folders = "\n".join(f"- {f}" for f in await asyncio.to_thread(user_folders))
        # Facts of this machine, so a request that names "a browser" or "my
        # documents" is decided against what really exists here.
        browsers = "\n".join(f"- {name}" + (" (the default)" if default else "")
                             for name, default in await installed_browsers())
        prompt = (
            f"Goal of a user at a {os_display_name()} computer: {goal}\n"
            f"Fields extracted from the request: {json.dumps(fields)}\n"
            f"The user's folders on this computer:\n{folders or '- (unknown)'}\n"
            f"The web browsers on this computer:\n{browsers or '- (unknown)'}\n\n"
            "The assistant opens URLs directly in the user's browser, opens files and "
            "folders, and launches installed applications. Decide what to open so the user "
            "LANDS ON THE RESULT, and whether opening it IS the whole goal. Respond ONLY with JSON:\n"
            '{"open": "url" | "path" | "app" | "none", "url": "https://...", '
            '"path": "absolute path of the file or folder", "app": "application name", '
            '"completes_goal": true | false, "remaining": "what the assistant itself must still do '
            'on screen after opening, as an instruction to itself; \\"\\" when nothing"}\n'
            "Decision rules:\n"
            "- A folder or a file of the user's (\"my documents\", \"the report on my desktop\"): "
            "open=path, built from the folders listed above -- never open a file manager app "
            "and navigate.\n"
            "- Web content (a search, a site, a video, a page): open=url with the most direct "
            "URL, e.g. the search-results page for the query. Never open a browser app and "
            "then type into it: the URL already does that.\n"
            "- open=app only when the goal is about an installed application itself "
            "(\"open Spotify\", \"launch Notepad\"), or names an app to use. When the "
            "application named is a web browser and the goal is web content, the content is "
            "still open=url: the URL opens in the browser named.\n"
            "- completes_goal=true when landing there is what the user asked for (a search "
            "shows its results). completes_goal=false only when something must still be "
            "clicked, typed or read on screen afterwards (\"play the first video\").\n"
            "- open=none when nothing needs to be opened.\n"
            "- remaining is work for the assistant, never a remark about what the user will do: "
            "it becomes the assistant's next goal on screen."
        )
        try:
            response = await asyncio.wait_for(
                self._prime.generate(
                    prompt=prompt,
                    system_prompt="You decide how a desktop assistant starts a task. Return only JSON.",
                    max_tokens=256,
                    temperature=0.0,
                ),
                timeout=float(os.getenv("JARVIS_OPEN_RESOLVE_TIMEOUT_S", "15")),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[RuntimeTask] open-target resolution failed: %s", exc)
            return {}

        decision = parse_json_object(getattr(response, "content", None)) or {}
        kind = str(decision.get("open") or "").lower()
        url = str(decision.get("url") or "").strip()
        app = str(decision.get("app") or "").strip()
        path = resolve_path(str(decision.get("path") or "")) if kind == "path" else None
        if kind == "url" and not looks_like_url(url):
            kind = "app" if app else "none"
        if kind == "path" and path is None:
            # Named a file or folder that is not there (nor anything like it).
            kind = "none"
        if kind == "app" and not app:
            kind = "none"
        resolved = {
            "open": kind if kind in ("url", "path", "app") else "none",
            "url": normalize_url(url) if kind == "url" else "",
            "path": str(path) if kind == "path" else "",
            "app": app if kind == "app" else "",
            "completes_goal": bool(decision.get("completes_goal")) and kind in ("url", "path", "app"),
            "remaining": str(decision.get("remaining") or "").strip(),
        }
        logger.info("[RuntimeTask] open decision for %r: %s", goal[:60], resolved)
        return resolved

    @staticmethod
    def _report_open(result: Any, opened: Any) -> Any:
        """Say what WAS done when the screen work after opening could not finish."""
        if opened is None or not getattr(opened, "ok", False) or not isinstance(result, dict):
            return result
        if result.get("success"):
            return result
        why = str(result.get("result") or result.get("error") or "").strip()
        result = dict(result)
        result["result"] = (
            f"{opened.describe()}, but I couldn't finish the rest on screen"
            + (f": {why}" if why else ".")
        )
        result["opened"] = opened.resolved
        return result

    # JARVIS's routes run by a handler of their own, by route name
    # (agent_bindings). "look" is here because a screen question can now be a
    # step of a chain; alone it is answered before the orchestrator.
    _DESKTOP_ROUTES = {"close": "_dispatch_to_close", "look": "_dispatch_to_look"}

    async def _dispatch_desktop_route(self, route: str, goal: str, step: Dict[str, Any]) -> Dict[str, Any]:
        handler = getattr(self, self._DESKTOP_ROUTES.get(route, ""), None)
        if handler is None:
            return {"success": False, "result": f"no desktop route {route!r}"}
        return await handler(goal, step)

    async def _resolve_close_target(
        self, goal: str, step: Dict[str, Any], windows: List[Tuple[str, str]],
    ) -> str:
        """Ask J-Prime which open program or window the goal means to close.

        Decided against what is actually open (program and window title, from
        the desktop), so "close the dogs search", "close that file" and
        "close firefox" each resolve to something real. "" when nothing open
        matches or the Mind is unavailable.
        """
        if self._prime is None or not windows:
            return ""
        from backend.core.llm_json import parse_json_object

        listing = "\n".join(f"- program: {program} | window: {title}" for program, title in windows)
        fields = {k: step.get(k) for k in ("target_app", "search_query") if step.get(k)}
        prompt = (
            f"Request: {goal}\nFields extracted from it: {json.dumps(fields)}\n\n"
            f"Open on the user's desktop right now, front-most first:\n{listing}\n\n"
            "What does the request ask to close? Respond ONLY with JSON:\n"
            '{"close": "a program name or a window title copied exactly from the list, or \"\""}\n'
            "- A program name closes all of that program's windows: use it when the request "
            "names an application.\n"
            "- A window title closes only that window: use it when the request names a "
            "document, file, folder, page or one particular window.\n"
            '- "" when nothing in the list is what the request names.'
        )
        try:
            response = await asyncio.wait_for(
                self._prime.generate(
                    prompt=prompt,
                    system_prompt="You decide what a desktop assistant closes. Return only JSON.",
                    max_tokens=128,
                    temperature=0.0,
                ),
                timeout=float(os.getenv("JARVIS_OPEN_RESOLVE_TIMEOUT_S", "15")),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[RuntimeTask] close-target resolution failed: %s", exc)
            return ""
        decision = parse_json_object(getattr(response, "content", None)) or {}
        target = str(decision.get("close") or "").strip()
        logger.info("[RuntimeTask] close decision for %r: %r", goal[:60], target)
        return target

    async def _dispatch_to_look(self, goal: str, step: Dict[str, Any]) -> Dict[str, Any]:
        """Answer a question about the screen by looking at it."""
        from backend.vision.screen_question import answer_about_screen

        reply = await answer_about_screen(goal)
        return {"success": bool(reply.get("success")), "result": reply.get("response", "")}

    async def _dispatch_to_close(self, goal: str, step: Dict[str, Any]) -> Dict[str, Any]:
        """Close what the goal names, gracefully (backend.core.desktop_close)."""
        from backend.core.desktop_close import close_target, open_windows

        windows = await open_windows()
        target = await self._resolve_close_target(goal, step, windows)
        if not target and not windows:
            # Platforms without a window listing: the request's own words.
            target = step.get("target_app") or ""
        if not target:
            return {"success": False, "result": "nothing open matches what you asked me to close"}
        outcome = await close_target(target)
        logger.info("[RuntimeTask] %s (method=%s)", outcome.describe(), outcome.method)
        return {
            "success": outcome.ok,
            "result": outcome.describe(),
            "closed": list(outcome.closed),
            "still_open": list(outcome.still_open),
        }

    async def _dispatch_to_vision(self, goal: str, step: Dict[str, Any]) -> Any:
        """Route a browser/UI task: open what it starts with, then the vision loop.

        Phase 0 opens through backend.core.desktop_open (every OS) whatever
        J-Prime decided the goal starts with. When opening IS the goal (a
        search answered by its results page) the task ends there, with no
        screen work. Otherwise the vision loop continues from the opened
        state; if it cannot, the reply still says what was opened.
        """
        from backend.core.desktop_open import open_app, open_path, open_url, preferred_browser

        if step.get("url"):
            decision: Dict[str, Any] = {"open": "url", "url": step["url"], "completes_goal": False}
        else:
            decision = await self._resolve_open_target(goal, step)
        if not decision and step.get("target_app"):
            # The Mind is unreachable: the classifier's own extraction still names the app.
            decision = {"open": "app", "app": step["target_app"], "completes_goal": False}

        opened = None
        if decision.get("open") == "url":
            # In the browser the user named ("... on firefox"), else the
            # preferred one. open_url resolves the name against the browsers
            # the OS has registered and falls back to the default when the
            # named app is not a browser. The named app used to be dropped:
            # every URL opened in the default browser.
            opened = await open_url(
                decision["url"], browser=step.get("target_app") or preferred_browser(),
            )
        elif decision.get("open") == "path":
            opened = await open_path(decision["path"])
        elif decision.get("open") == "app":
            opened = await open_app(decision["app"])
        if opened is not None:
            logger.info("[RuntimeTask] %s (method=%s)", opened.describe(), opened.method)

        if opened is not None and opened.ok and decision.get("completes_goal"):
            # Satisfied only on evidence: a launch that returned 0 with no
            # window to show for it was reported "Firefox is now open and
            # ready" (2026-10-14 12:24, the window never came).
            return {
                "success": opened.evidenced,
                "result": opened.describe(),
                "stop_reason": (StopReason.GOAL_SATISFIED if opened.evidenced
                                else StopReason.ERROR).value,
                "opened": opened.resolved,
            }
        if opened is not None and opened.ok:
            if opened.in_front is False:
                # Everything below clicks and types into the foreground window;
                # with the opened one not in front that is whatever the user
                # had there (once, the terminal received a typed search).
                return self._report_open({
                    "success": False,
                    "result": "it did not come to the front, so I did not act on the screen",
                    "stop_reason": StopReason.ERROR.value,
                }, opened)
            if opened.in_front is None:
                # Not observable on this platform: let the window paint.
                await asyncio.sleep(float(os.getenv("JARVIS_OPEN_SETTLE_S", "2.0")))
            # The eyes continue from where opening left off, told what is in front.
            remaining = str(decision.get("remaining") or "").strip()
            goal = remaining or goal
            if opened.window:
                goal = f'{goal} (the window in front is "{opened.window}", opened for this)'

        # Phase 0.5: JARVIS-CU (local Computer Use — 3-layer cascade)
        # Accessibility API (<5ms) → Doubleword 235B (~2s) → Claude Vision (fallback)
        # Powered by 60fps SHM capture. Auto-activates vision on first call.
        jarvis_cu_enabled = os.getenv("JARVIS_CU_ENABLED", "true").lower() in ("true", "1", "yes")
        if jarvis_cu_enabled:
            try:
                from backend.vision.vision_activator import VisionActivator
                activator = VisionActivator.get_instance()
                logger.info("[RuntimeTask] === JARVIS-CU START === goal: %s", goal[:80])
                cu_result = await activator.run_goal(goal)
                logger.info(
                    "[RuntimeTask] === JARVIS-CU END === success=%s steps=%s/%s layers=%s",
                    cu_result.get("success"),
                    cu_result.get("steps_completed"),
                    cu_result.get("steps_total"),
                    cu_result.get("layers_used"),
                )
                if cu_result.get("success"):
                    return cu_result
                # If JARVIS-CU failed, fall through to Lean Vision Loop
                logger.warning(
                    "[RuntimeTask] JARVIS-CU partial failure (%s/%s steps), "
                    "falling back to Lean Vision Loop",
                    cu_result.get("steps_completed"), cu_result.get("steps_total"),
                )
            except Exception as cu_exc:
                logger.warning("[RuntimeTask] JARVIS-CU error: %s, falling back", cu_exc)

        # Phase 1: Lean Vision Loop (Path A) — fallback when JARVIS-CU fails
        # Try BOTH import paths — unified_supervisor may use either root
        lean_enabled = os.getenv("VISION_LEAN_ENABLED", "true").lower() in ("true", "1", "yes")
        if lean_enabled:
            _LeanVisionLoop = None
            for _import_path in ("backend.vision.lean_loop", "vision.lean_loop"):
                try:
                    import importlib
                    _mod = importlib.import_module(_import_path)
                    _LeanVisionLoop = _mod.LeanVisionLoop
                    logger.info("[RuntimeTask] Lean loop imported from: %s", _import_path)
                    break
                except ImportError:
                    continue

            if _LeanVisionLoop is not None:
                try:
                    lean = _LeanVisionLoop.get_instance()
                    logger.info("[RuntimeTask] === LEAN VISION LOOP START === goal: %s", goal[:80])
                    result = await lean.run(goal)
                    logger.info("[RuntimeTask] === LEAN VISION LOOP END === success=%s turns=%s",
                               result.get("success"), result.get("turns"))
                    return self._report_open(result, opened)
                except Exception as lean_exc:
                    logger.error("[RuntimeTask] LEAN VISION LOOP CRASHED: %s", lean_exc, exc_info=True)
            else:
                logger.error(
                    "[RuntimeTask] LEAN VISION LOOP IMPORT FAILED — tried both "
                    "'backend.vision.lean_loop' and 'vision.lean_loop'. "
                    "File missing or dependency error."
                )

        # Legacy fallback — but NO false success.
        # If we get here, vision is broken. Say so.
        import hashlib as _hashlib
        _val = await self._get_vision_action_loop()
        if _val is None:
            logger.error(
                "[RuntimeTask] VISION UNAVAILABLE — %s, but cannot perform screen "
                "actions. Both lean loop and legacy pipeline failed.",
                opened.describe() if opened is not None else "nothing was opened",
            )
            return self._report_open({
                "success": False,
                "result": "my vision system isn't working, so I can't see or interact with the screen right now",
                "stop_reason": StopReason.ERROR.value,
            }, opened)

        pipeline = _val.frame_pipeline
        mind = self._get_mind_client()
        if mind is None:
            return {
                "success": True,
                "result": "Opened but no reasoning backend available.",
                "stop_reason": StopReason.ERROR.value,
            }

        max_turns = int(os.getenv("VISION_LOOP_MAX_TURNS", "10"))
        stagnation_window = int(os.getenv("VISION_LOOP_STAGNATION_WINDOW", "3"))
        frame_stagnation = int(os.getenv("VISION_LOOP_FRAME_STAGNATION", "3"))
        default_settle_ms = int(os.getenv("VISION_LOOP_DEFAULT_SETTLE_MS", "200"))
        max_settle_ms = int(os.getenv("VISION_LOOP_MAX_SETTLE_MS", "2000"))
        action_log: list = []
        request_id = str(uuid.uuid4())[:12]
        session_id = getattr(self, "_session_id", str(uuid.uuid4())[:12])

        try:
            for turn in range(max_turns):
                # SEE
                frame = pipeline.latest_frame
                if frame is None:
                    await asyncio.sleep(0.5)
                    frame = pipeline.latest_frame
                if frame is None:
                    continue

                # Compress frame
                from PIL import Image as _Image
                pil_image = _Image.fromarray(frame.data)
                compressed = mind._compress_frame_jpeg(pil_image)
                frame_b64 = compressed["data"]
                frame_dims = {
                    "width": compressed["width"],
                    "height": compressed["height"],
                    "scale": getattr(frame, "scale_factor", 1.0),
                }
                frame_hash = _hashlib.sha256(frame.data.tobytes()[:4096]).hexdigest()[:12]

                # THINK
                response = await mind.reason_vision_turn(
                    request_id=request_id,
                    session_id=session_id,
                    goal=goal,
                    action_log=action_log,
                    frame_jpeg_b64=frame_b64,
                    frame_dims=frame_dims,
                    allowed_action_types=["click", "type", "scroll"],
                )

                # GATE: goal achieved?
                if response.get("goal_achieved"):
                    return {
                        "success": True,
                        "result": f"Goal achieved: {goal}",
                        "stop_reason": response.get("stop_reason", StopReason.GOAL_SATISFIED.value),
                        "turns": turn + 1,
                        "action_log": action_log,
                    }

                # Model refusal or error
                if response.get("stop_reason") in (StopReason.MODEL_REFUSAL.value, StopReason.ERROR.value):
                    return {
                        "success": False,
                        "result": response.get("reasoning", "Model could not proceed"),
                        "stop_reason": response.get("stop_reason"),
                        "turns": turn + 1,
                        "action_log": action_log,
                    }

                next_action = response.get("next_action")
                if not next_action:
                    return {
                        "success": False,
                        "result": "No action proposed and goal not achieved",
                        "stop_reason": StopReason.ERROR.value,
                        "turns": turn + 1,
                        "action_log": action_log,
                    }

                # STAGNATION CHECK
                proposed = type("P", (), {
                    "action_type": next_action.get("action_type"),
                    "target": next_action.get("target"),
                    "text": next_action.get("text"),
                })()
                if self._is_stagnant_static(action_log, proposed, stagnation_window, frame_stagnation):
                    return {
                        "success": False,
                        "result": f"Stagnation detected after {turn + 1} turns",
                        "stop_reason": StopReason.STAGNATION.value,
                        "turns": turn + 1,
                        "action_log": action_log,
                    }

                # ACT
                action_type = next_action.get("action_type", "click")
                coords_hint = None
                if next_action.get("coords"):
                    c = next_action["coords"]
                    if isinstance(c, (list, tuple)) and len(c) >= 2:
                        coords_hint = (int(c[0]), int(c[1]))

                try:
                    act_result = await asyncio.wait_for(
                        _val.execute_action(
                            target_description=next_action.get("target", goal),
                            action_type=action_type,
                            target_text=next_action.get("text", ""),
                            coords_hint=coords_hint,
                        ),
                        timeout=20.0,
                    )
                    act_success = act_result.success
                    verify_status = getattr(act_result, "verification_status", "unknown")
                except asyncio.TimeoutError:
                    act_success = False
                    verify_status = "timeout"
                except Exception as act_exc:
                    act_success = False
                    verify_status = f"error: {act_exc}"

                # SETTLE
                settle = next_action.get("settle_ms") or default_settle_ms
                settle = min(settle, max_settle_ms)
                await asyncio.sleep(settle / 1000.0)

                # RECORD
                entry = {
                    "turn": turn + 1,
                    "action_type": action_type,
                    "target": next_action.get("target", ""),
                    "text": next_action.get("text"),
                    "coords": next_action.get("coords"),
                    "result": ActionOutcome.SUCCESS.value if act_success else ActionOutcome.FAILURE.value,
                    "verify_tier": VerifyTier.EXECUTOR.value,
                    "observation": verify_status,
                    "confidence": response.get("confidence"),
                    "frame_hash": frame_hash,
                }
                action_log.append(entry)

                logger.info(
                    "[RuntimeTask] Vision turn %d: %s '%s' -> %s",
                    turn + 1, action_type,
                    next_action.get("target", "")[:30],
                    "OK" if act_success else "FAIL",
                )

        except asyncio.CancelledError:
            raise  # Must propagate -- CancelledError is BaseException
        except Exception as exc:
            logger.warning("[RuntimeTask] Agentic vision loop error: %s", exc)
            return {
                "success": False,
                "result": f"Vision loop error: {exc}",
                "stop_reason": StopReason.ERROR.value,
                "turns": len(action_log),
                "action_log": action_log,
            }

        # Max turns exhausted
        return {
            "success": len(action_log) > 0,
            "result": f"Completed {len(action_log)} actions, max turns reached",
            "stop_reason": StopReason.MAX_TURNS.value,
            "turns": max_turns,
            "action_log": action_log,
        }

    async def _get_vision_action_loop(self) -> Any:
        """Get the kernel's VisionActionLoop instance if running."""
        try:
            from backend.vision.realtime.vision_action_loop import VisionActionLoop
            _cls = VisionActionLoop
            if hasattr(_cls, "get_instance"):
                return _cls.get_instance()
            # Fallback: check if supervisor stored it
            return None
        except ImportError:
            return None

    def _get_mind_client(self):
        """Get MindClient singleton if available."""
        try:
            from backend.core.mind_client import get_mind_client
            return get_mind_client()
        except ImportError:
            return None

    async def _find_strategy(self, goal: str, app_context: str):
        """Check for graduated navigation strategy. Returns None today.
        Future: Ouroboros fills this via AgentRegistry semantic lookup (not regex).
        """
        return None

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
        from backend.core.host_platform import os_display_name

        prompt = (
            f"Generate a Python async function that accomplishes this goal:\n"
            f"Goal: {goal}\n\n"
            f"CRITICAL RULES:\n"
            f"- Single async function: async def execute(context: dict) -> dict\n"
            f"- The function MUST accept exactly one argument: context (a dict)\n"
            f"- Returns a dict with 'success' (bool) and 'result' (str) keys\n"
            f"- This runs on {os_display_name()} for a DESKTOP AI assistant with a screen\n"
            f"- For ANY task involving websites, search, or browsing:\n"
            f"  USE webbrowser.open(url) to open the URL in the user's browser\n"
            f"  The user wants to SEE the page, NOT receive raw HTML\n"
            f"  NEVER use aiohttp/requests/urllib to fetch web pages — open them in the browser\n"
            f"- For app tasks: launch the application the way {os_display_name()} does\n"
            f"- For web search: build the site's search-results URL and open it\n"
            f"- Available imports: subprocess, webbrowser, os, json, re, urllib, asyncio\n"
            f"- Output ONLY the function code, no markdown, no explanation\n"
        )

        try:
            response = await self._prime.generate(
                prompt=prompt,
                system_prompt=(
                    f"You are a code generator for a {os_display_name()} desktop AI assistant. "
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
    def _result_failure(result: Any) -> Optional[str]:
        """What went wrong, when a step's result says it did not work; None
        when it worked.

        A result dict fails by ``success: False`` or -- the agents' own
        convention -- by carrying an ``error`` without a ``success: True``.
        The latter read as success: the workspace agent's "Google Workspace
        email is not connected" became "Done." The message keeps what the
        agent says the user can do about it (``action_required``).
        """
        if not isinstance(result, dict):
            return None
        failed = result.get("success") is False or (
            bool(result.get("error")) and result.get("success") is not True)
        if not failed:
            return None
        message = str(result.get("error") or result.get("result") or "").strip() or "Action failed"
        action = str(result.get("action_required") or "").strip()
        return f"{message} ({action})" if action else message

    @staticmethod
    def _step_message(step: StepResolution) -> str:
        """The human-readable outcome a step's own result reported, if any."""
        result = step.result
        if isinstance(result, dict):
            for key in ("result", "response", "answer", "message", "final_message"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

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
            # All good — say what each step reported doing (what it opened,
            # the answer it found); internal agent names are not news to a user.
            reported = [m for m in (RuntimeTaskOrchestrator._step_message(s) for s in steps) if m]
            if reported:
                summary = "\n\n".join(reported)
            elif total == 1:
                summary = "Done."
            else:
                summary = f"Done. All {total} steps completed."

            if synthesized > 0:
                summary += f" {synthesized} tool{'s' if synthesized > 1 else ''} synthesized on the fly."
        else:
            # Partial or complete failure — surface the actual error
            errors = [s.error for s in steps if s.error]
            if succeeded > 0:
                # What the steps that worked found or did, then what did not.
                # The counts alone ("Partially done. 1 of 2 steps succeeded.")
                # threw away an answer the screen step had already given.
                reported = [m for m in (RuntimeTaskOrchestrator._step_message(s)
                                        for s in steps if s.error is None) if m]
                summary = "\n\n".join(reported) if reported else \
                    f"Partially done. {succeeded} of {total} steps succeeded."
                if errors:
                    summary += f"\n\nI couldn't do all of it: {errors[0]}"
            else:
                if errors:
                    # Make it conversational — trim long error messages
                    err_msg = errors[0][:150] if errors[0] else "Unknown error"
                    summary = f"That didn't work. {err_msg}"
                else:
                    summary = "That didn't work."

        return summary

    # -----------------------------------------------------------------------
    # Step 5.5: Voice completion feedback
    # -----------------------------------------------------------------------

    @staticmethod
    def _worked_on_desktop(executed: List[StepResolution]) -> bool:
        """Whether the task acted on the desktop (opened, closed, looked) rather
        than answering -- an answer is already words for the user."""
        return any(s.resolution in (TaskResolution.VISION_ACTION, TaskResolution.DESKTOP_ACTION)
                   for s in executed)

    async def _phrase_for_user(self, query: str, outcome: str) -> str:
        """The outcome of a desktop task as JARVIS would say it to the user.

        One short sentence from the Mind, told only the request and the facts
        of what happened (successes and failures alike), bounded by
        JARVIS_REPLY_PHRASE_TIMEOUT_S. The factual outcome stands when the
        Mind is unavailable or slow: a plain true sentence beats a late one.
        """
        if self._prime is None or not outcome.strip():
            return outcome
        try:
            from backend.core.owner import owner_name
            name = owner_name()
        except Exception:  # noqa: BLE001 -- unnamed: speak without a name
            name = ""
        prompt = (
            f"The user{f' ({name})' if name else ''} asked: {query}\n"
            f"What you did, as it happened:\n{outcome}\n\n"
            "Tell them what you did in one short spoken sentence, the way a capable assistant "
            "talks to the person it works for. Say plainly if something did not work or is not "
            "confirmed. State only what the lines above say happened: do not predict or describe "
            "what is on their screen, and add no detail the lines do not contain -- what is "
            "\"still open\" was not closed. No URLs, file paths or technical detail. Reply with "
            "the sentence only."
        )
        try:
            response = await asyncio.wait_for(
                self._prime.generate(
                    prompt=prompt,
                    system_prompt="You are JARVIS, a personal AI assistant, speaking to its user.",
                    max_tokens=80,
                    temperature=0.3,
                ),
                timeout=float(os.getenv("JARVIS_REPLY_PHRASE_TIMEOUT_S", "4")),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the factual outcome stands
            logger.debug("[RuntimeTask] reply phrasing unavailable: %s", exc)
            return outcome
        from backend.core.prime_router import generation_ok
        if not generation_ok(response):
            return outcome
        said = str(getattr(response, "content", "") or "").strip().strip('"').strip()
        return said or outcome

    async def _speak_completion(self, result: TaskResult) -> None:
        """Speak a brief completion message so the user gets audible feedback.

        v305.0: Fire-and-forget — never blocks task return. Uses safe_say
        which routes through say -o tempfile → afplay (no GIL, no device contention).
        """
        try:
            from backend.core.supervisor.unified_voice_orchestrator import safe_say, spoken_form

            await safe_say(
                spoken_form(result.summary or "Done."),
                source="runtime_task_completion",
                skip_dedup=False,  # v307: dedup enabled — prevents "Done" repeating 3-4x
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

    @staticmethod
    def _is_stagnant_static(
        action_log: list,
        proposed_action,
        stagnation_window: int = 3,
        frame_stagnation: int = 3,
    ) -> bool:
        """Detect stuck vision loops: same successful action repeated, or frozen frames."""
        # Check 1: identical SUCCESSFUL action proposed N times
        recent = action_log[-stagnation_window:] if len(action_log) >= stagnation_window else action_log
        matches = sum(
            1 for e in recent
            if e.get("action_type") == proposed_action.action_type
            and e.get("target") == proposed_action.target
            and e.get("text") == getattr(proposed_action, "text", None)
            and e.get("result") == "success"
        )
        if matches >= stagnation_window:
            return True

        # Check 2: frame unchanged for N turns
        if len(action_log) >= frame_stagnation:
            recent_hashes = [e.get("frame_hash") for e in action_log[-frame_stagnation:]]
            if len(set(h for h in recent_hashes if h)) == 1:
                return True

        return False

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
