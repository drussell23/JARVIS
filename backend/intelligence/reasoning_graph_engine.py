"""
JARVIS Advanced Reasoning Graph Engine with Multi-Branch Execution

This module implements a sophisticated reasoning graph that enables JARVIS to:
- Generate multiple solution branches in parallel
- Dynamically switch between approaches when one fails
- Learn from failures to generate better alternatives
- Narrate its thinking process in real-time
- Continuously cycle until success or human intervention

Architecture:
    User Input
        |
        v
    [Analyze Problem] -----> [Generate Solution Branches]
        |                           |
        |                    Branch A    Branch B    Branch C
        |                      |            |            |
        v                      v            v            v
    [Evaluate Branches] <-- [Execute] <-- [Execute] <-- [Execute]
        |                      |            |            |
        |                  Success?      Success?     Success?
        |                      |            |            |
        v                      v            v            v
    [Synthesize Learning] --> [If all fail: Generate new branches]
        |
        v
    [Final Response with Narration]

Author: JARVIS AI System
Version: 2.0.0 - Multi-Branch Architecture
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any, Awaitable, Callable, Coroutine, Deque, Dict, Generic, List,
    Literal, Optional, Protocol, Set, Tuple, Type, TypeVar, Union
)
from uuid import uuid4
import weakref

try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    StateGraph = None
    END = "end"
    MemorySaver = None

from pydantic import BaseModel, Field

# Phase 5A: Bounded queue backpressure
try:
    from backend.core.bounded_queue import BoundedAsyncQueue, OverflowPolicy
except ImportError:
    BoundedAsyncQueue = None

logger = logging.getLogger(__name__)


# ============================================================================
# Enums and Types
# ============================================================================

class BranchStatus(str, Enum):
    """Status of a solution branch."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"
    ABANDONED = "abandoned"
    BLOCKED = "blocked"


class ReasoningEvent(str, Enum):
    """Events that can be narrated."""
    ANALYZING = "analyzing"
    BRANCHING = "branching"
    EXECUTING = "executing"
    FAILED = "failed"
    RECOVERING = "recovering"
    LEARNING = "learning"
    SUCCESS = "success"
    SYNTHESIZING = "synthesizing"


class NarrationStyle(str, Enum):
    """Style of voice narration."""
    CONCISE = "concise"      # Brief updates
    DETAILED = "detailed"    # Full explanation
    TECHNICAL = "technical"  # Technical details
    CASUAL = "casual"        # Conversational


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class SolutionBranch:
    """A single solution branch being explored."""
    branch_id: str
    description: str
    approach: str
    priority: float = 0.5
    confidence: float = 0.5
    status: BranchStatus = BranchStatus.PENDING
    parent_branch_id: Optional[str] = None
    child_branches: List[str] = field(default_factory=list)
    execution_steps: List[Dict[str, Any]] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    failure_reason: Optional[str] = None
    learning_insights: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    execution_time_ms: float = 0
    retry_count: int = 0
    max_retries: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "description": self.description,
            "approach": self.approach,
            "priority": self.priority,
            "confidence": self.confidence,
            "status": self.status.value,
            "parent_branch_id": self.parent_branch_id,
            "child_branches": self.child_branches,
            "failure_reason": self.failure_reason,
            "learning_insights": self.learning_insights,
            "execution_time_ms": self.execution_time_ms,
            "retry_count": self.retry_count
        }


@dataclass
class NarrationMessage:
    """A message to be narrated."""
    message_id: str
    event: ReasoningEvent
    text: str
    priority: int = 1  # Higher = more important
    branch_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FailureAnalysis:
    """Analysis of why a solution failed."""
    failure_id: str
    branch_id: str
    error_type: str
    error_message: str
    root_cause: Optional[str] = None
    suggested_alternatives: List[str] = field(default_factory=list)
    similar_past_failures: List[Dict[str, Any]] = field(default_factory=list)
    recovery_probability: float = 0.5
    learning_value: float = 0.5


# ============================================================================
# Narration System
# ============================================================================

