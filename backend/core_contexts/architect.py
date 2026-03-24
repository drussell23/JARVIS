"""
Architect Context -- plans, decomposes goals, selects contexts and tools.

The Architect is the brain of the Core Context system.  It receives a
user goal, classifies the intent, decomposes it into a DAG of steps,
selects which Core Context handles each step, and orchestrates execution.

The Architect uses the 397B model (via J-Prime) for reasoning.  It does
NOT use hardcoded if/elif routing -- it reads tool manifests and reasons
about which tools to compose for each goal.

Tool access:
    intelligence.*   -- classify intent, detect patterns, environment context
    memory.*         -- recall past knowledge for informed planning
    All other contexts' tool_manifest() for DAG construction
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core_contexts.tools import intelligence, memory

logger = logging.getLogger(__name__)


@dataclass
class DAGStep:
    """A single step in a task execution plan.

    Attributes:
        step_id: Unique step identifier.
        context: Which Core Context handles this step
            ("executor", "developer", "communicator", "observer").
        tool: Tool name to call (e.g., "input.click", "workspace.send_email").
        args: Arguments to pass to the tool.
        depends_on: List of step_ids that must complete before this step.
        description: Human-readable description for observability.
    """
    step_id: str
    context: str
    tool: str
    args: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)
    description: str = ""


@dataclass
class TaskPlan:
    """A complete execution plan for a user goal.

    Attributes:
        goal: The original user goal.
        intent: Classified intent from intelligence tools.
        steps: Ordered list of DAG steps.
        primary_context: Which context handles most of the work.
        estimated_turns: How many vision/action turns are expected.
    """
    goal: str
    intent: str
    steps: List[DAGStep]
    primary_context: str
    estimated_turns: int = 1


class Architect:
    """Task planning and decomposition context.

    The Architect reads tool manifests from all 5 contexts, uses the
    397B model to decompose goals into DAGs, and dispatches steps to
    the appropriate context.  Zero hardcoded routing.

    Usage::

        architect = Architect()
        plan = await architect.plan("open WhatsApp and message Zach")
        # plan.steps = [
        #   DAGStep(context="executor", tool="apps.open_app", args={"app_name": "WhatsApp"}),
        #   DAGStep(context="executor", tool="screen.await_pixel_settlement"),
        #   DAGStep(context="executor", tool="screen.capture_and_compress"),
        #   ...
        # ]
    """

    TOOLS = {
        "intelligence.classify_intent": intelligence.classify_intent,
        "intelligence.detect_anomalies": intelligence.detect_anomalies,
        "intelligence.detect_patterns": intelligence.detect_patterns,
        "intelligence.get_environment_context": intelligence.get_environment_context,
        "memory.store_memory": memory.store_memory,
        "memory.recall_memory": memory.recall_memory,
        "memory.recall_similar_context": memory.recall_similar_context,
        "memory.find_patterns": memory.find_patterns,
    }

    async def classify(self, command: str) -> intelligence.IntentClassification:
        """Classify a command's intent to decide how to handle it.

        This is the entry point for all incoming commands.  The result
        determines which context handles the task and what complexity
        level of model to use.

        Args:
            command: Raw user command (voice or typed).

        Returns:
            IntentClassification with category, level, and suggested context.
        """
        return await intelligence.classify_intent(command)

    async def recall_relevant_context(self, goal: str) -> List[memory.MemoryEntry]:
        """Pull relevant past knowledge for a goal.

        Searches semantic memory for past interactions, preferences,
        and solutions related to the current goal.

        Args:
            goal: The user's goal to find context for.

        Returns:
            List of relevant memory entries.
        """
        return await memory.recall_similar_context(goal, limit=5)

    async def plan(self, goal: str) -> TaskPlan:
        """Decompose a goal into a DAG of executable steps.

        Uses intent classification and memory context to create an
        ordered list of tool calls across contexts.  This is where
        the 397B model's reasoning power is applied.

        Currently returns a simple linear plan based on intent.
        Future: full DAG planning via J-Prime reasoning endpoint.

        Args:
            goal: The user's goal in natural language.

        Returns:
            TaskPlan with steps, context assignments, and metadata.
        """
        intent = await self.classify(goal)
        context_name = intent.suggested_context or "executor"

        logger.info(
            "[Architect] Goal: %s -> intent=%s, context=%s, level=%s",
            goal[:60], intent.category, context_name, intent.level,
        )

        # For now, create a single-step plan that delegates to the
        # appropriate context.  Phase 2.5 will add full DAG planning
        # via J-Prime's reasoning graph.
        steps = [
            DAGStep(
                step_id="step-0",
                context=context_name,
                tool="__goal__",
                args={"goal": goal},
                description=f"Execute via {context_name}: {goal[:80]}",
            ),
        ]

        return TaskPlan(
            goal=goal,
            intent=intent.category,
            steps=steps,
            primary_context=context_name,
            estimated_turns=3 if intent.level in ("heavy", "complex") else 1,
        )

    @classmethod
    def tool_manifest(cls) -> List[Dict[str, str]]:
        """Return the Architect's tool manifest."""
        manifest = []
        for name, fn in cls.TOOLS.items():
            manifest.append({
                "name": name,
                "description": (fn.__doc__ or "").strip().split("\n")[0],
                "module": name.split(".")[0],
            })
        return manifest

    async def execute_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute an Architect tool by name."""
        fn = self.TOOLS.get(tool_name)
        if fn is None:
            raise KeyError(f"Unknown Architect tool: {tool_name}")
        if asyncio.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return fn(**kwargs)
