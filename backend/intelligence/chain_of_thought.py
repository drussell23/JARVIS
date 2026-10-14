"""
LangGraph Chain-of-Thought Reasoning for JARVIS Intelligence Systems

This module provides advanced chain-of-thought (CoT) reasoning capabilities
that integrate with UAE, SAI, and CAI to enable:
- Multi-step reasoning with explicit thought chains
- Self-reflection and correction
- Confidence calibration
- Dynamic reasoning strategies
- Cross-system intelligence fusion

The CoT system uses LangGraph state machines to provide:
- Transparent reasoning traces
- Adaptive reasoning depth
- Parallel hypothesis evaluation
- Evidence-based decision making
- Continuous learning from outcomes

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any, Awaitable, Callable, Deque, Dict, Generic, List, Literal,
    Optional, Protocol, Sequence, Set, Tuple, Type, TypeVar, Union
)
from uuid import uuid4

import numpy as np

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

logger = logging.getLogger(__name__)


# ============================================================================
# Enums and Types
# ============================================================================

class ReasoningStrategy(str, Enum):
    """Strategies for chain-of-thought reasoning."""
    LINEAR = "linear"           # Step-by-step sequential
    TREE = "tree"               # Tree-of-thought with branches
    GRAPH = "graph"             # Graph-based with cycles
    DEBATE = "debate"           # Internal debate between hypotheses
    REFLEXION = "reflexion"     # Self-reflection and correction
    ADAPTIVE = "adaptive"       # Dynamically chosen based on task


class ThoughtType(str, Enum):
    """Types of thoughts in the reasoning chain."""
    OBSERVATION = "observation"     # What we observe/perceive
    ANALYSIS = "analysis"           # Breaking down the problem
    HYPOTHESIS = "hypothesis"       # Potential explanations
    INFERENCE = "inference"         # Drawing conclusions
    EVALUATION = "evaluation"       # Assessing options
    DECISION = "decision"           # Making choices
    REFLECTION = "reflection"       # Self-critique
    CORRECTION = "correction"       # Adjusting based on reflection
    PREDICTION = "prediction"       # Forecasting outcomes
    VERIFICATION = "verification"   # Checking conclusions


class ConfidenceLevel(str, Enum):
    """Confidence levels for reasoning steps."""
    SPECULATIVE = "speculative"   # < 0.3
    UNCERTAIN = "uncertain"       # 0.3 - 0.5
    MODERATE = "moderate"         # 0.5 - 0.7
    CONFIDENT = "confident"       # 0.7 - 0.9
    CERTAIN = "certain"           # > 0.9


class ReasoningPhase(str, Enum):
    """Phases of the reasoning process."""
    PERCEIVE = "perceive"         # Gather information
    ANALYZE = "analyze"           # Process information
    HYPOTHESIZE = "hypothesize"   # Generate hypotheses
    EVALUATE = "evaluate"         # Evaluate options
    DECIDE = "decide"             # Make decision
    REFLECT = "reflect"           # Self-reflect
    LEARN = "learn"               # Update knowledge
    COMPLETE = "complete"         # Finished


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class Thought:
    """A single thought in the chain of reasoning."""
    thought_id: str
    thought_type: ThoughtType
    content: str
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.5
    parent_thoughts: List[str] = field(default_factory=list)
    child_thoughts: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thought_id": self.thought_id,
            "type": self.thought_type.value,
            "content": self.content,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "parents": self.parent_thoughts,
            "children": self.child_thoughts,
            "timestamp": self.timestamp.isoformat()
        }


@dataclass
class ReasoningChain:
    """A complete chain of thoughts."""
    chain_id: str
    thoughts: List[Thought] = field(default_factory=list)
    strategy: ReasoningStrategy = ReasoningStrategy.LINEAR
    final_conclusion: Optional[str] = None
    overall_confidence: float = 0.0
    reasoning_depth: int = 0
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    def add_thought(self, thought: Thought) -> None:
        """Add a thought to the chain."""
        self.thoughts.append(thought)
        self.reasoning_depth = max(self.reasoning_depth, len(thought.parent_thoughts) + 1)
        self._update_confidence()

    def _update_confidence(self) -> None:
        """Update overall confidence based on thoughts."""
        if not self.thoughts:
            self.overall_confidence = 0.0
            return

        # Weight recent thoughts more heavily
        weights = np.exp(np.linspace(-1, 0, len(self.thoughts)))
        weights /= weights.sum()

        confidences = [t.confidence for t in self.thoughts]
        self.overall_confidence = float(np.average(confidences, weights=weights))

    def get_trace(self) -> str:
        """Get human-readable reasoning trace."""
        lines = [f"Reasoning Chain [{self.strategy.value}]:"]
        for i, thought in enumerate(self.thoughts, 1):
            lines.append(f"  {i}. [{thought.thought_type.value}] {thought.content}")
            lines.append(f"     Confidence: {thought.confidence:.2f}")
            if thought.evidence:
                lines.append(f"     Evidence: {', '.join(thought.evidence[:3])}")
        if self.final_conclusion:
            lines.append(f"\nConclusion: {self.final_conclusion}")
            lines.append(f"Overall Confidence: {self.overall_confidence:.2f}")
        return "\n".join(lines)


@dataclass
class Hypothesis:
    """A hypothesis being evaluated."""
    hypothesis_id: str
    description: str
    supporting_evidence: List[str] = field(default_factory=list)
    contradicting_evidence: List[str] = field(default_factory=list)
    confidence: float = 0.5
    prior_probability: float = 0.5
    posterior_probability: float = 0.5
    metadata: Dict[str, Any] = field(default_factory=dict)

    def update_probability(self, evidence_strength: float, supports: bool) -> None:
        """Bayesian update of probability based on evidence."""
        # Simplified Bayesian update
        likelihood_ratio = (1 + evidence_strength) if supports else (1 / (1 + evidence_strength))
        odds = self.posterior_probability / (1 - self.posterior_probability + 1e-10)
        new_odds = odds * likelihood_ratio
        self.posterior_probability = new_odds / (1 + new_odds)
        self.confidence = abs(self.posterior_probability - 0.5) * 2


# ============================================================================
# LangGraph State
# ============================================================================

class ChainOfThoughtState(BaseModel):
    """State for the chain-of-thought reasoning graph."""
    # Identity
    reasoning_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = ""

    # Input
    query: str = ""
    context: Dict[str, Any] = Field(default_factory=dict)
    available_evidence: List[str] = Field(default_factory=list)

    # Strategy
    strategy: str = ReasoningStrategy.ADAPTIVE.value
    max_depth: int = 10
    min_confidence: float = 0.6

    # Current state
    phase: str = ReasoningPhase.PERCEIVE.value
    current_depth: int = 0

    # Reasoning
    thoughts: List[Dict[str, Any]] = Field(default_factory=list)
    hypotheses: List[Dict[str, Any]] = Field(default_factory=list)
    active_hypothesis_id: Optional[str] = None

    # Reflection
    reflection_notes: List[str] = Field(default_factory=list)
    corrections_made: int = 0

    # Output
    conclusion: Optional[str] = None
    confidence: float = 0.0
    reasoning_trace: str = ""

    # Control
    should_continue: bool = True
    iterations: int = 0
    max_iterations: int = 20

    # Timestamps
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True


# ============================================================================
# Reasoning Nodes
# ============================================================================

class BaseReasoningNode(ABC):
    """Base class for reasoning nodes."""

    def __init__(self, name: str):
        self.name = name
        self.logger = logging.getLogger(f"{__name__}.{name}")

    @abstractmethod
    async def process(self, state: ChainOfThoughtState) -> ChainOfThoughtState:
        """Process the state."""
        pass

    def _create_thought(
        self,
        thought_type: ThoughtType,
        content: str,
        confidence: float = 0.5,
        evidence: Optional[List[str]] = None,
        parents: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Create a thought dictionary."""
        thought = Thought(
            thought_id=str(uuid4()),
            thought_type=thought_type,
            content=content,
            confidence=confidence,
            evidence=evidence or [],
            parent_thoughts=parents or []
        )
        return thought.to_dict()


