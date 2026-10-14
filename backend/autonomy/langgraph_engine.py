"""
LangGraph Autonomous Reasoning Engine for JARVIS

This module implements a sophisticated state machine using LangGraph that enables
autonomous reasoning with planning, reflection, and adaptive execution capabilities.

Features:
- Multi-node reasoning pipeline (analyze → plan → execute → reflect → learn)
- Dynamic state management with type safety
- Conditional routing based on confidence and context
- Integration with JARVIS autonomy systems
- Async-first design with parallel execution support
- Checkpointing and recovery capabilities
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any, Callable, Coroutine, Dict, Generic, List, Literal, Optional,
    Protocol, Sequence, Set, Tuple, Type, TypeVar, Union, cast
)
from uuid import uuid4

try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.prebuilt import ToolNode
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    StateGraph = None
    END = "end"
    MemorySaver = None
    ToolNode = None

from pydantic import BaseModel, Field, validator

logger = logging.getLogger(__name__)


# ============================================================================
# State Definitions
# ============================================================================

class ReasoningPhase(str, Enum):
    """Current phase in the reasoning pipeline."""
    INITIALIZING = "initializing"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    VALIDATING = "validating"
    EXECUTING = "executing"
    REFLECTING = "reflecting"
    LEARNING = "learning"
    COMPLETED = "completed"
    ERROR_RECOVERY = "error_recovery"
    WAITING_PERMISSION = "waiting_permission"


class ConfidenceLevel(str, Enum):
    """Confidence level for decisions and actions."""
    VERY_LOW = "very_low"      # < 0.2
    LOW = "low"                # 0.2 - 0.4
    MEDIUM = "medium"          # 0.4 - 0.6
    HIGH = "high"              # 0.6 - 0.8
    VERY_HIGH = "very_high"    # > 0.8

    @classmethod
    def from_score(cls, score: float) -> "ConfidenceLevel":
        """Convert numeric score to confidence level."""
        if score < 0.2:
            return cls.VERY_LOW
        elif score < 0.4:
            return cls.LOW
        elif score < 0.6:
            return cls.MEDIUM
        elif score < 0.8:
            return cls.HIGH
        return cls.VERY_HIGH


class ActionOutcome(str, Enum):
    """Possible outcomes of an action execution."""
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    ROLLBACK = "rollback"


@dataclass
class ContextSnapshot:
    """Immutable snapshot of context at a point in time."""
    timestamp: datetime
    user_state: Dict[str, Any]
    system_state: Dict[str, Any]
    workspace_state: Dict[str, Any]
    active_goals: List[str]
    environmental_factors: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "user_state": self.user_state,
            "system_state": self.system_state,
            "workspace_state": self.workspace_state,
            "active_goals": self.active_goals,
            "environmental_factors": self.environmental_factors
        }


@dataclass
class ReasoningStep:
    """Record of a single reasoning step in the pipeline."""
    step_id: str
    phase: ReasoningPhase
    input_data: Dict[str, Any]
    output_data: Dict[str, Any]
    reasoning: str
    confidence: float
    duration_ms: float
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "phase": self.phase.value,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp.isoformat()
        }


@dataclass
class PlannedAction:
    """An action planned by the reasoning engine."""
    action_id: str
    action_type: str
    target: str
    parameters: Dict[str, Any]
    priority: int
    confidence: float
    reasoning: str
    dependencies: List[str] = field(default_factory=list)
    requires_permission: bool = True
    rollback_available: bool = False
    estimated_duration_ms: float = 0
    risk_level: float = 0.0


@dataclass
class ExecutionResult:
    """Result of executing an action."""
    action_id: str
    outcome: ActionOutcome
    result_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    actual_duration_ms: float = 0
    side_effects: List[str] = field(default_factory=list)


class GraphState(BaseModel):
    """
    The state object that flows through the LangGraph pipeline.

    This represents the complete state at any point in the reasoning process,
    including context, plans, execution results, and learning signals.
    """
    # Identity
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str = Field(default_factory=lambda: str(uuid4()))

    # Current phase
    phase: ReasoningPhase = ReasoningPhase.INITIALIZING

    # Input/Output
    input_query: str = ""
    input_context: Dict[str, Any] = Field(default_factory=dict)
    final_response: Optional[str] = None

    # Context
    context_snapshot: Optional[Dict[str, Any]] = None
    inferred_goals: List[str] = Field(default_factory=list)
    user_intent: Optional[str] = None

    # Analysis
    analysis_result: Dict[str, Any] = Field(default_factory=dict)
    situation_assessment: Dict[str, Any] = Field(default_factory=dict)

    # Planning
    planned_actions: List[Dict[str, Any]] = Field(default_factory=list)
    action_graph: Dict[str, List[str]] = Field(default_factory=dict)  # dependency graph
    execution_strategy: str = "sequential"  # sequential, parallel, adaptive

    # Execution
    current_action_index: int = 0
    execution_results: List[Dict[str, Any]] = Field(default_factory=list)
    pending_permissions: List[str] = Field(default_factory=list)

    # Reflection & Learning
    reflection_notes: List[str] = Field(default_factory=list)
    learning_signals: Dict[str, Any] = Field(default_factory=dict)
    performance_metrics: Dict[str, float] = Field(default_factory=dict)

    # Control flow
    confidence: float = 0.0
    should_continue: bool = True
    max_iterations: int = 10
    current_iteration: int = 0
    error_count: int = 0
    max_errors: int = 3

    # History
    reasoning_trace: List[Dict[str, Any]] = Field(default_factory=list)

    # Timestamps
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    # Agent Runtime integration
    goal_validated: bool = False
    needs_replan: bool = False
    working_memory_ref: Optional[str] = None  # goal_id for cross-referencing
    verification_result: Optional[Dict[str, Any]] = None

    class Config:
        arbitrary_types_allowed = True


# ============================================================================
# Node Protocols & Base Classes
# ============================================================================

class ReasoningNode(Protocol):
    """Protocol for reasoning nodes in the graph."""

    async def process(self, state: GraphState) -> GraphState:
        """Process the state and return updated state."""
        ...


class BaseReasoningNode(ABC):
    """Base class for reasoning nodes with common functionality."""

    def __init__(self, name: str):
        self.name = name
        self.logger = logging.getLogger(f"{__name__}.{name}")

    @abstractmethod
    async def process(self, state: GraphState) -> GraphState:
        """Process the state and return updated state."""
        pass

    def _create_step_record(
        self,
        phase: ReasoningPhase,
        input_data: Dict[str, Any],
        output_data: Dict[str, Any],
        reasoning: str,
        confidence: float,
        duration_ms: float
    ) -> Dict[str, Any]:
        """Create a reasoning step record."""
        step = ReasoningStep(
            step_id=str(uuid4()),
            phase=phase,
            input_data=input_data,
            output_data=output_data,
            reasoning=reasoning,
            confidence=confidence,
            duration_ms=duration_ms
        )
        return step.to_dict()

    def _update_confidence(self, state: GraphState, local_confidence: float) -> float:
        """Update overall confidence using weighted average."""
        # Exponential moving average
        alpha = 0.3
        return alpha * local_confidence + (1 - alpha) * state.confidence


# ============================================================================
# Reasoning Nodes Implementation
# ============================================================================

class AnalysisNode(BaseReasoningNode):
    """
    Analyzes the input and context to understand the situation.

    Responsibilities:
    - Parse and understand user input
    - Analyze current context
    - Infer user goals and intent
    - Assess the situation
    """

    def __init__(self, llm_client: Optional[Any] = None):
        super().__init__("analysis")
        self.llm_client = llm_client

    async def process(self, state: GraphState) -> GraphState:
        start_time = time.time()
        self.logger.info(f"Analyzing input: {state.input_query[:100]}...")

        # Update phase
        state.phase = ReasoningPhase.ANALYZING

        # Perform analysis
        analysis_result = await self._analyze(state)

        # Update state
        state.analysis_result = analysis_result
        state.inferred_goals = analysis_result.get("inferred_goals", [])
        state.user_intent = analysis_result.get("primary_intent")
        state.situation_assessment = analysis_result.get("situation", {})

        # Calculate confidence
        local_confidence = analysis_result.get("confidence", 0.5)
        state.confidence = self._update_confidence(state, local_confidence)

        # Record step
        duration_ms = (time.time() - start_time) * 1000
        step_record = self._create_step_record(
            phase=ReasoningPhase.ANALYZING,
            input_data={"query": state.input_query, "context": state.input_context},
            output_data=analysis_result,
            reasoning=f"Analyzed input to understand intent: {state.user_intent}",
            confidence=local_confidence,
            duration_ms=duration_ms
        )
        state.reasoning_trace.append(step_record)

        return state

    async def _analyze(self, state: GraphState) -> Dict[str, Any]:
        """Perform the actual analysis."""
        # Dynamic analysis based on input characteristics
        query = state.input_query.lower()
        context = state.input_context

        # Intent classification patterns (dynamically loaded)
        intent_patterns = self._get_intent_patterns()

        inferred_intent = "general_query"
        intent_confidence = 0.5

        for intent, patterns in intent_patterns.items():
            for pattern in patterns:
                if pattern in query:
                    inferred_intent = intent
                    intent_confidence = 0.8
                    break

        # Extract entities and goals
        inferred_goals = self._extract_goals(query, context)

        # Situation assessment
        situation = {
            "complexity": self._assess_complexity(query, context),
            "urgency": self._assess_urgency(query, context),
            "risk_level": self._assess_risk(query, context),
            "requires_tools": self._check_tool_requirements(query, context),
            "multi_step": len(inferred_goals) > 1
        }

        return {
            "primary_intent": inferred_intent,
            "inferred_goals": inferred_goals,
            "situation": situation,
            "confidence": intent_confidence,
            "entities": self._extract_entities(query),
            "temporal_context": self._analyze_temporal(query)
        }

    def _get_intent_patterns(self) -> Dict[str, List[str]]:
        """Get intent patterns - designed to be dynamically configurable."""
        return {
            "automation": ["automate", "schedule", "recurring", "every day", "automatically"],
            "information": ["what is", "how do", "explain", "tell me about", "search for"],
            "action": ["open", "close", "start", "stop", "run", "execute", "launch"],
            "monitoring": ["watch", "monitor", "track", "alert", "notify"],
            "organization": ["organize", "sort", "clean up", "arrange", "file"],
            "communication": ["send", "message", "email", "notify", "respond"],
            "analysis": ["analyze", "review", "check", "examine", "investigate"]
        }

    def _extract_goals(self, query: str, context: Dict) -> List[str]:
        """Extract goals from the query and context."""
        goals = []
        # Use context to enhance goal extraction
        if "organize" in query or "clean" in query:
            goals.append("workspace_organization")
        if "send" in query or "message" in query:
            goals.append("communication")
        if "monitor" in query or "watch" in query:
            goals.append("monitoring_setup")
        if not goals:
            goals.append("general_assistance")
        return goals

    def _assess_complexity(self, query: str, context: Dict) -> str:
        """Assess task complexity."""
        word_count = len(query.split())
        if word_count > 50 or "and then" in query.lower():
            return "high"
        elif word_count > 20:
            return "medium"
        return "low"

    def _assess_urgency(self, query: str, context: Dict) -> str:
        """Assess task urgency."""
        urgent_keywords = ["urgent", "asap", "immediately", "now", "quick", "emergency"]
        if any(kw in query.lower() for kw in urgent_keywords):
            return "high"
        return "normal"

    def _assess_risk(self, query: str, context: Dict) -> float:
        """Assess risk level (0-1)."""
        high_risk_keywords = ["delete", "remove", "destroy", "format", "reset", "shutdown"]
        medium_risk_keywords = ["change", "modify", "update", "overwrite"]

        if any(kw in query.lower() for kw in high_risk_keywords):
            return 0.8
        elif any(kw in query.lower() for kw in medium_risk_keywords):
            return 0.5
        return 0.2

    def _check_tool_requirements(self, query: str, context: Dict) -> List[str]:
        """Check which tools might be needed."""
        tools = []
        tool_indicators = {
            "file_manager": ["file", "folder", "directory", "document"],
            "web_browser": ["search", "browse", "website", "url"],
            "terminal": ["command", "run", "execute", "terminal"],
            "calendar": ["schedule", "meeting", "appointment", "calendar"],
            "email": ["email", "mail", "send message"],
            "screenshot": ["screenshot", "capture", "screen"]
        }

        for tool, indicators in tool_indicators.items():
            if any(ind in query.lower() for ind in indicators):
                tools.append(tool)
        return tools

    def _extract_entities(self, query: str) -> Dict[str, List[str]]:
        """Extract named entities from query."""
        # Simplified entity extraction - can be enhanced with NLP
        return {
            "applications": [],
            "files": [],
            "people": [],
            "dates": [],
            "locations": []
        }

    def _analyze_temporal(self, query: str) -> Dict[str, Any]:
        """Analyze temporal aspects of the query."""
        return {
            "is_immediate": "now" in query.lower() or "immediately" in query.lower(),
            "is_scheduled": "at" in query.lower() or "schedule" in query.lower(),
            "is_recurring": "every" in query.lower() or "daily" in query.lower()
        }


class PlanningNode(BaseReasoningNode):
    """
    Creates an execution plan based on analysis.

    Responsibilities:
    - Generate action plans
    - Build dependency graphs
    - Optimize execution order
    - Estimate resources and time
    """

    def __init__(self, tool_registry: Optional[Any] = None):
        super().__init__("planning")
        self.tool_registry = tool_registry

    async def process(self, state: GraphState) -> GraphState:
        start_time = time.time()
        self.logger.info(f"Planning for goals: {state.inferred_goals}")

        # Update phase
        state.phase = ReasoningPhase.PLANNING

        # Generate plan
        plan = await self._create_plan(state)

        # Update state
        state.planned_actions = plan["actions"]
        state.action_graph = plan["dependency_graph"]
        state.execution_strategy = plan["strategy"]

        # Calculate confidence
        local_confidence = plan.get("confidence", 0.7)
        state.confidence = self._update_confidence(state, local_confidence)

        # Record step
        duration_ms = (time.time() - start_time) * 1000
        step_record = self._create_step_record(
            phase=ReasoningPhase.PLANNING,
            input_data={"goals": state.inferred_goals, "situation": state.situation_assessment},
            output_data=plan,
            reasoning=f"Created plan with {len(plan['actions'])} actions using {plan['strategy']} strategy",
            confidence=local_confidence,
            duration_ms=duration_ms
        )
        state.reasoning_trace.append(step_record)

        return state

    async def _create_plan(self, state: GraphState) -> Dict[str, Any]:
        """Create an execution plan."""
        actions = []
        dependency_graph = {}

        # Generate actions for each goal
        for idx, goal in enumerate(state.inferred_goals):
            goal_actions = self._generate_actions_for_goal(goal, state)

            for action in goal_actions:
                action["action_id"] = f"action_{len(actions)}"
                actions.append(action)
                dependency_graph[action["action_id"]] = action.get("dependencies", [])

        # Determine execution strategy
        strategy = self._determine_strategy(actions, state)

        # Optimize action order
        actions = self._optimize_order(actions, dependency_graph)

        return {
            "actions": actions,
            "dependency_graph": dependency_graph,
            "strategy": strategy,
            "confidence": self._calculate_plan_confidence(actions),
            "estimated_duration_ms": sum(a.get("estimated_duration_ms", 1000) for a in actions)
        }

    def _generate_actions_for_goal(self, goal: str, state: GraphState) -> List[Dict[str, Any]]:
        """Generate actions needed to achieve a goal."""
        action_templates = {
            "workspace_organization": [
                {
                    "action_type": "analyze_workspace",
                    "target": "current_desktop",
                    "parameters": {"depth": "full"},
                    "priority": 1,
                    "confidence": 0.9,
                    "requires_permission": False,
                    "estimated_duration_ms": 500
                },
                {
                    "action_type": "organize_windows",
                    "target": "all_windows",
                    "parameters": {"strategy": "by_category"},
                    "priority": 2,
                    "confidence": 0.8,
                    "requires_permission": True,
                    "estimated_duration_ms": 2000
                }
            ],
            "communication": [
                {
                    "action_type": "prepare_communication",
                    "target": "message_system",
                    "parameters": {},
                    "priority": 1,
                    "confidence": 0.85,
                    "requires_permission": True,
                    "estimated_duration_ms": 1000
                }
            ],
            "monitoring_setup": [
                {
                    "action_type": "configure_monitoring",
                    "target": "system",
                    "parameters": {"type": "continuous"},
                    "priority": 1,
                    "confidence": 0.9,
                    "requires_permission": True,
                    "estimated_duration_ms": 500
                }
            ],
            "general_assistance": [
                {
                    "action_type": "provide_information",
                    "target": "user",
                    "parameters": {"format": "detailed"},
                    "priority": 1,
                    "confidence": 0.7,
                    "requires_permission": False,
                    "estimated_duration_ms": 200
                }
            ]
        }

        return action_templates.get(goal, action_templates["general_assistance"])

    def _determine_strategy(self, actions: List[Dict], state: GraphState) -> str:
        """Determine the best execution strategy."""
        if len(actions) <= 1:
            return "sequential"

        # Check for dependencies
        has_dependencies = any(a.get("dependencies") for a in actions)

        # Check situation
        urgency = state.situation_assessment.get("urgency", "normal")
        risk = state.situation_assessment.get("risk_level", 0)

        if risk > 0.7:
            return "sequential"  # High risk = careful sequential execution
        elif urgency == "high" and not has_dependencies:
            return "parallel"  # Urgent + no deps = parallel
        elif has_dependencies:
            return "adaptive"  # Has deps = adaptive

        return "sequential"

    def _optimize_order(self, actions: List[Dict], dep_graph: Dict) -> List[Dict]:
        """Topologically sort actions based on dependencies."""
        # Simple topological sort
        sorted_actions = []
        remaining = actions.copy()
        completed_ids = set()

        while remaining:
            # Find actions with no pending dependencies
            ready = [
                a for a in remaining
                if all(dep in completed_ids for dep in dep_graph.get(a["action_id"], []))
            ]

            if not ready:
                # Circular dependency or error - just add remaining
                sorted_actions.extend(remaining)
                break

            # Sort ready actions by priority
            ready.sort(key=lambda x: x.get("priority", 999))

            # Add first ready action
            action = ready[0]
            sorted_actions.append(action)
            completed_ids.add(action["action_id"])
            remaining.remove(action)

        return sorted_actions

    def _calculate_plan_confidence(self, actions: List[Dict]) -> float:
        """Calculate overall plan confidence."""
        if not actions:
            return 0.5
        confidences = [a.get("confidence", 0.5) for a in actions]
        # Use geometric mean for plan confidence
        import math
        return math.exp(sum(math.log(c) for c in confidences) / len(confidences))


class ValidationNode(BaseReasoningNode):
    """
    Validates the plan before execution.

    Responsibilities:
    - Check permissions
    - Validate resources
    - Risk assessment
    - Safety checks
    """

    def __init__(self, permission_manager: Optional[Any] = None):
        super().__init__("validation")
        self.permission_manager = permission_manager

    async def process(self, state: GraphState) -> GraphState:
        start_time = time.time()
        self.logger.info(f"Validating plan with {len(state.planned_actions)} actions")

        # Update phase
        state.phase = ReasoningPhase.VALIDATING

        # Perform validation
        validation_result = await self._validate_plan(state)

        # Update state based on validation
        if validation_result["requires_permission"]:
            state.pending_permissions = validation_result["permission_requests"]
            state.phase = ReasoningPhase.WAITING_PERMISSION

        state.should_continue = validation_result["is_valid"]

        # Calculate confidence
        local_confidence = validation_result.get("confidence", 0.8)
        state.confidence = self._update_confidence(state, local_confidence)

        # Record step
        duration_ms = (time.time() - start_time) * 1000
        step_record = self._create_step_record(
            phase=ReasoningPhase.VALIDATING,
            input_data={"actions": state.planned_actions},
            output_data=validation_result,
            reasoning=f"Validation result: {'valid' if validation_result['is_valid'] else 'invalid'}",
            confidence=local_confidence,
            duration_ms=duration_ms
        )
        state.reasoning_trace.append(step_record)

        return state

    async def _validate_plan(self, state: GraphState) -> Dict[str, Any]:
        """Validate the execution plan."""
        issues = []
        permission_requests = []

        for action in state.planned_actions:
            # Check if action requires permission
            if action.get("requires_permission", True):
                # Check with permission manager if available
                if self.permission_manager:
                    has_permission = await self._check_permission(action)
                    if not has_permission:
                        permission_requests.append(action["action_id"])
                else:
                    # Default: request permission for anything that requires it
                    permission_requests.append(action["action_id"])

            # Risk validation
            if action.get("risk_level", 0) > 0.7:
                issues.append(f"High risk action: {action['action_type']}")

        is_valid = len(issues) == 0

        return {
            "is_valid": is_valid,
            "issues": issues,
            "requires_permission": len(permission_requests) > 0,
            "permission_requests": permission_requests,
            "confidence": 0.9 if is_valid else 0.5
        }

    async def _check_permission(self, action: Dict) -> bool:
        """Check if action is permitted."""
        if self.permission_manager is None:
            return False

        try:
            # Interface with existing permission manager
            return await self.permission_manager.check_permission(
                action_type=action["action_type"],
                target=action["target"],
                context=action.get("parameters", {})
            )
        except Exception as e:
            self.logger.warning(f"Permission check failed: {e}")
            return False


class ExecutionNode(BaseReasoningNode):
    """
    Executes the planned actions.

    Responsibilities:
    - Execute actions safely
    - Handle failures and retries
    - Track results
    - Support rollback
    """

    def __init__(self, action_executor: Optional[Any] = None, tool_orchestrator: Optional[Any] = None):
        super().__init__("execution")
        self.action_executor = action_executor
        self.tool_orchestrator = tool_orchestrator

    async def process(self, state: GraphState) -> GraphState:
        start_time = time.time()
        self.logger.info(f"Executing actions (strategy: {state.execution_strategy})")

        # Update phase
        state.phase = ReasoningPhase.EXECUTING

        # Execute based on strategy
        if state.execution_strategy == "parallel":
            results = await self._execute_parallel(state)
        elif state.execution_strategy == "adaptive":
            results = await self._execute_adaptive(state)
        else:
            results = await self._execute_sequential(state)

        # Update state
        state.execution_results = results

        # Check if all succeeded
        success_count = sum(1 for r in results if r["outcome"] == ActionOutcome.SUCCESS.value)
        total = len(results)

        # Calculate confidence
        local_confidence = success_count / total if total > 0 else 0
        state.confidence = self._update_confidence(state, local_confidence)

        # Update control flow
        if success_count < total:
            state.error_count += (total - success_count)

        state.should_continue = state.error_count < state.max_errors

        # Record step
        duration_ms = (time.time() - start_time) * 1000
        step_record = self._create_step_record(
            phase=ReasoningPhase.EXECUTING,
            input_data={"actions": state.planned_actions, "strategy": state.execution_strategy},
            output_data={"results": results, "success_rate": success_count / total if total else 0},
            reasoning=f"Executed {total} actions, {success_count} succeeded",
            confidence=local_confidence,
            duration_ms=duration_ms
        )
        state.reasoning_trace.append(step_record)

        return state

    async def _execute_sequential(self, state: GraphState) -> List[Dict[str, Any]]:
        """Execute actions sequentially."""
        results = []

        for action in state.planned_actions:
            result = await self._execute_single_action(action, state)
            results.append(result)

            # Stop on failure if not tolerant
            if result["outcome"] == ActionOutcome.FAILURE.value:
                self.logger.warning(f"Action {action['action_id']} failed, stopping sequential execution")
                break

        return results

    async def _execute_parallel(self, state: GraphState) -> List[Dict[str, Any]]:
        """Execute actions in parallel."""
        tasks = [
            self._execute_single_action(action, state)
            for action in state.planned_actions
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert exceptions to failure results
        processed_results = []
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                processed_results.append({
                    "action_id": state.planned_actions[idx]["action_id"],
                    "outcome": ActionOutcome.FAILURE.value,
                    "error_message": str(result)
                })
            else:
                processed_results.append(result)

        return processed_results

    async def _execute_adaptive(self, state: GraphState) -> List[Dict[str, Any]]:
        """Execute actions adaptively based on dependencies."""
        results = []
        completed = set()
        remaining = state.planned_actions.copy()

        while remaining:
            # Find actions whose dependencies are satisfied
            ready = [
                a for a in remaining
                if all(dep in completed for dep in state.action_graph.get(a["action_id"], []))
            ]

            if not ready:
                # Deadlock - execute remaining sequentially
                for action in remaining:
                    result = await self._execute_single_action(action, state)
                    results.append(result)
                break

            # Execute ready actions in parallel
            if len(ready) > 1:
                tasks = [self._execute_single_action(a, state) for a in ready]
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)

                for idx, result in enumerate(batch_results):
                    if isinstance(result, Exception):
                        results.append({
                            "action_id": ready[idx]["action_id"],
                            "outcome": ActionOutcome.FAILURE.value,
                            "error_message": str(result)
                        })
                    else:
                        results.append(result)
                    completed.add(ready[idx]["action_id"])
                    remaining.remove(ready[idx])
            else:
                # Single ready action
                action = ready[0]
                result = await self._execute_single_action(action, state)
                results.append(result)
                completed.add(action["action_id"])
                remaining.remove(action)

        return results

    async def _execute_single_action(self, action: Dict, state: GraphState) -> Dict[str, Any]:
        """Execute a single action."""
        start_time = time.time()

        try:
            # Use tool orchestrator if available
            if self.tool_orchestrator:
                result = await self.tool_orchestrator.execute(
                    action_type=action["action_type"],
                    target=action["target"],
                    parameters=action.get("parameters", {})
                )
            # Fall back to action executor
            elif self.action_executor:
                result = await self.action_executor.execute_action(action)
            else:
                # Simulate execution
                await asyncio.sleep(0.01)  # Minimal delay
                result = {"status": "simulated"}

            duration_ms = (time.time() - start_time) * 1000

            return {
                "action_id": action["action_id"],
                "outcome": ActionOutcome.SUCCESS.value,
                "result_data": result,
                "actual_duration_ms": duration_ms
            }

        except asyncio.TimeoutError:
            return {
                "action_id": action["action_id"],
                "outcome": ActionOutcome.TIMEOUT.value,
                "error_message": "Action timed out"
            }
        except Exception as e:
            self.logger.error(f"Action {action['action_id']} failed: {e}")
            return {
                "action_id": action["action_id"],
                "outcome": ActionOutcome.FAILURE.value,
                "error_message": str(e)
            }


class ReflectionNode(BaseReasoningNode):
    """
    Reflects on execution results and generates insights.

    Responsibilities:
    - Analyze results
    - Identify improvements
    - Generate feedback
    - Update strategies
    """

    def __init__(self):
        super().__init__("reflection")

    async def process(self, state: GraphState) -> GraphState:
        start_time = time.time()
        self.logger.info("Reflecting on execution results")

        # Update phase
        state.phase = ReasoningPhase.REFLECTING

        # Perform reflection
        reflection = await self._reflect(state)

        # Update state
        state.reflection_notes = reflection["notes"]
        state.learning_signals = reflection["learning_signals"]
        state.performance_metrics = reflection["metrics"]

        # Generate final response
        state.final_response = self._generate_response(state, reflection)

        # Calculate confidence
        local_confidence = reflection.get("confidence", 0.8)
        state.confidence = self._update_confidence(state, local_confidence)

        # Record step
        duration_ms = (time.time() - start_time) * 1000
        step_record = self._create_step_record(
            phase=ReasoningPhase.REFLECTING,
            input_data={"results": state.execution_results},
            output_data=reflection,
            reasoning=f"Reflected on {len(state.execution_results)} results",
            confidence=local_confidence,
            duration_ms=duration_ms
        )
        state.reasoning_trace.append(step_record)

        return state

    async def _reflect(self, state: GraphState) -> Dict[str, Any]:
        """Perform reflection on results."""
        notes = []
        learning_signals = {}

        # Analyze execution results
        results = state.execution_results
        success_count = sum(1 for r in results if r["outcome"] == ActionOutcome.SUCCESS.value)
        failure_count = sum(1 for r in results if r["outcome"] == ActionOutcome.FAILURE.value)

        success_rate = success_count / len(results) if results else 0

        # Generate notes
        if success_rate == 1.0:
            notes.append("All actions completed successfully")
        elif success_rate >= 0.7:
            notes.append(f"Most actions succeeded ({success_count}/{len(results)})")
            notes.append("Some actions failed - review for improvements")
        else:
            notes.append(f"Many actions failed ({failure_count}/{len(results)})")
            notes.append("Significant improvements needed")

        # Generate learning signals
        learning_signals = {
            "success_rate": success_rate,
            "avg_duration": sum(r.get("actual_duration_ms", 0) for r in results) / len(results) if results else 0,
            "strategy_effectiveness": self._evaluate_strategy(state),
            "plan_accuracy": self._evaluate_plan_accuracy(state),
            "should_adjust_confidence": abs(state.confidence - success_rate) > 0.2
        }

        # Calculate metrics
        metrics = {
            "total_actions": len(results),
            "successful_actions": success_count,
            "failed_actions": failure_count,
            "success_rate": success_rate,
            "total_duration_ms": sum(r.get("actual_duration_ms", 0) for r in results),
            "confidence_calibration": abs(state.confidence - success_rate)
        }

        return {
            "notes": notes,
            "learning_signals": learning_signals,
            "metrics": metrics,
            "confidence": success_rate
        }

    def _evaluate_strategy(self, state: GraphState) -> float:
        """Evaluate how well the execution strategy worked."""
        # Compare planned vs actual
        results = state.execution_results
        if not results:
            return 0.5

        # Check if parallel execution helped
        if state.execution_strategy == "parallel":
            total_duration = max(r.get("actual_duration_ms", 0) for r in results)
            sequential_estimate = sum(r.get("actual_duration_ms", 0) for r in results)
            if sequential_estimate > 0:
                return min(1.0, (sequential_estimate - total_duration) / sequential_estimate + 0.5)

        return 0.7  # Default effectiveness

    def _evaluate_plan_accuracy(self, state: GraphState) -> float:
        """Evaluate how accurate the plan was."""
        if not state.planned_actions or not state.execution_results:
            return 0.5

        # Compare planned confidence with actual success
        planned_conf = sum(a.get("confidence", 0.5) for a in state.planned_actions) / len(state.planned_actions)
        actual_success = sum(1 for r in state.execution_results if r["outcome"] == ActionOutcome.SUCCESS.value) / len(state.execution_results)

        # Lower is better (closer calibration)
        accuracy = 1 - abs(planned_conf - actual_success)
        return accuracy

    def _generate_response(self, state: GraphState, reflection: Dict) -> str:
        """Generate final response for user."""
        metrics = reflection["metrics"]
        notes = reflection["notes"]

        response_parts = []

        # Summary
        if metrics["success_rate"] == 1.0:
            response_parts.append(f"Successfully completed all {metrics['total_actions']} actions.")
        elif metrics["success_rate"] > 0:
            response_parts.append(
                f"Completed {metrics['successful_actions']} of {metrics['total_actions']} actions "
                f"({metrics['success_rate']:.0%} success rate)."
            )
        else:
            response_parts.append("Could not complete the requested actions.")

        # Add relevant notes
        for note in notes[:2]:  # Limit notes
            response_parts.append(note)

        return " ".join(response_parts)


class LearningNode(BaseReasoningNode):
    """
    Updates learning models based on experience.

    Responsibilities:
    - Update permission models
    - Adjust confidence calibration
    - Store successful patterns
    - Feed feedback loops
    """

    def __init__(self, learning_db: Optional[Any] = None):
        super().__init__("learning")
        self.learning_db = learning_db

    async def process(self, state: GraphState) -> GraphState:
        start_time = time.time()
        self.logger.info("Processing learning signals")

        # Update phase
        state.phase = ReasoningPhase.LEARNING

        # Process learning
        learning_result = await self._learn(state)

        # Update state
        state.completed_at = datetime.utcnow().isoformat()
        state.phase = ReasoningPhase.COMPLETED

        # Record step
        duration_ms = (time.time() - start_time) * 1000
        step_record = self._create_step_record(
            phase=ReasoningPhase.LEARNING,
            input_data={"signals": state.learning_signals},
            output_data=learning_result,
            reasoning=f"Updated {learning_result['updates_count']} learning parameters",
            confidence=0.9,
            duration_ms=duration_ms
        )
        state.reasoning_trace.append(step_record)

        return state

    async def _learn(self, state: GraphState) -> Dict[str, Any]:
        """Process learning signals and update models."""
        updates_count = 0

        # Store experience if learning DB available
        if self.learning_db:
            try:
                experience = {
                    "session_id": state.session_id,
                    "run_id": state.run_id,
                    "input": state.input_query,
                    "goals": state.inferred_goals,
                    "actions": state.planned_actions,
                    "results": state.execution_results,
                    "metrics": state.performance_metrics,
                    "learning_signals": state.learning_signals,
                    "timestamp": datetime.utcnow().isoformat()
                }
                await self._store_experience(experience)
                updates_count += 1
            except Exception as e:
                self.logger.warning(f"Failed to store experience: {e}")

        # Process specific learning signals
        signals = state.learning_signals

        # Confidence calibration
        if signals.get("should_adjust_confidence"):
            updates_count += 1

        # Strategy effectiveness
        if signals.get("strategy_effectiveness", 0.5) < 0.5:
            # Strategy wasn't effective - learn alternative
            updates_count += 1

        return {
            "updates_count": updates_count,
            "stored_experience": self.learning_db is not None,
            "calibration_adjusted": signals.get("should_adjust_confidence", False)
        }

    async def _store_experience(self, experience: Dict) -> None:
        """Store experience in learning database."""
        if self.learning_db is None:
            return

        try:
            # Interface with JARVIS learning database
            await self.learning_db.store_experience(experience)
        except Exception as e:
            self.logger.error(f"Failed to store experience: {e}")


# ============================================================================
# Router Functions
# ============================================================================

def route_after_analysis(state: GraphState) -> str:
    """Route after analysis node."""
    if state.confidence < 0.2:
        return "error_recovery"
    return "planning"


def route_after_validation(state: GraphState) -> str:
    """Route after validation node."""
    if not state.should_continue:
        return "reflection"
    if state.phase == ReasoningPhase.WAITING_PERMISSION:
        return "wait_permission"
    return "execution"


def route_after_execution(state: GraphState) -> str:
    """Route after execution node."""
    if state.error_count >= state.max_errors:
        return "error_recovery"
    return "reflection"


def route_after_reflection(state: GraphState) -> str:
    """Route after reflection node.

    Enhanced for Agent Runtime integration:
    - goal_validated: short-circuit to learning (goal confirmed complete)
    - needs_replan: force re-analysis regardless of confidence
    - Original low-confidence retry preserved
    """
    # Agent Runtime: goal validated → proceed to learning
    if state.goal_validated:
        return "learning"

    # Agent Runtime: explicit replan requested → re-analyze
    if state.needs_replan:
        return "analysis"

    # Original logic: iterate if confidence is low
    if state.current_iteration < state.max_iterations and state.should_continue:
        if state.confidence < 0.5:
            return "analysis"

    return "learning"


def should_continue(state: GraphState) -> str:
    """Determine if we should continue or end."""
    if state.phase == ReasoningPhase.COMPLETED:
        return END
    if state.error_count >= state.max_errors:
        return END
    if state.current_iteration >= state.max_iterations:
        return END
    return "continue"


# ============================================================================
# Main Engine
# ============================================================================

class LangGraphReasoningEngine:
    """
    Main LangGraph-based reasoning engine for JARVIS.

    Orchestrates the multi-node reasoning pipeline with support for:
    - Dynamic node registration
    - Checkpointing and recovery
    - Async execution
    - Integration with JARVIS subsystems
    """

    def __init__(
        self,
        permission_manager: Optional[Any] = None,
        action_executor: Optional[Any] = None,
        tool_orchestrator: Optional[Any] = None,
        learning_db: Optional[Any] = None,
        llm_client: Optional[Any] = None,
        enable_checkpointing: bool = True
    ):
        self.permission_manager = permission_manager
        self.action_executor = action_executor
        self.tool_orchestrator = tool_orchestrator
        self.learning_db = learning_db
        self.llm_client = llm_client
        self.enable_checkpointing = enable_checkpointing

        self.logger = logging.getLogger(__name__)

        # Initialize nodes
        self._init_nodes()

        # Build graph
        self.graph = self._build_graph()

        # Checkpointer
        self.checkpointer = MemorySaver() if LANGGRAPH_AVAILABLE and enable_checkpointing else None

        # Compile graph
        self.compiled_graph = self._compile_graph()

    def _init_nodes(self):
        """Initialize reasoning nodes."""
        self.analysis_node = AnalysisNode(llm_client=self.llm_client)
        self.planning_node = PlanningNode()
        self.validation_node = ValidationNode(permission_manager=self.permission_manager)
        self.execution_node = ExecutionNode(
            action_executor=self.action_executor,
            tool_orchestrator=self.tool_orchestrator
        )
        self.reflection_node = ReflectionNode()
        self.learning_node = LearningNode(learning_db=self.learning_db)

    def _build_graph(self) -> Optional[StateGraph]:
        """Build the LangGraph state graph."""
        if not LANGGRAPH_AVAILABLE:
            from backend.core.optional_dependencies import note_missing
            note_missing("langgraph", "LangGraph reasoning engine (fallback execution)", install="langgraph")
            return None

        # Create state graph
        graph = StateGraph(GraphState)

        # Add nodes
        graph.add_node("analysis", self._wrap_node(self.analysis_node))
        graph.add_node("planning", self._wrap_node(self.planning_node))
        graph.add_node("validation", self._wrap_node(self.validation_node))
        graph.add_node("execution", self._wrap_node(self.execution_node))
        graph.add_node("reflection", self._wrap_node(self.reflection_node))
        graph.add_node("learning", self._wrap_node(self.learning_node))
        graph.add_node("error_recovery", self._error_recovery_node)
        graph.add_node("wait_permission", self._wait_permission_node)

        # Set entry point
        graph.set_entry_point("analysis")

        # Add edges with conditional routing
        graph.add_conditional_edges(
            "analysis",
            route_after_analysis,
            {
                "planning": "planning",
                "error_recovery": "error_recovery"
            }
        )

        graph.add_edge("planning", "validation")

        graph.add_conditional_edges(
            "validation",
            route_after_validation,
            {
                "execution": "execution",
                "reflection": "reflection",
                "wait_permission": "wait_permission"
            }
        )

        graph.add_edge("wait_permission", "execution")

        graph.add_conditional_edges(
            "execution",
            route_after_execution,
            {
                "reflection": "reflection",
                "error_recovery": "error_recovery"
            }
        )

        graph.add_conditional_edges(
            "reflection",
            route_after_reflection,
            {
                "learning": "learning",
                "analysis": "analysis"
            }
        )

        graph.add_edge("learning", END)
        graph.add_edge("error_recovery", "reflection")

        return graph

    def _compile_graph(self):
        """Compile the graph with checkpointing."""
        if self.graph is None:
            return None

        compile_kwargs = {}
        if self.checkpointer:
            compile_kwargs["checkpointer"] = self.checkpointer

        return self.graph.compile(**compile_kwargs)

    def _wrap_node(self, node: BaseReasoningNode):
        """Wrap a node for LangGraph compatibility."""
        async def wrapped(state: GraphState) -> GraphState:
            return await node.process(state)
        return wrapped

    async def _error_recovery_node(self, state: GraphState) -> GraphState:
        """Handle error recovery."""
        self.logger.warning(f"Entering error recovery (errors: {state.error_count})")

        state.phase = ReasoningPhase.ERROR_RECOVERY
        state.reflection_notes.append(f"Entered error recovery after {state.error_count} errors")

        # Try to salvage what we can
        successful_results = [
            r for r in state.execution_results
            if r["outcome"] == ActionOutcome.SUCCESS.value
        ]

        if successful_results:
            state.reflection_notes.append(
                f"Recovered {len(successful_results)} successful actions"
            )

        return state

    async def _wait_permission_node(self, state: GraphState) -> GraphState:
        """Handle permission waiting."""
        self.logger.info(f"Waiting for permission on {len(state.pending_permissions)} actions")

        # In real implementation, this would:
        # 1. Request permissions from permission manager
        # 2. Wait for user response or auto-approval
        # 3. Update state based on response

        # For now, we'll auto-approve for demonstration
        state.pending_permissions = []
        state.phase = ReasoningPhase.EXECUTING

        return state

    async def run(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None
    ) -> GraphState:
        """
        Run the reasoning pipeline.

        Args:
            query: User query or input
            context: Additional context information
            config: Runtime configuration

        Returns:
            Final state after reasoning
        """
        # Initialize state
        initial_state = GraphState(
            input_query=query,
            input_context=context or {},
            started_at=datetime.utcnow().isoformat()
        )

        # Run graph if available
        if self.compiled_graph is not None:
            try:
                run_config = {"configurable": {"thread_id": initial_state.session_id}}
                if config:
                    run_config.update(config)

                # Run the graph
                final_state = await self.compiled_graph.ainvoke(
                    initial_state,
                    config=run_config
                )
                return final_state
            except Exception as e:
                self.logger.error(f"Graph execution failed: {e}")
                return await self._fallback_execution(initial_state)
        else:
            return await self._fallback_execution(initial_state)

    async def _fallback_execution(self, state: GraphState) -> GraphState:
        """Fallback execution when LangGraph is not available."""
        self.logger.info("Using fallback sequential execution")

        # Sequential execution through nodes
        state = await self.analysis_node.process(state)

        if state.confidence >= 0.2:
            state = await self.planning_node.process(state)
            state = await self.validation_node.process(state)

            if state.should_continue:
                state = await self.execution_node.process(state)

            state = await self.reflection_node.process(state)
            state = await self.learning_node.process(state)
        else:
            state.phase = ReasoningPhase.ERROR_RECOVERY
            state.final_response = "Could not understand the request with sufficient confidence."

        state.completed_at = datetime.utcnow().isoformat()
        return state

    async def get_checkpoint(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Get checkpoint for a thread."""
        if self.checkpointer is None:
            return None

        try:
            checkpoint = await self.checkpointer.aget(
                {"configurable": {"thread_id": thread_id}}
            )
            return checkpoint
        except Exception as e:
            self.logger.error(f"Failed to get checkpoint: {e}")
            return None

    async def resume_from_checkpoint(
        self,
        thread_id: str,
        updates: Optional[Dict[str, Any]] = None
    ) -> GraphState:
        """Resume execution from a checkpoint."""
        if self.compiled_graph is None or self.checkpointer is None:
            raise RuntimeError("Checkpointing not available")

        config = {"configurable": {"thread_id": thread_id}}

        if updates:
            # Apply updates before resuming
            checkpoint = await self.get_checkpoint(thread_id)
            if checkpoint:
                state = checkpoint.get("channel_values", {})
                state.update(updates)

        # Resume execution
        return await self.compiled_graph.ainvoke(None, config=config)