class VoiceNarrator:
    """
    Handles real-time narration of JARVIS's reasoning process.

    Integrates with TTS systems to provide voice output of what
    JARVIS is thinking and doing.
    """

    def __init__(
        self,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        style: NarrationStyle = NarrationStyle.DETAILED,
        enabled: bool = True
    ):
        self.tts_callback = tts_callback
        self.style = style
        self.enabled = enabled
        self._message_queue: asyncio.Queue[NarrationMessage] = (
            BoundedAsyncQueue(maxsize=200, policy=OverflowPolicy.DROP_OLDEST, name="narration_messages")
            if BoundedAsyncQueue is not None else asyncio.Queue()
        )
        self._narration_history: List[NarrationMessage] = []
        self._is_speaking = False
        self._suppress_similar_threshold = 3.0  # seconds
        self.logger = logging.getLogger(f"{__name__}.narrator")

        # Event templates for natural narration
        self._templates = {
            ReasoningEvent.ANALYZING: {
                NarrationStyle.CONCISE: "Analyzing {count} potential solutions.",
                NarrationStyle.DETAILED: "I'm analyzing the problem. I see {count} potential approaches we could try.",
                NarrationStyle.TECHNICAL: "Initiating multi-branch analysis. Evaluating {count} solution vectors.",
                NarrationStyle.CASUAL: "Let me think about this... I see {count} ways we could tackle it."
            },
            ReasoningEvent.BRANCHING: {
                NarrationStyle.CONCISE: "Trying approach: {approach}.",
                NarrationStyle.DETAILED: "I'm going to try {approach}. This has a {confidence}% chance of working.",
                NarrationStyle.TECHNICAL: "Executing branch {branch_id}: {approach}. Confidence: {confidence}%.",
                NarrationStyle.CASUAL: "Okay, let me try {approach}."
            },
            ReasoningEvent.EXECUTING: {
                NarrationStyle.CONCISE: "Executing step {step}.",
                NarrationStyle.DETAILED: "Now executing step {step}: {description}.",
                NarrationStyle.TECHNICAL: "Step {step}/{total}: {description}. ETA: {eta}ms.",
                NarrationStyle.CASUAL: "Working on {description}..."
            },
            ReasoningEvent.FAILED: {
                NarrationStyle.CONCISE: "That didn't work. Trying alternative.",
                NarrationStyle.DETAILED: "That approach didn't work because {reason}. Let me try a different strategy.",
                NarrationStyle.TECHNICAL: "Branch {branch_id} failed: {reason}. Initiating fallback to alternative branch.",
                NarrationStyle.CASUAL: "Hmm, that didn't work. {reason}. Let me try something else."
            },
            ReasoningEvent.RECOVERING: {
                NarrationStyle.CONCISE: "Recovering. Found {count} alternatives.",
                NarrationStyle.DETAILED: "I've analyzed why that failed and found {count} alternative approaches. Trying the most promising one.",
                NarrationStyle.TECHNICAL: "Recovery initiated. Generated {count} alternative branches from failure analysis.",
                NarrationStyle.CASUAL: "No worries, I learned from that. I have {count} new ideas to try."
            },
            ReasoningEvent.LEARNING: {
                NarrationStyle.CONCISE: "Learning from this attempt.",
                NarrationStyle.DETAILED: "I'm storing what I learned: {insight}. This will help with similar problems.",
                NarrationStyle.TECHNICAL: "Recording learning signal: {insight}. Updating decision weights.",
                NarrationStyle.CASUAL: "Good to know: {insight}. I'll remember that."
            },
            ReasoningEvent.SUCCESS: {
                NarrationStyle.CONCISE: "Success!",
                NarrationStyle.DETAILED: "That worked! {description}",
                NarrationStyle.TECHNICAL: "Branch {branch_id} completed successfully. Result: {description}",
                NarrationStyle.CASUAL: "Got it! {description}"
            },
            ReasoningEvent.SYNTHESIZING: {
                NarrationStyle.CONCISE: "Combining results.",
                NarrationStyle.DETAILED: "I'm combining the results from multiple approaches to give you the best answer.",
                NarrationStyle.TECHNICAL: "Synthesizing {count} partial results. Applying weighted aggregation.",
                NarrationStyle.CASUAL: "Let me put all of this together for you."
            }
        }

    async def narrate(
        self,
        event: ReasoningEvent,
        context: Optional[Dict[str, Any]] = None,
        priority: int = 1
    ) -> None:
        """
        Generate and queue a narration message.

        Args:
            event: Type of reasoning event
            context: Context data for template substitution
            priority: Message priority (higher = more important)
        """
        if not self.enabled:
            return

        context = context or {}

        # Get template for event and style
        templates = self._templates.get(event, {})
        template = templates.get(self.style, templates.get(NarrationStyle.DETAILED, "Processing..."))

        # Format message
        try:
            text = template.format(**context)
        except KeyError:
            text = template  # Use template as-is if formatting fails

        message = NarrationMessage(
            message_id=str(uuid4()),
            event=event,
            text=text,
            priority=priority,
            branch_id=context.get("branch_id"),
            metadata=context
        )

        # Check for similar recent messages
        if not self._should_suppress(message):
            self._narration_history.append(message)
            await self._message_queue.put(message)

            # Speak immediately if callback available
            if self.tts_callback:
                await self._speak(message)

    def _should_suppress(self, message: NarrationMessage) -> bool:
        """Check if message is too similar to recent ones."""
        if not self._narration_history:
            return False

        now = datetime.utcnow()
        for recent in reversed(self._narration_history[-5:]):
            time_diff = (now - recent.timestamp).total_seconds()
            if time_diff < self._suppress_similar_threshold:
                if recent.event == message.event and recent.text == message.text:
                    return True
        return False

    async def _speak(self, message: NarrationMessage) -> None:
        """Send message to TTS system."""
        if self._is_speaking:
            return  # Don't interrupt current speech

        self._is_speaking = True
        try:
            if self.tts_callback:
                await self.tts_callback(message.text)
        except Exception as e:
            self.logger.warning(f"TTS failed: {e}")
        finally:
            self._is_speaking = False

    def set_style(self, style: NarrationStyle) -> None:
        """Change narration style."""
        self.style = style

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get narration history."""
        return [
            {
                "text": m.text,
                "event": m.event.value,
                "timestamp": m.timestamp.isoformat()
            }
            for m in self._narration_history[-limit:]
        ]


# ============================================================================
# Solution Branch Manager
# ============================================================================

class SolutionBranchManager:
    """
    Manages multiple solution branches and their lifecycle.

    Responsible for:
    - Creating new branches
    - Tracking branch status
    - Generating alternatives from failures
    - Learning from outcomes
    """

    def __init__(
        self,
        max_parallel_branches: int = 3,
        max_total_branches: int = 10,
        min_confidence_threshold: float = 0.3,
        learning_callback: Optional[Callable[[Dict], Awaitable[None]]] = None
    ):
        self.max_parallel_branches = max_parallel_branches
        self.max_total_branches = max_total_branches
        self.min_confidence_threshold = min_confidence_threshold
        self.learning_callback = learning_callback

        self._branches: Dict[str, SolutionBranch] = {}
        self._active_branches: Set[str] = set()
        self._completed_branches: Set[str] = set()
        self._failed_branches: Set[str] = set()
        self._failure_patterns: Dict[str, List[FailureAnalysis]] = defaultdict(list)

        self.logger = logging.getLogger(f"{__name__}.branch_manager")

    def create_branch(
        self,
        description: str,
        approach: str,
        priority: float = 0.5,
        confidence: float = 0.5,
        parent_id: Optional[str] = None,
        execution_steps: Optional[List[Dict]] = None
    ) -> SolutionBranch:
        """Create a new solution branch."""
        if len(self._branches) >= self.max_total_branches:
            # Prune lowest priority failed branches
            self._prune_branches()

        branch = SolutionBranch(
            branch_id=str(uuid4()),
            description=description,
            approach=approach,
            priority=priority,
            confidence=confidence,
            parent_branch_id=parent_id,
            execution_steps=execution_steps or []
        )

        self._branches[branch.branch_id] = branch

        if parent_id and parent_id in self._branches:
            self._branches[parent_id].child_branches.append(branch.branch_id)

        self.logger.info(f"Created branch {branch.branch_id}: {description[:50]}...")
        return branch

    def activate_branch(self, branch_id: str) -> bool:
        """Activate a branch for execution."""
        if len(self._active_branches) >= self.max_parallel_branches:
            return False

        if branch_id in self._branches:
            self._branches[branch_id].status = BranchStatus.IN_PROGRESS
            self._active_branches.add(branch_id)
            return True
        return False

    def complete_branch(
        self,
        branch_id: str,
        success: bool,
        results: Optional[List[Dict]] = None,
        failure_reason: Optional[str] = None
    ) -> None:
        """Mark a branch as complete."""
        if branch_id not in self._branches:
            return

        branch = self._branches[branch_id]
        branch.completed_at = datetime.utcnow()
        branch.results = results or []

        if success:
            branch.status = BranchStatus.SUCCESS
            self._completed_branches.add(branch_id)
        else:
            branch.status = BranchStatus.FAILED
            branch.failure_reason = failure_reason
            self._failed_branches.add(branch_id)

            # Analyze failure
            self._analyze_failure(branch)

        self._active_branches.discard(branch_id)

    def _analyze_failure(self, branch: SolutionBranch) -> FailureAnalysis:
        """Analyze why a branch failed and extract learnings."""
        analysis = FailureAnalysis(
            failure_id=str(uuid4()),
            branch_id=branch.branch_id,
            error_type=self._classify_error(branch.failure_reason),
            error_message=branch.failure_reason or "Unknown error",
            root_cause=self._identify_root_cause(branch),
            suggested_alternatives=self._generate_alternatives(branch),
            recovery_probability=self._estimate_recovery_probability(branch),
            learning_value=self._calculate_learning_value(branch)
        )

        # Store for pattern recognition
        error_key = analysis.error_type
        self._failure_patterns[error_key].append(analysis)

        # Extract learning insights
        branch.learning_insights = [
            f"Approach '{branch.approach}' failed due to: {analysis.error_type}",
            f"Root cause identified: {analysis.root_cause}",
            f"Alternative strategies: {', '.join(analysis.suggested_alternatives[:3])}"
        ]

        return analysis

    def _classify_error(self, error: Optional[str]) -> str:
        """Classify the type of error."""
        if not error:
            return "unknown"

        error_lower = error.lower()

        if any(kw in error_lower for kw in ["timeout", "timed out", "too long"]):
            return "timeout"
        elif any(kw in error_lower for kw in ["permission", "denied", "access"]):
            return "permission"
        elif any(kw in error_lower for kw in ["not found", "missing", "doesn't exist"]):
            return "not_found"
        elif any(kw in error_lower for kw in ["invalid", "malformed", "format"]):
            return "invalid_input"
        elif any(kw in error_lower for kw in ["connection", "network", "unreachable"]):
            return "network"
        elif any(kw in error_lower for kw in ["resource", "memory", "disk"]):
            return "resource"
        else:
            return "execution_error"

    def _identify_root_cause(self, branch: SolutionBranch) -> str:
        """Identify the root cause of failure."""
        error_type = self._classify_error(branch.failure_reason)

        root_causes = {
            "timeout": "Operation took longer than expected, possibly due to system load or complex processing",
            "permission": "Insufficient permissions to perform the requested action",
            "not_found": "Required resource or element was not available at expected location",
            "invalid_input": "Input data format or content was not compatible with the operation",
            "network": "Network connectivity issue prevented communication with required service",
            "resource": "System resources (memory, disk, CPU) were insufficient for the operation",
            "execution_error": "An unexpected error occurred during execution"
        }

        return root_causes.get(error_type, "Unknown root cause")

    def _generate_alternatives(self, branch: SolutionBranch) -> List[str]:
        """Generate alternative approaches based on failure."""
        error_type = self._classify_error(branch.failure_reason)

        alternatives = {
            "timeout": [
                "Break the task into smaller chunks",
                "Use a more efficient algorithm",
                "Increase timeout and retry",
                "Process asynchronously with progress updates"
            ],
            "permission": [
                "Request elevated permissions",
                "Use an alternative method that doesn't require this permission",
                "Work around by using available tools"
            ],
            "not_found": [
                "Search for the element using different criteria",
                "Verify the element exists before attempting action",
                "Use fuzzy matching to find similar elements",
                "Check if the UI has changed and re-analyze"
            ],
            "invalid_input": [
                "Validate and transform input before processing",
                "Use a more flexible parser",
                "Request clarification from user"
            ],
            "network": [
                "Retry with exponential backoff",
                "Use cached data if available",
                "Switch to offline alternative",
                "Check connectivity and wait for recovery"
            ],
            "resource": [
                "Free up resources and retry",
                "Use a lighter-weight approach",
                "Process in batches to reduce peak resource usage"
            ],
            "execution_error": [
                "Try a completely different approach",
                "Break down into simpler steps",
                "Verify preconditions before execution",
                "Use fallback method"
            ]
        }

        return alternatives.get(error_type, ["Try a different approach"])

    def _estimate_recovery_probability(self, branch: SolutionBranch) -> float:
        """Estimate probability of recovery with alternative approach."""
        # Check history of similar failures
        error_type = self._classify_error(branch.failure_reason)
        similar_failures = self._failure_patterns.get(error_type, [])

        if not similar_failures:
            return 0.5  # No history, use default

        # Calculate success rate after similar failures
        # This is a simplified heuristic
        base_probability = 0.7  # Base recovery probability

        # Reduce probability based on retry count
        retry_penalty = branch.retry_count * 0.15

        # Reduce if we've seen many similar failures
        frequency_penalty = min(len(similar_failures) * 0.05, 0.3)

        return max(0.1, base_probability - retry_penalty - frequency_penalty)

    def _calculate_learning_value(self, branch: SolutionBranch) -> float:
        """Calculate how much we can learn from this failure."""
        error_type = self._classify_error(branch.failure_reason)
        similar_count = len(self._failure_patterns.get(error_type, []))

        # First few failures of a type are most valuable
        if similar_count < 3:
            return 0.9
        elif similar_count < 10:
            return 0.6
        else:
            return 0.3

    def _prune_branches(self) -> None:
        """Remove lowest value branches to make room."""
        # Sort failed branches by priority and learning value
        failed_list = [
            (bid, self._branches[bid])
            for bid in self._failed_branches
            if bid in self._branches
        ]

        if not failed_list:
            return

        # Sort by priority (lower = less important)
        failed_list.sort(key=lambda x: x[1].priority)

        # Remove up to 3 lowest priority failed branches
        for bid, _ in failed_list[:3]:
            del self._branches[bid]
            self._failed_branches.discard(bid)

    def get_best_pending_branches(self, count: int = 3) -> List[SolutionBranch]:
        """Get the best pending branches to try next."""
        pending = [
            b for b in self._branches.values()
            if b.status == BranchStatus.PENDING
            and b.confidence >= self.min_confidence_threshold
        ]

        # Sort by confidence * priority (higher = better)
        pending.sort(key=lambda b: b.confidence * b.priority, reverse=True)

        return pending[:count]

    def generate_recovery_branches(
        self,
        failed_branch: SolutionBranch,
        problem_context: Dict[str, Any]
    ) -> List[SolutionBranch]:
        """Generate new branches based on failure analysis."""
        analysis = self._analyze_failure(failed_branch)
        new_branches = []

        for i, alternative in enumerate(analysis.suggested_alternatives[:3]):
            confidence = analysis.recovery_probability * (1 - i * 0.1)

            branch = self.create_branch(
                description=f"Alternative after failure: {alternative}",
                approach=alternative,
                priority=failed_branch.priority + 0.1,  # Slightly higher priority
                confidence=confidence,
                parent_id=failed_branch.branch_id
            )
            new_branches.append(branch)

        return new_branches

    def get_stats(self) -> Dict[str, Any]:
        """Get branch manager statistics."""
        return {
            "total_branches": len(self._branches),
            "active_branches": len(self._active_branches),
            "completed_branches": len(self._completed_branches),
            "failed_branches": len(self._failed_branches),
            "success_rate": (
                len(self._completed_branches) /
                (len(self._completed_branches) + len(self._failed_branches))
                if (self._completed_branches or self._failed_branches) else 0
            ),
            "failure_patterns": {k: len(v) for k, v in self._failure_patterns.items()}
        }


# ============================================================================
# Reasoning Graph State
# ============================================================================

class ReasoningGraphState(BaseModel):
    """State for the reasoning graph."""
    # Identity
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str = Field(default_factory=lambda: str(uuid4()))

    # Input
    query: str = ""
    context: Dict[str, Any] = Field(default_factory=dict)
    constraints: Dict[str, Any] = Field(default_factory=dict)

    # Problem Analysis
    problem_type: str = ""
    complexity_score: float = 0.0
    identified_challenges: List[str] = Field(default_factory=list)

    # Solution Branches
    branches: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    active_branch_ids: List[str] = Field(default_factory=list)
    successful_branch_ids: List[str] = Field(default_factory=list)
    failed_branch_ids: List[str] = Field(default_factory=list)
    current_branch_id: Optional[str] = None

    # Execution State
    phase: str = "initializing"
    iteration: int = 0
    max_iterations: int = 10
    total_attempts: int = 0
    max_attempts: int = 20

    # Results
    partial_results: List[Dict[str, Any]] = Field(default_factory=list)
    final_result: Optional[Dict[str, Any]] = None
    confidence: float = 0.0

    # Learning
    learning_insights: List[str] = Field(default_factory=list)
    failure_analyses: List[Dict[str, Any]] = Field(default_factory=list)

    # Narration
    narration_log: List[Dict[str, Any]] = Field(default_factory=list)
    should_narrate: bool = True

    # Control
    should_continue: bool = True
    needs_human_intervention: bool = False
    intervention_reason: Optional[str] = None

    # Timestamps
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True


# ============================================================================
# Reasoning Graph Nodes
# ============================================================================

class BaseGraphNode(ABC):
    """Base class for graph nodes."""

    def __init__(self, name: str, narrator: Optional[VoiceNarrator] = None):
        self.name = name
        self.narrator = narrator
        self.logger = logging.getLogger(f"{__name__}.{name}")

    @abstractmethod
    async def process(self, state: ReasoningGraphState) -> ReasoningGraphState:
        """Process the state."""
        pass

    async def narrate(
        self,
        event: ReasoningEvent,
        state: ReasoningGraphState,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Narrate an event."""
        if self.narrator and state.should_narrate:
            ctx = context or {}
            await self.narrator.narrate(event, ctx)
            state.narration_log.append({
                "event": event.value,
                "node": self.name,
                "context": ctx,
                "timestamp": datetime.utcnow().isoformat()
            })