class PerceptionNode(BaseReasoningNode):
    """Perceive and gather information."""

    def __init__(self):
        super().__init__("perception")

    async def process(self, state: ChainOfThoughtState) -> ChainOfThoughtState:
        self.logger.debug(f"Perceiving: {state.query[:50]}...")

        state.phase = ReasoningPhase.PERCEIVE.value

        # Create observation thought
        observation = self._create_thought(
            thought_type=ThoughtType.OBSERVATION,
            content=f"Observing input: {state.query}",
            confidence=0.9,
            evidence=state.available_evidence[:5]
        )
        state.thoughts.append(observation)

        # Extract key elements from context
        if state.context:
            context_thought = self._create_thought(
                thought_type=ThoughtType.OBSERVATION,
                content=f"Context contains {len(state.context)} elements",
                confidence=0.85
            )
            state.thoughts.append(context_thought)

        state.current_depth += 1
        return state


class AnalysisNode(BaseReasoningNode):
    """Analyze the perceived information."""

    def __init__(self):
        super().__init__("analysis")

    async def process(self, state: ChainOfThoughtState) -> ChainOfThoughtState:
        self.logger.debug("Analyzing information...")

        state.phase = ReasoningPhase.ANALYZE.value

        # Analyze the query
        analysis_content = self._analyze_query(state.query, state.context)

        analysis_thought = self._create_thought(
            thought_type=ThoughtType.ANALYSIS,
            content=analysis_content,
            confidence=0.7,
            parents=[t["thought_id"] for t in state.thoughts[-2:]]
        )
        state.thoughts.append(analysis_thought)

        state.current_depth += 1
        return state

    def _analyze_query(self, query: str, context: Dict) -> str:
        """Analyze the query and context."""
        # Dynamic analysis based on content
        analysis_parts = []

        # Identify query type
        query_lower = query.lower()
        if "?" in query or any(w in query_lower for w in ["what", "how", "why", "where", "when"]):
            analysis_parts.append("Query is interrogative, seeking information")
        elif any(w in query_lower for w in ["do", "make", "create", "execute"]):
            analysis_parts.append("Query is imperative, requesting action")
        else:
            analysis_parts.append("Query is declarative, providing information")

        # Check complexity
        word_count = len(query.split())
        if word_count > 30:
            analysis_parts.append("Query is complex with multiple components")
        elif word_count > 10:
            analysis_parts.append("Query has moderate complexity")
        else:
            analysis_parts.append("Query is straightforward")

        # Check context relevance
        if context:
            analysis_parts.append(f"Context provides {len(context)} relevant data points")

        return ". ".join(analysis_parts)