# ============================================================================
# Factory Functions
# ============================================================================

def create_reasoning_engine(
    permission_manager: Optional[Any] = None,
    action_executor: Optional[Any] = None,
    tool_orchestrator: Optional[Any] = None,
    learning_db: Optional[Any] = None,
    llm_client: Optional[Any] = None,
    enable_checkpointing: bool = True
) -> LangGraphReasoningEngine:
    """
    Factory function to create a configured reasoning engine.

    Args:
        permission_manager: JARVIS permission manager instance
        action_executor: JARVIS action executor instance
        tool_orchestrator: Tool orchestrator for execution
        learning_db: Learning database for persistence
        llm_client: LLM client for advanced reasoning
        enable_checkpointing: Enable state checkpointing

    Returns:
        Configured LangGraphReasoningEngine instance
    """
    return LangGraphReasoningEngine(
        permission_manager=permission_manager,
        action_executor=action_executor,
        tool_orchestrator=tool_orchestrator,
        learning_db=learning_db,
        llm_client=llm_client,
        enable_checkpointing=enable_checkpointing
    )


async def quick_reason(query: str, context: Optional[Dict] = None) -> str:
    """
    Quick reasoning helper for simple queries.

    Args:
        query: User query
        context: Optional context

    Returns:
        Final response string
    """
    engine = create_reasoning_engine(enable_checkpointing=False)
    state = await engine.run(query, context)
    return state.final_response or "No response generated."