class ProblemAnalysisNode(BaseGraphNode):
    """Analyzes the problem and generates initial solution branches."""

    def __init__(
        self,
        branch_manager: SolutionBranchManager,
        narrator: Optional[VoiceNarrator] = None
    ):
        super().__init__("problem_analysis", narrator)
        self.branch_manager = branch_manager

    async def process(self, state: ReasoningGraphState) -> ReasoningGraphState:
        start_time = time.time()
        state.phase = "analyzing"

        # Analyze problem
        analysis = await self._analyze_problem(state)
        state.problem_type = analysis["type"]
        state.complexity_score = analysis["complexity"]
        state.identified_challenges = analysis["challenges"]

        # Generate initial solution branches
        branches = self._generate_initial_branches(state, analysis)

        await self.narrate(
            ReasoningEvent.ANALYZING,
            state,
            {"count": len(branches)}
        )

        # Register branches
        for branch in branches:
            state.branches[branch.branch_id] = branch.to_dict()

        state.phase = "branching"
        return state

    async def _analyze_problem(self, state: ReasoningGraphState) -> Dict[str, Any]:
        """Analyze the problem to understand its nature."""
        query = state.query.lower()
        context = state.context

        # Classify problem type
        if any(kw in query for kw in ["error", "fix", "broken", "not working", "failing"]):
            problem_type = "error_resolution"
        elif any(kw in query for kw in ["find", "locate", "where", "search"]):
            problem_type = "search"
        elif any(kw in query for kw in ["create", "make", "build", "add"]):
            problem_type = "creation"
        elif any(kw in query for kw in ["change", "update", "modify", "edit"]):
            problem_type = "modification"
        elif any(kw in query for kw in ["explain", "how", "why", "what"]):
            problem_type = "information"
        else:
            problem_type = "general_action"

        # Assess complexity
        complexity = 0.3  # Base complexity

        # More words = more complex
        complexity += min(0.3, len(query.split()) * 0.01)

        # Multiple steps mentioned
        if " and " in query or " then " in query:
            complexity += 0.2

        # Technical terms increase complexity
        technical_terms = ["api", "database", "server", "config", "deploy", "compile"]
        complexity += 0.1 * sum(1 for t in technical_terms if t in query)

        complexity = min(1.0, complexity)

        # Identify challenges
        challenges = []
        if problem_type == "error_resolution":
            challenges.append("Need to identify root cause")
            challenges.append("May require multiple attempts to fix")
        if complexity > 0.7:
            challenges.append("High complexity - may need to break into subtasks")
        if context.get("time_sensitive"):
            challenges.append("Time constraint - prioritize quick solutions")

        return {
            "type": problem_type,
            "complexity": complexity,
            "challenges": challenges,
            "keywords": self._extract_keywords(query)
        }

    def _extract_keywords(self, query: str) -> List[str]:
        """Extract key terms from query."""
        # Simple keyword extraction - could be enhanced with NLP
        stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                      "being", "have", "has", "had", "do", "does", "did", "will",
                      "would", "could", "should", "may", "might", "must", "shall",
                      "can", "need", "to", "of", "in", "for", "on", "with", "at",
                      "by", "from", "as", "into", "through", "during", "before",
                      "after", "above", "below", "between", "under", "again", "i",
                      "my", "me", "we", "our", "you", "your", "it", "its", "this",
                      "that", "these", "those", "and", "or", "but", "if", "then"}

        words = query.lower().split()
        keywords = [w for w in words if w not in stop_words and len(w) > 2]
        return keywords[:10]

    def _generate_initial_branches(
        self,
        state: ReasoningGraphState,
        analysis: Dict[str, Any]
    ) -> List[SolutionBranch]:
        """Generate initial solution branches based on analysis."""
        branches = []
        problem_type = analysis["type"]

        # Strategy templates by problem type
        strategies = {
            "error_resolution": [
                ("Direct fix based on error message", "direct_fix", 0.7),
                ("Analyze logs and stack trace", "log_analysis", 0.6),
                ("Try common fixes for this error type", "common_fixes", 0.5),
            ],
            "search": [
                ("Direct search with given criteria", "direct_search", 0.8),
                ("Fuzzy search with relaxed criteria", "fuzzy_search", 0.5),
                ("Search with alternative keywords", "alternative_search", 0.4),
            ],
            "creation": [
                ("Create using standard approach", "standard_creation", 0.7),
                ("Create using template", "template_creation", 0.6),
                ("Create incrementally with verification", "incremental_creation", 0.5),
            ],
            "modification": [
                ("Direct modification", "direct_modification", 0.7),
                ("Safe modification with backup", "safe_modification", 0.6),
                ("Incremental modification with checkpoints", "incremental_modification", 0.5),
            ],
            "information": [
                ("Direct lookup", "direct_lookup", 0.8),
                ("Search documentation", "doc_search", 0.6),
                ("Analyze context for answer", "context_analysis", 0.5),
            ],
            "general_action": [
                ("Execute directly", "direct_execution", 0.6),
                ("Execute with precondition checks", "safe_execution", 0.5),
                ("Break down and execute stepwise", "stepwise_execution", 0.4),
            ]
        }

        for description, approach, confidence in strategies.get(problem_type, strategies["general_action"]):
            # Adjust confidence based on complexity
            adjusted_confidence = confidence * (1.0 - analysis["complexity"] * 0.3)

            branch = self.branch_manager.create_branch(
                description=f"{description} for: {state.query[:50]}...",
                approach=approach,
                priority=adjusted_confidence,
                confidence=adjusted_confidence
            )
            branches.append(branch)

        return branches