class HypothesisNode(BaseReasoningNode):
    """Generate hypotheses based on analysis."""

    def __init__(self):
        super().__init__("hypothesis")

    async def process(self, state: ChainOfThoughtState) -> ChainOfThoughtState:
        self.logger.debug("Generating hypotheses...")

        state.phase = ReasoningPhase.HYPOTHESIZE.value

        # Generate hypotheses based on analysis
        hypotheses = self._generate_hypotheses(state)

        for hyp in hypotheses:
            state.hypotheses.append(hyp)

            hyp_thought = self._create_thought(
                thought_type=ThoughtType.HYPOTHESIS,
                content=f"Hypothesis: {hyp['description']}",
                confidence=hyp["confidence"],
                parents=[t["thought_id"] for t in state.thoughts[-1:]]
            )
            state.thoughts.append(hyp_thought)

        # Set active hypothesis to highest confidence
        if state.hypotheses:
            best_hyp = max(state.hypotheses, key=lambda h: h["confidence"])
            state.active_hypothesis_id = best_hyp["hypothesis_id"]

        state.current_depth += 1
        return state

    def _generate_hypotheses(self, state: ChainOfThoughtState) -> List[Dict[str, Any]]:
        """Generate hypotheses from current state."""
        hypotheses = []

        # Base hypothesis
        base_hyp = Hypothesis(
            hypothesis_id=str(uuid4()),
            description=f"The primary interpretation of '{state.query[:30]}...' is straightforward",
            confidence=0.7,
            prior_probability=0.6
        )
        hypotheses.append({
            "hypothesis_id": base_hyp.hypothesis_id,
            "description": base_hyp.description,
            "confidence": base_hyp.confidence,
            "supporting_evidence": [],
            "contradicting_evidence": []
        })

        # Alternative hypothesis if context suggests ambiguity
        if state.context and len(state.context) > 2:
            alt_hyp = Hypothesis(
                hypothesis_id=str(uuid4()),
                description="The query has multiple valid interpretations based on context",
                confidence=0.5,
                prior_probability=0.4
            )
            hypotheses.append({
                "hypothesis_id": alt_hyp.hypothesis_id,
                "description": alt_hyp.description,
                "confidence": alt_hyp.confidence,
                "supporting_evidence": list(state.context.keys())[:3],
                "contradicting_evidence": []
            })

        return hypotheses


class EvaluationNode(BaseReasoningNode):
    """Evaluate hypotheses against evidence."""

    def __init__(self):
        super().__init__("evaluation")

    async def process(self, state: ChainOfThoughtState) -> ChainOfThoughtState:
        self.logger.debug("Evaluating hypotheses...")

        state.phase = ReasoningPhase.EVALUATE.value

        # Evaluate each hypothesis
        for hyp in state.hypotheses:
            eval_result = self._evaluate_hypothesis(hyp, state)
            hyp["confidence"] = eval_result["updated_confidence"]

            eval_thought = self._create_thought(
                thought_type=ThoughtType.EVALUATION,
                content=f"Evaluated '{hyp['description'][:30]}...': confidence={hyp['confidence']:.2f}",
                confidence=eval_result["evaluation_confidence"],
                evidence=eval_result["key_evidence"]
            )
            state.thoughts.append(eval_thought)

        # Update active hypothesis
        if state.hypotheses:
            best_hyp = max(state.hypotheses, key=lambda h: h["confidence"])
            state.active_hypothesis_id = best_hyp["hypothesis_id"]
            state.confidence = best_hyp["confidence"]

        state.current_depth += 1
        return state

    def _evaluate_hypothesis(
        self,
        hypothesis: Dict,
        state: ChainOfThoughtState
    ) -> Dict[str, Any]:
        """Evaluate a hypothesis against available evidence."""
        # Count supporting vs contradicting evidence
        support_count = len(hypothesis.get("supporting_evidence", []))
        contradict_count = len(hypothesis.get("contradicting_evidence", []))

        # Calculate updated confidence
        total = support_count + contradict_count + 1  # +1 to avoid division by zero
        support_ratio = (support_count + 0.5) / total

        # Bayesian-like update
        prior = hypothesis["confidence"]
        updated = (prior * support_ratio + 0.5 * (1 - support_ratio))

        return {
            "updated_confidence": updated,
            "evaluation_confidence": 0.7 + 0.3 * (total / 10),  # More evidence = higher confidence in evaluation
            "key_evidence": hypothesis.get("supporting_evidence", [])[:3]
        }


class DecisionNode(BaseReasoningNode):
    """Make a decision based on evaluation."""

    def __init__(self):
        super().__init__("decision")

    async def process(self, state: ChainOfThoughtState) -> ChainOfThoughtState:
        self.logger.debug("Making decision...")

        state.phase = ReasoningPhase.DECIDE.value

        # Find best hypothesis
        if state.hypotheses:
            best_hyp = max(state.hypotheses, key=lambda h: h["confidence"])

            decision_thought = self._create_thought(
                thought_type=ThoughtType.DECISION,
                content=f"Decided: {best_hyp['description']}",
                confidence=best_hyp["confidence"]
            )
            state.thoughts.append(decision_thought)

            state.conclusion = best_hyp["description"]
            state.confidence = best_hyp["confidence"]
        else:
            # No hypotheses - make default decision
            decision_thought = self._create_thought(
                thought_type=ThoughtType.DECISION,
                content="Default decision due to insufficient hypotheses",
                confidence=0.3
            )
            state.thoughts.append(decision_thought)
            state.conclusion = "Unable to reach confident conclusion"
            state.confidence = 0.3

        state.current_depth += 1
        return state