class BranchExecutionNode(BaseGraphNode):
    """Executes solution branches."""

    def __init__(
        self,
        branch_manager: SolutionBranchManager,
        tool_orchestrator: Optional[Any] = None,
        narrator: Optional[VoiceNarrator] = None
    ):
        super().__init__("branch_execution", narrator)
        self.branch_manager = branch_manager
        self.tool_orchestrator = tool_orchestrator

    async def process(self, state: ReasoningGraphState) -> ReasoningGraphState:
        state.phase = "executing"

        # Get best pending branches
        pending_branches = self.branch_manager.get_best_pending_branches(
            count=self.branch_manager.max_parallel_branches
        )

        if not pending_branches:
            state.should_continue = False
            if not state.successful_branch_ids:
                state.needs_human_intervention = True
                state.intervention_reason = "No more solution branches available"
            return state

        # Execute branches (could be parallel or sequential based on config)
        for branch in pending_branches:
            state.current_branch_id = branch.branch_id
            state.total_attempts += 1

            if state.total_attempts > state.max_attempts:
                state.should_continue = False
                state.needs_human_intervention = True
                state.intervention_reason = f"Exceeded maximum attempts ({state.max_attempts})"
                break

            await self.narrate(
                ReasoningEvent.BRANCHING,
                state,
                {
                    "approach": branch.approach,
                    "confidence": int(branch.confidence * 100),
                    "branch_id": branch.branch_id
                }
            )

            # Activate and execute
            self.branch_manager.activate_branch(branch.branch_id)
            result = await self._execute_branch(branch, state)

            # Update state based on result
            if result["success"]:
                self.branch_manager.complete_branch(
                    branch.branch_id,
                    success=True,
                    results=[result]
                )
                state.successful_branch_ids.append(branch.branch_id)
                state.partial_results.append(result)
                state.branches[branch.branch_id] = self.branch_manager._branches[branch.branch_id].to_dict()

                await self.narrate(
                    ReasoningEvent.SUCCESS,
                    state,
                    {
                        "branch_id": branch.branch_id,
                        "description": result.get("description", "Task completed")
                    }
                )

                # Check if we have enough successful results
                if len(state.successful_branch_ids) >= 1:
                    state.confidence = max(result.get("confidence", 0.7), state.confidence)
                    break
            else:
                self.branch_manager.complete_branch(
                    branch.branch_id,
                    success=False,
                    failure_reason=result.get("error", "Unknown failure")
                )
                state.failed_branch_ids.append(branch.branch_id)
                state.branches[branch.branch_id] = self.branch_manager._branches[branch.branch_id].to_dict()

                await self.narrate(
                    ReasoningEvent.FAILED,
                    state,
                    {
                        "branch_id": branch.branch_id,
                        "reason": result.get("error", "Unknown")[:100]
                    }
                )

                # Generate recovery branches
                failed_branch = self.branch_manager._branches[branch.branch_id]
                recovery_branches = self.branch_manager.generate_recovery_branches(
                    failed_branch,
                    {"query": state.query, "context": state.context}
                )

                await self.narrate(
                    ReasoningEvent.RECOVERING,
                    state,
                    {"count": len(recovery_branches)}
                )

                for rb in recovery_branches:
                    state.branches[rb.branch_id] = rb.to_dict()

        state.iteration += 1
        return state

    async def _execute_branch(
        self,
        branch: SolutionBranch,
        state: ReasoningGraphState
    ) -> Dict[str, Any]:
        """Execute a single solution branch."""
        start_time = time.time()

        try:
            # If we have a tool orchestrator, use it
            if self.tool_orchestrator:
                result = await self.tool_orchestrator.execute(
                    action_type=branch.approach,
                    target=state.query,
                    parameters={"context": state.context}
                )

                return {
                    "success": result is not None,
                    "data": result,
                    "description": f"Executed {branch.approach}",
                    "confidence": branch.confidence,
                    "duration_ms": (time.time() - start_time) * 1000
                }
            else:
                # Simulate execution for testing
                await asyncio.sleep(0.1)

                # Simulate some failures for demonstration
                import random
                if random.random() < 0.3:  # 30% failure rate for simulation
                    return {
                        "success": False,
                        "error": "Simulated failure for testing",
                        "duration_ms": (time.time() - start_time) * 1000
                    }

                return {
                    "success": True,
                    "data": {"simulated": True, "approach": branch.approach},
                    "description": f"Successfully executed {branch.approach}",
                    "confidence": branch.confidence,
                    "duration_ms": (time.time() - start_time) * 1000
                }

        except Exception as e:
            self.logger.error(f"Branch execution failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "duration_ms": (time.time() - start_time) * 1000
            }