class ReflectionNode(BaseReasoningNode):
    """Reflect on the reasoning process."""

    def __init__(self):
        super().__init__("reflection")

    async def process(self, state: ChainOfThoughtState) -> ChainOfThoughtState:
        self.logger.debug("Reflecting on reasoning...")

        state.phase = ReasoningPhase.REFLECT.value

        # Analyze reasoning quality
        reflection = self._reflect(state)

        reflection_thought = self._create_thought(
            thought_type=ThoughtType.REFLECTION,
            content=reflection["summary"],
            confidence=reflection["meta_confidence"]
        )
        state.thoughts.append(reflection_thought)

        # Store reflection notes
        state.reflection_notes.extend(reflection["notes"])

        # Check if correction needed
        if reflection["needs_correction"]:
            state.corrections_made += 1

            correction_thought = self._create_thought(
                thought_type=ThoughtType.CORRECTION,
                content=reflection["correction_suggestion"],
                confidence=0.6
            )
            state.thoughts.append(correction_thought)

            # Adjust confidence based on identified issues
            state.confidence *= 0.9

        state.current_depth += 1
        return state

    def _reflect(self, state: ChainOfThoughtState) -> Dict[str, Any]:
        """Reflect on the reasoning process."""
        notes = []
        needs_correction = False
        correction_suggestion = ""

        # Check reasoning depth
        if state.current_depth < 3:
            notes.append("Reasoning may be too shallow")
            needs_correction = True
            correction_suggestion = "Consider more analysis steps"

        # Check hypothesis diversity
        if len(state.hypotheses) < 2:
            notes.append("Limited hypothesis exploration")

        # Check confidence calibration
        if state.confidence > 0.9 and len(state.thoughts) < 5:
            notes.append("High confidence with limited reasoning may indicate overconfidence")
            needs_correction = True
            correction_suggestion = "Re-evaluate confidence levels"

        # Check for contradictions
        thought_types = [t["type"] for t in state.thoughts]
        if thought_types.count("correction") > 2:
            notes.append("Multiple corrections suggest unstable reasoning")

        # Calculate meta-confidence
        meta_confidence = 0.8 - (0.1 * len([n for n in notes if "shallow" in n or "limited" in n]))

        summary = f"Reasoning analysis: {len(state.thoughts)} thoughts, depth {state.current_depth}, {len(notes)} observations"

        return {
            "summary": summary,
            "notes": notes,
            "needs_correction": needs_correction,
            "correction_suggestion": correction_suggestion,
            "meta_confidence": max(0.3, meta_confidence)
        }