class ResultSynthesisNode(BaseGraphNode):
    """Synthesizes results from successful branches."""

    def __init__(
        self,
        branch_manager: SolutionBranchManager,
        narrator: Optional[VoiceNarrator] = None
    ):
        super().__init__("result_synthesis", narrator)
        self.branch_manager = branch_manager

    async def process(self, state: ReasoningGraphState) -> ReasoningGraphState:
        state.phase = "synthesizing"

        if state.successful_branch_ids:
            await self.narrate(
                ReasoningEvent.SYNTHESIZING,
                state,
                {"count": len(state.partial_results)}
            )

            # Combine results
            state.final_result = self._synthesize_results(state)
            state.confidence = state.final_result.get("confidence", 0.7)
        else:
            state.final_result = {
                "success": False,
                "message": "Unable to find a working solution",
                "attempts": state.total_attempts,
                "learning_insights": state.learning_insights
            }
            state.confidence = 0.0

        # Collect learning insights
        for bid in state.failed_branch_ids:
            if bid in self.branch_manager._branches:
                branch = self.branch_manager._branches[bid]
                state.learning_insights.extend(branch.learning_insights)

        await self.narrate(
            ReasoningEvent.LEARNING,
            state,
            {"insight": state.learning_insights[0] if state.learning_insights else "Process complete"}
        )

        state.completed_at = datetime.utcnow().isoformat()
        state.phase = "complete"

        return state

    def _synthesize_results(self, state: ReasoningGraphState) -> Dict[str, Any]:
        """Synthesize results from multiple successful branches."""
        if not state.partial_results:
            return {"success": False, "message": "No results to synthesize"}

        if len(state.partial_results) == 1:
            result = state.partial_results[0]
            return {
                "success": True,
                "data": result.get("data"),
                "description": result.get("description"),
                "confidence": result.get("confidence", 0.7),
                "branches_used": 1,
                "total_attempts": state.total_attempts
            }

        # Multiple results - combine intelligently
        best_result = max(state.partial_results, key=lambda r: r.get("confidence", 0))

        combined_data = {
            "primary_result": best_result.get("data"),
            "alternative_results": [
                r.get("data") for r in state.partial_results if r != best_result
            ]
        }

        avg_confidence = sum(r.get("confidence", 0.5) for r in state.partial_results) / len(state.partial_results)

        return {
            "success": True,
            "data": combined_data,
            "description": f"Synthesized from {len(state.partial_results)} successful approaches",
            "confidence": min(0.95, avg_confidence + 0.1),  # Boost for multiple successes
            "branches_used": len(state.partial_results),
            "total_attempts": state.total_attempts
        }