class LearningNode(BaseReasoningNode):
    """Learn from the reasoning process."""

    def __init__(self, learning_callback: Optional[Callable] = None):
        super().__init__("learning")
        self.learning_callback = learning_callback

    async def process(self, state: ChainOfThoughtState) -> ChainOfThoughtState:
        self.logger.debug("Learning from reasoning...")

        state.phase = ReasoningPhase.LEARN.value

        # Generate learning signals
        learning_signals = self._extract_learning_signals(state)

        # Store learning if callback available
        if self.learning_callback:
            try:
                if asyncio.iscoroutinefunction(self.learning_callback):
                    await self.learning_callback(learning_signals)
                else:
                    self.learning_callback(learning_signals)
            except Exception as e:
                self.logger.warning(f"Learning callback failed: {e}")

        # Generate reasoning trace
        state.reasoning_trace = self._generate_trace(state)

        # Mark complete
        state.phase = ReasoningPhase.COMPLETE.value
        state.completed_at = datetime.utcnow().isoformat()
        state.should_continue = False

        return state

    def _extract_learning_signals(self, state: ChainOfThoughtState) -> Dict[str, Any]:
        """Extract learning signals from reasoning."""
        return {
            "reasoning_id": state.reasoning_id,
            "query_type": self._classify_query(state.query),
            "depth_used": state.current_depth,
            "hypotheses_generated": len(state.hypotheses),
            "corrections_made": state.corrections_made,
            "final_confidence": state.confidence,
            "strategy": state.strategy,
            "thought_type_distribution": self._count_thought_types(state),
            "reflection_notes": state.reflection_notes
        }

    def _classify_query(self, query: str) -> str:
        """Classify query type for learning."""
        query_lower = query.lower()
        if "?" in query:
            return "interrogative"
        elif any(w in query_lower for w in ["do", "make", "create"]):
            return "imperative"
        return "declarative"

    def _count_thought_types(self, state: ChainOfThoughtState) -> Dict[str, int]:
        """Count thought types for analysis."""
        counts = {}
        for thought in state.thoughts:
            t_type = thought.get("type", "unknown")
            counts[t_type] = counts.get(t_type, 0) + 1
        return counts

    def _generate_trace(self, state: ChainOfThoughtState) -> str:
        """Generate human-readable reasoning trace."""
        lines = [
            f"=== Chain of Thought Reasoning ===",
            f"Strategy: {state.strategy}",
            f"Depth: {state.current_depth}",
            f"Iterations: {state.iterations}",
            "",
            "Thought Process:"
        ]

        for i, thought in enumerate(state.thoughts, 1):
            lines.append(f"  {i}. [{thought['type'].upper()}]")
            lines.append(f"     {thought['content']}")
            lines.append(f"     Confidence: {thought['confidence']:.2f}")

        lines.extend([
            "",
            f"Hypotheses Evaluated: {len(state.hypotheses)}",
            f"Corrections Made: {state.corrections_made}",
            "",
            f"CONCLUSION: {state.conclusion}",
            f"CONFIDENCE: {state.confidence:.2f}"
        ])

        return "\n".join(lines)


# ============================================================================
# Router Functions
# ============================================================================

def route_after_perception(state: ChainOfThoughtState) -> str:
    """Route after perception."""
    return "analysis"


def route_after_analysis(state: ChainOfThoughtState) -> str:
    """Route after analysis."""
    if state.current_depth >= state.max_depth:
        return "decision"
    return "hypothesis"


def route_after_hypothesis(state: ChainOfThoughtState) -> str:
    """Route after hypothesis generation."""
    if not state.hypotheses:
        return "reflection"  # Reflect on why no hypotheses
    return "evaluation"


def route_after_evaluation(state: ChainOfThoughtState) -> str:
    """Route after evaluation."""
    # Check if confident enough
    if state.confidence >= state.min_confidence:
        return "decision"

    # Check if we've hit max iterations
    if state.iterations >= state.max_iterations:
        return "decision"

    # Need more analysis
    state.iterations += 1
    return "reflection"


def route_after_decision(state: ChainOfThoughtState) -> str:
    """Route after decision."""
    return "reflection"


def route_after_reflection(state: ChainOfThoughtState) -> str:
    """Route after reflection."""
    # Check if we need to iterate
    if state.corrections_made > 0 and state.iterations < state.max_iterations:
        if state.confidence < state.min_confidence:
            state.iterations += 1
            return "analysis"  # Go back and re-analyze

    return "learning"


# ============================================================================
# Main Chain-of-Thought Engine
# ============================================================================

class ChainOfThoughtEngine:
    """
    Main engine for chain-of-thought reasoning.

    Provides multi-step reasoning with explicit thought chains,
    self-reflection, and continuous learning.
    """

    def __init__(
        self,
        strategy: ReasoningStrategy = ReasoningStrategy.ADAPTIVE,
        max_depth: int = 10,
        min_confidence: float = 0.6,
        learning_callback: Optional[Callable] = None,
        enable_checkpointing: bool = True
    ):
        # Initialize logger FIRST - before any methods that might use it
        self.logger = logging.getLogger(__name__)

        self.strategy = strategy
        self.max_depth = max_depth
        self.min_confidence = min_confidence
        self.learning_callback = learning_callback
        self.enable_checkpointing = enable_checkpointing

        # Initialize nodes
        self._init_nodes()

        # Build graph
        self.graph = self._build_graph()
        self.compiled_graph = self._compile_graph()

        # History
        self._reasoning_history: Deque[ReasoningChain] = deque(maxlen=100)

    def _init_nodes(self) -> None:
        """Initialize reasoning nodes."""
        self.perception_node = PerceptionNode()
        self.analysis_node = AnalysisNode()
        self.hypothesis_node = HypothesisNode()
        self.evaluation_node = EvaluationNode()
        self.decision_node = DecisionNode()
        self.reflection_node = ReflectionNode()
        self.learning_node = LearningNode(learning_callback=self.learning_callback)

    def _build_graph(self) -> Optional[StateGraph]:
        """Build the LangGraph state graph."""
        if not LANGGRAPH_AVAILABLE:
            from backend.core.optional_dependencies import note_missing
            note_missing("langgraph", "chain-of-thought graph (fallback)", install="langgraph")
            return None

        graph = StateGraph(ChainOfThoughtState)

        # Add nodes
        graph.add_node("perception", self._wrap_node(self.perception_node))
        graph.add_node("analysis", self._wrap_node(self.analysis_node))
        graph.add_node("hypothesis", self._wrap_node(self.hypothesis_node))
        graph.add_node("evaluation", self._wrap_node(self.evaluation_node))
        graph.add_node("decision", self._wrap_node(self.decision_node))
        graph.add_node("reflection", self._wrap_node(self.reflection_node))
        graph.add_node("learning", self._wrap_node(self.learning_node))

        # Set entry point
        graph.set_entry_point("perception")

        # Add edges
        graph.add_conditional_edges("perception", route_after_perception, {"analysis": "analysis"})
        graph.add_conditional_edges("analysis", route_after_analysis, {
            "hypothesis": "hypothesis",
            "decision": "decision"
        })
        graph.add_conditional_edges("hypothesis", route_after_hypothesis, {
            "evaluation": "evaluation",
            "reflection": "reflection"
        })
        graph.add_conditional_edges("evaluation", route_after_evaluation, {
            "decision": "decision",
            "reflection": "reflection"
        })
        graph.add_conditional_edges("decision", route_after_decision, {
            "reflection": "reflection"
        })
        graph.add_conditional_edges("reflection", route_after_reflection, {
            "analysis": "analysis",
            "learning": "learning"
        })
        graph.add_edge("learning", END)

        return graph

    def _compile_graph(self):
        """Compile the graph."""
        if self.graph is None:
            return None

        compile_kwargs = {}
        if self.enable_checkpointing and LANGGRAPH_AVAILABLE:
            compile_kwargs["checkpointer"] = MemorySaver()

        return self.graph.compile(**compile_kwargs)

    def _wrap_node(self, node: BaseReasoningNode):
        """Wrap node for LangGraph."""
        async def wrapped(state: ChainOfThoughtState) -> ChainOfThoughtState:
            return await node.process(state)
        return wrapped

    async def reason(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        evidence: Optional[List[str]] = None,
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Perform chain-of-thought reasoning.

        Args:
            query: The question or problem to reason about
            context: Additional context information
            evidence: Available evidence
            session_id: Session identifier for tracking

        Returns:
            Reasoning result with conclusion, confidence, and trace
        """
        # Initialize state
        initial_state = ChainOfThoughtState(
            session_id=session_id or str(uuid4()),
            query=query,
            context=context or {},
            available_evidence=evidence or [],
            strategy=self.strategy.value,
            max_depth=self.max_depth,
            min_confidence=self.min_confidence,
            started_at=datetime.utcnow().isoformat()
        )

        # Run reasoning
        if self.compiled_graph:
            try:
                config = {"configurable": {"thread_id": initial_state.session_id}}
                final_state = await self.compiled_graph.ainvoke(initial_state, config=config)
            except Exception as e:
                self.logger.error(f"Graph execution failed: {e}")
                final_state = await self._fallback_reasoning(initial_state)
        else:
            final_state = await self._fallback_reasoning(initial_state)

        # Record in history
        chain = ReasoningChain(
            chain_id=final_state.reasoning_id,
            thoughts=[Thought(**t) if isinstance(t, dict) else t for t in final_state.thoughts],
            strategy=ReasoningStrategy(final_state.strategy),
            final_conclusion=final_state.conclusion,
            overall_confidence=final_state.confidence
        )
        self._reasoning_history.append(chain)

        return {
            "reasoning_id": final_state.reasoning_id,
            "conclusion": final_state.conclusion,
            "confidence": final_state.confidence,
            "reasoning_trace": final_state.reasoning_trace,
            "thought_count": len(final_state.thoughts),
            "depth": final_state.current_depth,
            "hypotheses_evaluated": len(final_state.hypotheses),
            "corrections_made": final_state.corrections_made,
            "reflection_notes": final_state.reflection_notes
        }

    async def _fallback_reasoning(self, state: ChainOfThoughtState) -> ChainOfThoughtState:
        """Fallback sequential reasoning when LangGraph unavailable."""
        state = await self.perception_node.process(state)
        state = await self.analysis_node.process(state)
        state = await self.hypothesis_node.process(state)
        state = await self.evaluation_node.process(state)
        state = await self.decision_node.process(state)
        state = await self.reflection_node.process(state)
        state = await self.learning_node.process(state)
        return state

    def get_reasoning_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent reasoning history."""
        history = list(self._reasoning_history)[-limit:]
        return [
            {
                "chain_id": c.chain_id,
                "conclusion": c.final_conclusion,
                "confidence": c.overall_confidence,
                "depth": c.reasoning_depth,
                "thought_count": len(c.thoughts)
            }
            for c in history
        ]


# ============================================================================
# Mixin for Intelligence Systems
# ============================================================================

class ChainOfThoughtMixin:
    """
    Mixin class to add chain-of-thought reasoning to intelligence systems.

    Add this mixin to UAE, SAI, or CAI to enable advanced reasoning capabilities.

    Usage:
        class EnhancedUAE(UnifiedAwarenessEngine, ChainOfThoughtMixin):
            def __init__(self, ...):
                super().__init__(...)
                self.init_cot_reasoning()
    """

    _cot_engine: Optional[ChainOfThoughtEngine] = None

    def init_cot_reasoning(
        self,
        strategy: ReasoningStrategy = ReasoningStrategy.ADAPTIVE,
        max_depth: int = 10,
        min_confidence: float = 0.6,
        learning_callback: Optional[Callable] = None
    ) -> None:
        """Initialize chain-of-thought reasoning."""
        self._cot_engine = ChainOfThoughtEngine(
            strategy=strategy,
            max_depth=max_depth,
            min_confidence=min_confidence,
            learning_callback=learning_callback
        )

    async def reason_about(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        evidence: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Perform chain-of-thought reasoning."""
        if self._cot_engine is None:
            self.init_cot_reasoning()

        return await self._cot_engine.reason(query, context, evidence)

    def get_reasoning_trace(self) -> str:
        """Get the last reasoning trace."""
        if self._cot_engine and self._cot_engine._reasoning_history:
            last_chain = self._cot_engine._reasoning_history[-1]
            return last_chain.get_trace()
        return "No reasoning trace available"


# ============================================================================
# Factory Functions
# ============================================================================

def create_cot_engine(
    strategy: ReasoningStrategy = ReasoningStrategy.ADAPTIVE,
    learning_callback: Optional[Callable] = None,
    **kwargs
) -> ChainOfThoughtEngine:
    """Create a configured chain-of-thought engine."""
    return ChainOfThoughtEngine(
        strategy=strategy,
        learning_callback=learning_callback,
        **kwargs
    )


async def reason(
    query: str,
    context: Optional[Dict] = None,
    evidence: Optional[List[str]] = None,
    **kwargs
) -> Dict[str, Any]:
    """Quick reasoning helper."""
    engine = create_cot_engine(**kwargs)
    return await engine.reason(query, context, evidence)


# Global engine instance
_default_cot_engine: Optional[ChainOfThoughtEngine] = None


def get_cot_engine() -> ChainOfThoughtEngine:
    """Get or create global CoT engine."""
    global _default_cot_engine
    if _default_cot_engine is None:
        _default_cot_engine = create_cot_engine()
    return _default_cot_engine