# ============================================================================
# Router Functions
# ============================================================================

def route_after_analysis(state: ReasoningGraphState) -> str:
    """Route after problem analysis."""
    if not state.branches:
        return "synthesis"  # No branches generated - go to synthesis to report
    return "execution"


def route_after_execution(state: ReasoningGraphState) -> str:
    """Route after branch execution."""
    if not state.should_continue:
        return "synthesis"

    if state.successful_branch_ids:
        return "synthesis"

    if state.iteration >= state.max_iterations:
        return "synthesis"

    if state.needs_human_intervention:
        return "synthesis"

    # More pending branches? Continue execution
    pending_count = sum(
        1 for b in state.branches.values()
        if b.get("status") == "pending"
    )

    if pending_count > 0:
        return "execution"

    return "synthesis"


# ============================================================================
# Main Reasoning Graph Engine
# ============================================================================

class ReasoningGraphEngine:
    """
    Main engine for multi-branch reasoning with dynamic recovery.

    This engine provides:
    - Multi-branch solution exploration
    - Automatic failure recovery
    - Real-time voice narration
    - Learning from outcomes
    """

    def __init__(
        self,
        tool_orchestrator: Optional[Any] = None,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        learning_callback: Optional[Callable[[Dict], Awaitable[None]]] = None,
        narration_style: NarrationStyle = NarrationStyle.DETAILED,
        max_parallel_branches: int = 3,
        max_total_branches: int = 10,
        enable_checkpointing: bool = True
    ):
        self.tool_orchestrator = tool_orchestrator
        self.enable_checkpointing = enable_checkpointing

        # Initialize components
        self.narrator = VoiceNarrator(
            tts_callback=tts_callback,
            style=narration_style,
            enabled=tts_callback is not None
        )

        self.branch_manager = SolutionBranchManager(
            max_parallel_branches=max_parallel_branches,
            max_total_branches=max_total_branches,
            learning_callback=learning_callback
        )

        # Initialize nodes
        self._init_nodes()

        # Build graph
        self.logger = logging.getLogger(__name__)

        self.graph = self._build_graph()
        self.compiled_graph = self._compile_graph()

    def _init_nodes(self) -> None:
        """Initialize graph nodes."""
        self.analysis_node = ProblemAnalysisNode(
            self.branch_manager,
            self.narrator
        )
        self.execution_node = BranchExecutionNode(
            self.branch_manager,
            self.tool_orchestrator,
            self.narrator
        )
        self.synthesis_node = ResultSynthesisNode(
            self.branch_manager,
            self.narrator
        )

    def _build_graph(self) -> Optional[StateGraph]:
        """Build the LangGraph state graph."""
        if not LANGGRAPH_AVAILABLE:
            from backend.core.optional_dependencies import note_missing
            note_missing("langgraph", "reasoning graph (fallback)", install="langgraph")
            return None

        graph = StateGraph(ReasoningGraphState)

        # Add nodes
        graph.add_node("analysis", self._wrap_node(self.analysis_node))
        graph.add_node("execution", self._wrap_node(self.execution_node))
        graph.add_node("synthesis", self._wrap_node(self.synthesis_node))

        # Set entry point
        graph.set_entry_point("analysis")

        # Add edges
        graph.add_conditional_edges(
            "analysis",
            route_after_analysis,
            {"execution": "execution", "synthesis": "synthesis"}
        )

        graph.add_conditional_edges(
            "execution",
            route_after_execution,
            {"execution": "execution", "synthesis": "synthesis"}
        )

        graph.add_edge("synthesis", END)

        return graph

    def _compile_graph(self):
        """Compile the graph."""
        if self.graph is None:
            return None

        compile_kwargs = {}
        if self.enable_checkpointing and LANGGRAPH_AVAILABLE:
            compile_kwargs["checkpointer"] = MemorySaver()

        return self.graph.compile(**compile_kwargs)

    def _wrap_node(self, node: BaseGraphNode):
        """Wrap node for LangGraph."""
        async def wrapped(state: ReasoningGraphState) -> ReasoningGraphState:
            return await node.process(state)
        return wrapped

    async def reason(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        constraints: Optional[Dict[str, Any]] = None,
        narrate: bool = True
    ) -> Dict[str, Any]:
        """
        Perform multi-branch reasoning on a query.

        Args:
            query: The problem/task to solve
            context: Additional context information
            constraints: Execution constraints
            narrate: Whether to enable voice narration

        Returns:
            Reasoning result with solution, confidence, and insights
        """
        # Initialize state
        initial_state = ReasoningGraphState(
            query=query,
            context=context or {},
            constraints=constraints or {},
            should_narrate=narrate,
            started_at=datetime.utcnow().isoformat()
        )

        # Reset branch manager for new session
        self.branch_manager._branches.clear()
        self.branch_manager._active_branches.clear()
        self.branch_manager._completed_branches.clear()
        self.branch_manager._failed_branches.clear()

        # Run graph
        if self.compiled_graph:
            try:
                config = {"configurable": {"thread_id": initial_state.session_id}}
                final_state = await self.compiled_graph.ainvoke(initial_state, config=config)
            except Exception as e:
                self.logger.error(f"Graph execution failed: {e}")
                final_state = await self._fallback_execution(initial_state)
        else:
            final_state = await self._fallback_execution(initial_state)

        return {
            "session_id": final_state.session_id,
            "query": query,
            "result": final_state.final_result,
            "confidence": final_state.confidence,
            "total_attempts": final_state.total_attempts,
            "successful_branches": len(final_state.successful_branch_ids),
            "failed_branches": len(final_state.failed_branch_ids),
            "learning_insights": final_state.learning_insights,
            "needs_intervention": final_state.needs_human_intervention,
            "intervention_reason": final_state.intervention_reason,
            "narration_log": final_state.narration_log,
            "branch_stats": self.branch_manager.get_stats()
        }

    async def _fallback_execution(
        self,
        state: ReasoningGraphState
    ) -> ReasoningGraphState:
        """Fallback execution when LangGraph unavailable."""
        state = await self.analysis_node.process(state)

        while state.should_continue and state.iteration < state.max_iterations:
            state = await self.execution_node.process(state)

            if state.successful_branch_ids or state.needs_human_intervention:
                break

        state = await self.synthesis_node.process(state)
        return state

    def set_narration_style(self, style: NarrationStyle) -> None:
        """Change the narration style."""
        self.narrator.set_style(style)

    def get_narration_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get narration history."""
        return self.narrator.get_history(limit)


# ============================================================================
# Factory Functions
# ============================================================================

def create_reasoning_graph_engine(
    tool_orchestrator: Optional[Any] = None,
    tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    **kwargs
) -> ReasoningGraphEngine:
    """
    Create a configured reasoning graph engine.

    Args:
        tool_orchestrator: Tool orchestrator for execution
        tts_callback: Callback for text-to-speech narration
        **kwargs: Additional configuration

    Returns:
        Configured ReasoningGraphEngine
    """
    return ReasoningGraphEngine(
        tool_orchestrator=tool_orchestrator,
        tts_callback=tts_callback,
        **kwargs
    )


async def quick_reason(
    query: str,
    context: Optional[Dict] = None,
    narrate: bool = False
) -> Dict[str, Any]:
    """
    Quick reasoning helper.

    Args:
        query: Problem to solve
        context: Additional context
        narrate: Enable narration

    Returns:
        Reasoning result
    """
    engine = create_reasoning_graph_engine()
    return await engine.reason(query, context, narrate=narrate)


# ============================================================================
# Global Instance
# ============================================================================

_default_engine: Optional[ReasoningGraphEngine] = None


def get_reasoning_graph_engine() -> ReasoningGraphEngine:
    """Get or create global reasoning graph engine."""
    global _default_engine
    if _default_engine is None:
        _default_engine = create_reasoning_graph_engine()
    return _default_engine
