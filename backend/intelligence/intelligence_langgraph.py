"""
Unified Intelligence System with LangGraph Chain-of-Thought Reasoning

This module provides LangGraph-enhanced versions of SAI and CAI, plus a unified
orchestrator that coordinates all three intelligence systems (UAE, SAI, CAI)
with advanced chain-of-thought reasoning.

Components:
- EnhancedSAI: SAI with change reasoning and prediction
- EnhancedCAI: CAI with emotional reasoning chains
- UnifiedIntelligenceOrchestrator: Coordinates all systems

Features:
- Cross-system reasoning fusion
- Adaptive intelligence routing
- Parallel reasoning pipelines
- Continuous learning from outcomes
- Transparent decision audit trails

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
from enum import Enum
from typing import (
    Any, Awaitable, Callable, Deque, Dict, Generic, List, Literal,
    Optional, Protocol, Set, Tuple, Type, TypeVar, Union
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

# Import chain-of-thought
try:
    from .chain_of_thought import (
        ChainOfThoughtEngine,
        ChainOfThoughtMixin,
        ReasoningStrategy,
        ThoughtType,
        Thought,
        create_cot_engine
    )
except ImportError:
    from chain_of_thought import (
        ChainOfThoughtEngine,
        ChainOfThoughtMixin,
        ReasoningStrategy,
        ThoughtType,
        Thought,
        create_cot_engine
    )

logger = logging.getLogger(__name__)


# ============================================================================
# Enhanced SAI with LangGraph
# ============================================================================

class SAIReasoningPhase(str, Enum):
    """Phases for SAI reasoning."""
    ENVIRONMENT_SCAN = "environment_scan"
    CHANGE_DETECTION = "change_detection"
    CHANGE_ANALYSIS = "change_analysis"
    IMPACT_ASSESSMENT = "impact_assessment"
    PREDICTION = "prediction"
    RESPONSE_PLANNING = "response_planning"


class SAIGraphState(BaseModel):
    """State for SAI LangGraph reasoning."""
    reasoning_id: str = Field(default_factory=lambda: str(uuid4()))
    phase: str = SAIReasoningPhase.ENVIRONMENT_SCAN.value

    # Environment state
    current_snapshot: Dict[str, Any] = Field(default_factory=dict)
    previous_snapshot: Optional[Dict[str, Any]] = None
    environment_hash: str = ""

    # Change detection
    detected_changes: List[Dict[str, Any]] = Field(default_factory=list)
    change_significance: float = 0.0

    # Reasoning
    thoughts: List[Dict[str, Any]] = Field(default_factory=list)
    hypotheses: List[Dict[str, Any]] = Field(default_factory=list)

    # Analysis
    affected_elements: List[str] = Field(default_factory=list)
    cascade_effects: List[Dict[str, Any]] = Field(default_factory=list)

    # Prediction
    predicted_changes: List[Dict[str, Any]] = Field(default_factory=list)
    stability_score: float = 1.0

    # Response
    recommended_actions: List[Dict[str, Any]] = Field(default_factory=list)
    cache_invalidations: List[str] = Field(default_factory=list)

    # Control
    confidence: float = 0.0
    iterations: int = 0
    max_iterations: int = 5
    should_continue: bool = True

    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True


class EnvironmentScanNode:
    """Scan environment with reasoning."""

    async def process(self, state: SAIGraphState) -> SAIGraphState:
        state.phase = SAIReasoningPhase.ENVIRONMENT_SCAN.value

        # Create observation thought
        thought = {
            "thought_id": str(uuid4()),
            "type": ThoughtType.OBSERVATION.value,
            "content": f"Scanning environment. Current hash: {state.environment_hash[:16] if state.environment_hash else 'none'}",
            "confidence": 0.9,
            "timestamp": datetime.utcnow().isoformat()
        }
        state.thoughts.append(thought)

        return state


class ChangeDetectionNode:
    """Detect changes with reasoning."""

    async def process(self, state: SAIGraphState) -> SAIGraphState:
        state.phase = SAIReasoningPhase.CHANGE_DETECTION.value

        changes_detected = len(state.detected_changes)

        if changes_detected > 0:
            thought = {
                "thought_id": str(uuid4()),
                "type": ThoughtType.OBSERVATION.value,
                "content": f"Detected {changes_detected} environmental changes",
                "confidence": 0.85,
                "timestamp": datetime.utcnow().isoformat()
            }
            state.thoughts.append(thought)

            # Calculate significance
            state.change_significance = min(1.0, changes_detected / 10)
        else:
            thought = {
                "thought_id": str(uuid4()),
                "type": ThoughtType.OBSERVATION.value,
                "content": "No environmental changes detected",
                "confidence": 0.95,
                "timestamp": datetime.utcnow().isoformat()
            }
            state.thoughts.append(thought)

        return state


class ChangeAnalysisNode:
    """Analyze changes with chain-of-thought."""

    async def process(self, state: SAIGraphState) -> SAIGraphState:
        state.phase = SAIReasoningPhase.CHANGE_ANALYSIS.value

        for change in state.detected_changes:
            # Analyze each change
            analysis = self._analyze_change(change)

            thought = {
                "thought_id": str(uuid4()),
                "type": ThoughtType.ANALYSIS.value,
                "content": f"Change analysis: {analysis['summary']}",
                "confidence": analysis["confidence"],
                "timestamp": datetime.utcnow().isoformat()
            }
            state.thoughts.append(thought)

            # Track affected elements
            if analysis.get("affected_element"):
                state.affected_elements.append(analysis["affected_element"])

            # Create hypothesis about impact
            hyp = {
                "hypothesis_id": str(uuid4()),
                "change_type": change.get("type", "unknown"),
                "impact": analysis["impact"],
                "confidence": analysis["confidence"]
            }
            state.hypotheses.append(hyp)

        return state

    def _analyze_change(self, change: Dict) -> Dict[str, Any]:
        """Analyze a single change."""
        change_type = change.get("type", "unknown")

        impact_levels = {
            "display_changed": 0.9,
            "resolution_changed": 0.8,
            "space_changed": 0.7,
            "position_changed": 0.5,
            "element_disappeared": 0.6,
            "element_appeared": 0.4
        }

        impact = impact_levels.get(change_type, 0.5)

        return {
            "summary": f"{change_type}: impact level {impact:.2f}",
            "impact": impact,
            "confidence": 0.7 + 0.2 * (1 - impact),  # Lower impact = higher confidence
            "affected_element": change.get("element_id")
        }


class ImpactAssessmentNode:
    """Assess impact with reasoning."""

    async def process(self, state: SAIGraphState) -> SAIGraphState:
        state.phase = SAIReasoningPhase.IMPACT_ASSESSMENT.value

        # Calculate cascade effects
        if state.affected_elements:
            for element in state.affected_elements:
                cascade = self._calculate_cascade(element, state)
                state.cascade_effects.append(cascade)

            thought = {
                "thought_id": str(uuid4()),
                "type": ThoughtType.INFERENCE.value,
                "content": f"Impact assessment: {len(state.affected_elements)} elements affected with {len(state.cascade_effects)} cascade effects",
                "confidence": 0.75,
                "timestamp": datetime.utcnow().isoformat()
            }
            state.thoughts.append(thought)

        # Calculate stability score
        impact_sum = sum(h.get("impact", 0) for h in state.hypotheses)
        state.stability_score = max(0, 1.0 - impact_sum / max(len(state.hypotheses), 1))

        return state

    def _calculate_cascade(self, element: str, state: SAIGraphState) -> Dict[str, Any]:
        """Calculate cascade effects for an element."""
        return {
            "source_element": element,
            "cascade_type": "cache_invalidation",
            "affected_count": 1,
            "severity": 0.5
        }


class SAIPredictionNode:
    """Predict future changes."""

    async def process(self, state: SAIGraphState) -> SAIGraphState:
        state.phase = SAIReasoningPhase.PREDICTION.value

        # Predict based on patterns
        if state.change_significance > 0.5:
            prediction = {
                "prediction_type": "continued_instability",
                "probability": state.change_significance,
                "timeframe": "next_5_minutes",
                "reasoning": "High change activity suggests ongoing environment changes"
            }
            state.predicted_changes.append(prediction)

            thought = {
                "thought_id": str(uuid4()),
                "type": ThoughtType.PREDICTION.value,
                "content": f"Predicting continued instability ({state.change_significance:.0%} probability)",
                "confidence": 0.6,
                "timestamp": datetime.utcnow().isoformat()
            }
            state.thoughts.append(thought)
        else:
            thought = {
                "thought_id": str(uuid4()),
                "type": ThoughtType.PREDICTION.value,
                "content": "Environment appears stable, no significant changes predicted",
                "confidence": 0.8,
                "timestamp": datetime.utcnow().isoformat()
            }
            state.thoughts.append(thought)

        return state


class SAIResponsePlanningNode:
    """Plan response to changes."""

    async def process(self, state: SAIGraphState) -> SAIGraphState:
        state.phase = SAIReasoningPhase.RESPONSE_PLANNING.value

        # Plan cache invalidations
        for element in state.affected_elements:
            state.cache_invalidations.append(element)

            action = {
                "action_type": "invalidate_cache",
                "target": element,
                "priority": "high" if state.change_significance > 0.7 else "normal"
            }
            state.recommended_actions.append(action)

        # Calculate final confidence
        state.confidence = state.stability_score * 0.5 + 0.5 * (1 - state.change_significance)

        # Final decision thought
        thought = {
            "thought_id": str(uuid4()),
            "type": ThoughtType.DECISION.value,
            "content": f"Response plan: {len(state.recommended_actions)} actions, {len(state.cache_invalidations)} cache invalidations",
            "confidence": state.confidence,
            "timestamp": datetime.utcnow().isoformat()
        }
        state.thoughts.append(thought)

        state.completed_at = datetime.utcnow().isoformat()
        state.should_continue = False

        return state


class EnhancedSAI:
    """
    Enhanced SAI with LangGraph Chain-of-Thought Reasoning.

    Provides reasoning-based change detection and environmental awareness.
    """

    def __init__(self, base_sai: Optional[Any] = None):
        self.base_sai = base_sai
        self.graph: Optional[StateGraph] = None
        self.compiled_graph = None
        self._reasoning_history: Deque[Dict] = deque(maxlen=100)

        self._build_graph()
        self.logger = logging.getLogger(__name__)

    def _build_graph(self) -> None:
        """Build the SAI LangGraph."""
        if not LANGGRAPH_AVAILABLE:
            return

        graph = StateGraph(SAIGraphState)

        # Add nodes
        graph.add_node("environment_scan", self._wrap(EnvironmentScanNode()))
        graph.add_node("change_detection", self._wrap(ChangeDetectionNode()))
        graph.add_node("change_analysis", self._wrap(ChangeAnalysisNode()))
        graph.add_node("impact_assessment", self._wrap(ImpactAssessmentNode()))
        graph.add_node("prediction", self._wrap(SAIPredictionNode()))
        graph.add_node("response_planning", self._wrap(SAIResponsePlanningNode()))

        # Set entry and edges
        graph.set_entry_point("environment_scan")
        graph.add_edge("environment_scan", "change_detection")
        graph.add_edge("change_detection", "change_analysis")
        graph.add_edge("change_analysis", "impact_assessment")
        graph.add_edge("impact_assessment", "prediction")
        graph.add_edge("prediction", "response_planning")
        graph.add_edge("response_planning", END)

        self.graph = graph
        self.compiled_graph = graph.compile(checkpointer=MemorySaver())

    def _wrap(self, node):
        async def wrapped(state: SAIGraphState) -> SAIGraphState:
            return await node.process(state)
        return wrapped

    async def analyze_environment_with_reasoning(
        self,
        current_snapshot: Dict[str, Any],
        previous_snapshot: Optional[Dict[str, Any]] = None,
        detected_changes: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """Analyze environment with full reasoning."""
        initial_state = SAIGraphState(
            current_snapshot=current_snapshot,
            previous_snapshot=previous_snapshot,
            detected_changes=detected_changes or [],
            started_at=datetime.utcnow().isoformat()
        )

        if self.compiled_graph:
            config = {"configurable": {"thread_id": initial_state.reasoning_id}}
            final_state = await self.compiled_graph.ainvoke(initial_state, config=config)
        else:
            final_state = await self._fallback(initial_state)

        state_data = self._normalize_state(final_state)
        result = {
            "reasoning_id": state_data.get("reasoning_id"),
            "stability_score": state_data.get("stability_score", 0.0),
            "change_significance": state_data.get("change_significance", 0.0),
            "affected_elements": state_data.get("affected_elements", []),
            "recommended_actions": state_data.get("recommended_actions", []),
            "predictions": state_data.get("predicted_changes", []),
            "confidence": state_data.get("confidence", 0.0),
            "thought_count": len(state_data.get("thoughts", [])),
            "reasoning_trace": self._generate_trace(state_data),
        }

        self._reasoning_history.append(result)
        return result

    async def _fallback(self, state: SAIGraphState) -> SAIGraphState:
        """Fallback sequential execution."""
        state = await EnvironmentScanNode().process(state)
        state = await ChangeDetectionNode().process(state)
        state = await ChangeAnalysisNode().process(state)
        state = await ImpactAssessmentNode().process(state)
        state = await SAIPredictionNode().process(state)
        state = await SAIResponsePlanningNode().process(state)
        return state

    def _normalize_state(self, state: Any) -> Dict[str, Any]:
        """Normalize graph state to dictionary for robust access."""
        if isinstance(state, dict):
            return state
        if hasattr(state, "model_dump"):
            return state.model_dump()
        if hasattr(state, "dict"):
            return state.dict()
        return dict(vars(state))

    def _generate_trace(self, state: Any) -> str:
        """Generate reasoning trace."""
        data = self._normalize_state(state)
        lines = ["=== SAI Reasoning Trace ==="]
        for i, thought in enumerate(data.get("thoughts", []), 1):
            lines.append(f"{i}. [{thought['type']}] {thought['content']}")
        return "\n".join(lines)


# ============================================================================
# Enhanced CAI with LangGraph
# ============================================================================

class CAIReasoningPhase(str, Enum):
    """Phases for CAI reasoning."""
    SIGNAL_EXTRACTION = "signal_extraction"
    PATTERN_RECOGNITION = "pattern_recognition"
    EMOTIONAL_INFERENCE = "emotional_inference"
    COGNITIVE_ASSESSMENT = "cognitive_assessment"
    CONTEXT_UNDERSTANDING = "context_understanding"
    PERSONALITY_ADAPTATION = "personality_adaptation"


class CAIGraphState(BaseModel):
    """State for CAI LangGraph reasoning."""
    reasoning_id: str = Field(default_factory=lambda: str(uuid4()))
    phase: str = CAIReasoningPhase.SIGNAL_EXTRACTION.value

    # Input
    workspace_state: Dict[str, Any] = Field(default_factory=dict)
    activity_data: Dict[str, Any] = Field(default_factory=dict)

    # Signals
    behavioral_signals: List[Dict[str, Any]] = Field(default_factory=list)
    signal_patterns: List[Dict[str, Any]] = Field(default_factory=list)

    # Reasoning
    thoughts: List[Dict[str, Any]] = Field(default_factory=list)
    hypotheses: List[Dict[str, Any]] = Field(default_factory=list)

    # Analysis results
    emotional_state: str = "neutral"
    emotional_confidence: float = 0.5
    cognitive_load: str = "moderate"
    cognitive_confidence: float = 0.5
    work_context: str = "general"
    context_confidence: float = 0.5

    # Insights
    insights: List[str] = Field(default_factory=list)
    recommendations: List[Dict[str, Any]] = Field(default_factory=list)

    # Personality adaptation
    personality_adjustments: Dict[str, Any] = Field(default_factory=dict)
    communication_style: str = "balanced"

    # Control
    confidence: float = 0.0
    iterations: int = 0
    should_continue: bool = True

    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True


class SignalExtractionNode:
    """Extract behavioral signals with reasoning."""

    async def process(self, state: CAIGraphState) -> CAIGraphState:
        state.phase = CAIReasoningPhase.SIGNAL_EXTRACTION.value

        # Extract signals from workspace state
        signals = self._extract_signals(state.workspace_state, state.activity_data)
        state.behavioral_signals = signals

        thought = {
            "thought_id": str(uuid4()),
            "type": ThoughtType.OBSERVATION.value,
            "content": f"Extracted {len(signals)} behavioral signals from user activity",
            "confidence": 0.85,
            "timestamp": datetime.utcnow().isoformat()
        }
        state.thoughts.append(thought)

        return state

    def _extract_signals(self, workspace: Dict, activity: Dict) -> List[Dict]:
        """Extract behavioral signals."""
        signals = []

        # Window switching signal
        window_count = workspace.get("window_count", 0)
        signals.append({
            "type": "window_activity",
            "value": min(window_count / 10, 1.0),
            "confidence": 0.9
        })

        # Typing pattern
        typing_speed = activity.get("typing_speed", 50)
        signals.append({
            "type": "typing_pattern",
            "value": min(typing_speed / 100, 1.0),
            "confidence": 0.8
        })

        # Application diversity
        app_count = len(workspace.get("active_apps", []))
        signals.append({
            "type": "app_diversity",
            "value": min(app_count / 8, 1.0),
            "confidence": 0.85
        })

        return signals


class PatternRecognitionNode:
    """Recognize patterns with reasoning."""

    async def process(self, state: CAIGraphState) -> CAIGraphState:
        state.phase = CAIReasoningPhase.PATTERN_RECOGNITION.value

        # Recognize patterns from signals
        patterns = self._recognize_patterns(state.behavioral_signals)
        state.signal_patterns = patterns

        for pattern in patterns:
            thought = {
                "thought_id": str(uuid4()),
                "type": ThoughtType.ANALYSIS.value,
                "content": f"Pattern detected: {pattern['name']} (strength: {pattern['strength']:.2f})",
                "confidence": pattern["confidence"],
                "timestamp": datetime.utcnow().isoformat()
            }
            state.thoughts.append(thought)

        return state

    def _recognize_patterns(self, signals: List[Dict]) -> List[Dict]:
        """Recognize behavioral patterns."""
        patterns = []

        # Check for high activity
        high_signals = [s for s in signals if s["value"] > 0.7]
        if len(high_signals) > 1:
            patterns.append({
                "name": "high_activity",
                "strength": sum(s["value"] for s in high_signals) / len(high_signals),
                "confidence": 0.75
            })

        # Check for low activity
        low_signals = [s for s in signals if s["value"] < 0.3]
        if len(low_signals) > 1:
            patterns.append({
                "name": "low_activity",
                "strength": 1 - sum(s["value"] for s in low_signals) / len(low_signals),
                "confidence": 0.8
            })

        return patterns


class EmotionalInferenceNode:
    """Infer emotional state with chain-of-thought."""

    async def process(self, state: CAIGraphState) -> CAIGraphState:
        state.phase = CAIReasoningPhase.EMOTIONAL_INFERENCE.value

        # Generate emotional hypotheses
        hypotheses = self._generate_emotional_hypotheses(state)

        for hyp in hypotheses:
            state.hypotheses.append(hyp)

            thought = {
                "thought_id": str(uuid4()),
                "type": ThoughtType.HYPOTHESIS.value,
                "content": f"Emotional hypothesis: {hyp['state']} (confidence: {hyp['confidence']:.2f})",
                "confidence": hyp["confidence"],
                "timestamp": datetime.utcnow().isoformat()
            }
            state.thoughts.append(thought)

        # Select best hypothesis
        if hypotheses:
            best = max(hypotheses, key=lambda h: h["confidence"])
            state.emotional_state = best["state"]
            state.emotional_confidence = best["confidence"]

            # Decision thought
            thought = {
                "thought_id": str(uuid4()),
                "type": ThoughtType.DECISION.value,
                "content": f"Inferred emotional state: {state.emotional_state}",
                "confidence": state.emotional_confidence,
                "timestamp": datetime.utcnow().isoformat()
            }
            state.thoughts.append(thought)

        return state

    def _generate_emotional_hypotheses(self, state: CAIGraphState) -> List[Dict]:
        """Generate emotional state hypotheses."""
        hypotheses = []

        # Check patterns
        high_activity = any(p["name"] == "high_activity" for p in state.signal_patterns)
        low_activity = any(p["name"] == "low_activity" for p in state.signal_patterns)

        if high_activity:
            hypotheses.append({"state": "focused", "confidence": 0.7, "reasoning": "High activity patterns"})
            hypotheses.append({"state": "stressed", "confidence": 0.5, "reasoning": "Could indicate stress"})
        elif low_activity:
            hypotheses.append({"state": "relaxed", "confidence": 0.6, "reasoning": "Low activity patterns"})
            hypotheses.append({"state": "tired", "confidence": 0.5, "reasoning": "Could indicate fatigue"})
        else:
            hypotheses.append({"state": "neutral", "confidence": 0.7, "reasoning": "Balanced activity"})

        return hypotheses


class CognitiveAssessmentNode:
    """Assess cognitive load with reasoning."""

    async def process(self, state: CAIGraphState) -> CAIGraphState:
        state.phase = CAIReasoningPhase.COGNITIVE_ASSESSMENT.value

        # Calculate cognitive load
        load_score = self._calculate_load(state)

        if load_score < 0.3:
            state.cognitive_load = "low"
        elif load_score < 0.6:
            state.cognitive_load = "moderate"
        elif load_score < 0.8:
            state.cognitive_load = "high"
        else:
            state.cognitive_load = "overload"

        state.cognitive_confidence = 0.7

        thought = {
            "thought_id": str(uuid4()),
            "type": ThoughtType.INFERENCE.value,
            "content": f"Cognitive load assessed as {state.cognitive_load} (score: {load_score:.2f})",
            "confidence": state.cognitive_confidence,
            "timestamp": datetime.utcnow().isoformat()
        }
        state.thoughts.append(thought)

        return state

    def _calculate_load(self, state: CAIGraphState) -> float:
        """Calculate cognitive load score."""
        load = 0.0

        for signal in state.behavioral_signals:
            if signal["type"] == "window_activity":
                load += signal["value"] * 0.3
            elif signal["type"] == "app_diversity":
                load += signal["value"] * 0.4
            elif signal["type"] == "typing_pattern":
                load += signal["value"] * 0.3

        return min(1.0, load)


class ContextUnderstandingNode:
    """Understand work context with reasoning."""

    async def process(self, state: CAIGraphState) -> CAIGraphState:
        state.phase = CAIReasoningPhase.CONTEXT_UNDERSTANDING.value

        # Determine work context
        context = self._determine_context(state.workspace_state)
        state.work_context = context["type"]
        state.context_confidence = context["confidence"]

        thought = {
            "thought_id": str(uuid4()),
            "type": ThoughtType.INFERENCE.value,
            "content": f"Work context identified as {state.work_context}",
            "confidence": state.context_confidence,
            "timestamp": datetime.utcnow().isoformat()
        }
        state.thoughts.append(thought)

        # Generate insights
        state.insights = self._generate_insights(state)

        return state

    def _determine_context(self, workspace: Dict) -> Dict[str, Any]:
        """Determine work context."""
        apps = workspace.get("active_apps", [])
        apps_lower = [a.lower() for a in apps]

        # Check for development
        dev_apps = ["code", "xcode", "terminal", "vscode", "pycharm"]
        if any(d in " ".join(apps_lower) for d in dev_apps):
            return {"type": "development", "confidence": 0.85}

        # Check for meetings
        meeting_apps = ["zoom", "teams", "meet", "webex"]
        if any(m in " ".join(apps_lower) for m in meeting_apps):
            return {"type": "meetings", "confidence": 0.9}

        # Check for communication
        comm_apps = ["slack", "discord", "mail", "messages"]
        if any(c in " ".join(apps_lower) for c in comm_apps):
            return {"type": "communication", "confidence": 0.8}

        return {"type": "general", "confidence": 0.6}

    def _generate_insights(self, state: CAIGraphState) -> List[str]:
        """Generate contextual insights."""
        insights = []

        if state.cognitive_load in ["high", "overload"]:
            insights.append("Consider taking a short break to reduce cognitive load")

        if state.emotional_state == "stressed":
            insights.append("Stress indicators detected - mindfulness break recommended")

        if state.work_context == "development" and state.emotional_state == "focused":
            insights.append("User appears to be in a productive development flow")

        return insights


class PersonalityAdaptationNode:
    """Adapt personality based on analysis."""

    async def process(self, state: CAIGraphState) -> CAIGraphState:
        state.phase = CAIReasoningPhase.PERSONALITY_ADAPTATION.value

        # Determine adaptations
        adaptations = self._determine_adaptations(state)
        state.personality_adjustments = adaptations
        state.communication_style = adaptations.get("communication_style", "balanced")

        # Add recommendations
        state.recommendations = self._generate_recommendations(state)

        # Calculate final confidence
        state.confidence = (
            state.emotional_confidence * 0.4 +
            state.cognitive_confidence * 0.3 +
            state.context_confidence * 0.3
        )

        # Final thought
        thought = {
            "thought_id": str(uuid4()),
            "type": ThoughtType.DECISION.value,
            "content": f"Personality adapted: {state.communication_style} style, {len(state.recommendations)} recommendations",
            "confidence": state.confidence,
            "timestamp": datetime.utcnow().isoformat()
        }
        state.thoughts.append(thought)

        state.completed_at = datetime.utcnow().isoformat()
        state.should_continue = False

        return state

    def _determine_adaptations(self, state: CAIGraphState) -> Dict[str, Any]:
        """Determine personality adaptations."""
        if state.cognitive_load in ["high", "overload"]:
            return {
                "communication_style": "concise",
                "verbosity": "low",
                "proactivity": "minimal"
            }
        elif state.emotional_state in ["stressed", "frustrated"]:
            return {
                "communication_style": "supportive",
                "verbosity": "moderate",
                "proactivity": "gentle"
            }
        elif state.emotional_state == "focused":
            return {
                "communication_style": "minimal",
                "verbosity": "low",
                "proactivity": "passive"
            }
        else:
            return {
                "communication_style": "balanced",
                "verbosity": "moderate",
                "proactivity": "moderate"
            }

    def _generate_recommendations(self, state: CAIGraphState) -> List[Dict]:
        """Generate behavioral recommendations."""
        recs = []

        if state.cognitive_load == "overload":
            recs.append({
                "type": "suggestion",
                "priority": "high",
                "content": "Consider closing some applications to reduce cognitive load"
            })

        if state.emotional_state == "tired":
            recs.append({
                "type": "suggestion",
                "priority": "medium",
                "content": "A short break might help restore energy"
            })

        return recs


class EnhancedCAI:
    """
    Enhanced CAI with LangGraph Chain-of-Thought Reasoning.

    Provides reasoning-based emotional and contextual understanding.
    """

    def __init__(self, base_cai: Optional[Any] = None):
        self.base_cai = base_cai
        self.graph: Optional[StateGraph] = None
        self.compiled_graph = None
        self._reasoning_history: Deque[Dict] = deque(maxlen=100)

        self._build_graph()
        self.logger = logging.getLogger(__name__)

    def _build_graph(self) -> None:
        """Build the CAI LangGraph."""
        if not LANGGRAPH_AVAILABLE:
            return

        graph = StateGraph(CAIGraphState)

        # Add nodes
        graph.add_node("signal_extraction", self._wrap(SignalExtractionNode()))
        graph.add_node("pattern_recognition", self._wrap(PatternRecognitionNode()))
        graph.add_node("emotional_inference", self._wrap(EmotionalInferenceNode()))
        graph.add_node("cognitive_assessment", self._wrap(CognitiveAssessmentNode()))
        graph.add_node("context_understanding", self._wrap(ContextUnderstandingNode()))
        graph.add_node("personality_adaptation", self._wrap(PersonalityAdaptationNode()))

        # Set entry and edges
        graph.set_entry_point("signal_extraction")
        graph.add_edge("signal_extraction", "pattern_recognition")
        graph.add_edge("pattern_recognition", "emotional_inference")
        graph.add_edge("emotional_inference", "cognitive_assessment")
        graph.add_edge("cognitive_assessment", "context_understanding")
        graph.add_edge("context_understanding", "personality_adaptation")
        graph.add_edge("personality_adaptation", END)

        self.graph = graph
        self.compiled_graph = graph.compile(checkpointer=MemorySaver())

    def _wrap(self, node):
        async def wrapped(state: CAIGraphState) -> CAIGraphState:
            return await node.process(state)
        return wrapped

    async def analyze_user_state_with_reasoning(
        self,
        workspace_state: Dict[str, Any],
        activity_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Analyze user state with full reasoning."""
        initial_state = CAIGraphState(
            workspace_state=workspace_state,
            activity_data=activity_data or {},
            started_at=datetime.utcnow().isoformat()
        )

        if self.compiled_graph:
            config = {"configurable": {"thread_id": initial_state.reasoning_id}}
            final_state = await self.compiled_graph.ainvoke(initial_state, config=config)
        else:
            final_state = await self._fallback(initial_state)

        state_data = self._normalize_state(final_state)
        result = {
            "reasoning_id": state_data.get("reasoning_id"),
            "emotional_state": state_data.get("emotional_state", "neutral"),
            "emotional_confidence": state_data.get("emotional_confidence", 0.5),
            "cognitive_load": state_data.get("cognitive_load", "moderate"),
            "work_context": state_data.get("work_context", "general"),
            "insights": state_data.get("insights", []),
            "recommendations": state_data.get("recommendations", []),
            "personality_adjustments": state_data.get("personality_adjustments", {}),
            "communication_style": state_data.get("communication_style", "balanced"),
            "confidence": state_data.get("confidence", 0.0),
            "thought_count": len(state_data.get("thoughts", [])),
            "reasoning_trace": self._generate_trace(state_data),
        }

        self._reasoning_history.append(result)
        return result

    async def _fallback(self, state: CAIGraphState) -> CAIGraphState:
        """Fallback sequential execution."""
        state = await SignalExtractionNode().process(state)
        state = await PatternRecognitionNode().process(state)
        state = await EmotionalInferenceNode().process(state)
        state = await CognitiveAssessmentNode().process(state)
        state = await ContextUnderstandingNode().process(state)
        state = await PersonalityAdaptationNode().process(state)
        return state

    def _normalize_state(self, state: Any) -> Dict[str, Any]:
        """Normalize graph state to dictionary for robust access."""
        if isinstance(state, dict):
            return state
        if hasattr(state, "model_dump"):
            return state.model_dump()
        if hasattr(state, "dict"):
            return state.dict()
        return dict(vars(state))

    def _generate_trace(self, state: Any) -> str:
        """Generate reasoning trace."""
        data = self._normalize_state(state)
        lines = ["=== CAI Reasoning Trace ==="]
        for i, thought in enumerate(data.get("thoughts", []), 1):
            lines.append(f"{i}. [{thought['type']}] {thought['content']}")
        return "\n".join(lines)


# ============================================================================
# Unified Intelligence Orchestrator
# ============================================================================

class UnifiedIntelligenceOrchestrator:
    """
    Orchestrates UAE, SAI, and CAI with unified chain-of-thought reasoning.

    Provides:
    - Cross-system intelligence fusion
    - Coordinated decision making
    - Unified reasoning traces
    - Adaptive system selection
    """

    def __init__(
        self,
        enhanced_uae: Optional[Any] = None,
        enhanced_sai: Optional[EnhancedSAI] = None,
        enhanced_cai: Optional[EnhancedCAI] = None
    ):
        self.uae = enhanced_uae
        self.sai = enhanced_sai or EnhancedSAI()
        self.cai = enhanced_cai or EnhancedCAI()

        self._cot_engine = create_cot_engine(strategy=ReasoningStrategy.ADAPTIVE)
        self._unified_history: Deque[Dict] = deque(maxlen=100)

        self.logger = logging.getLogger(__name__)

    async def analyze_comprehensive(
        self,
        workspace_state: Dict[str, Any],
        activity_data: Optional[Dict[str, Any]] = None,
        element_id: Optional[str] = None,
        environmental_changes: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Perform comprehensive analysis using all intelligence systems.

        Args:
            workspace_state: Current workspace state
            activity_data: User activity data
            element_id: Element to locate (for UAE)
            environmental_changes: Detected changes (for SAI)

        Returns:
            Unified intelligence result with full reasoning
        """
        start_time = time.time()
        results = {}

        # Run analyses in parallel
        tasks = []

        # CAI analysis (always run)
        tasks.append(self._run_cai(workspace_state, activity_data))

        # SAI analysis (if changes provided)
        if environmental_changes:
            tasks.append(self._run_sai(workspace_state, environmental_changes))

        # UAE analysis (if element specified)
        if element_id and self.uae:
            tasks.append(self._run_uae(element_id))

        # Execute in parallel
        parallel_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for result in parallel_results:
            if isinstance(result, Exception):
                self.logger.warning(f"Analysis failed: {result}")
            elif isinstance(result, dict):
                results.update(result)

        # Perform unified reasoning
        unified_conclusion = await self._unified_reasoning(results)

        # Calculate overall metrics
        duration_ms = (time.time() - start_time) * 1000

        unified_result = {
            "timestamp": datetime.utcnow().isoformat(),
            "duration_ms": duration_ms,
            "systems_used": list(results.keys()),
            "cai_result": results.get("cai"),
            "sai_result": results.get("sai"),
            "uae_result": results.get("uae"),
            "unified_conclusion": unified_conclusion,
            "overall_confidence": self._calculate_overall_confidence(results)
        }

        self._unified_history.append(unified_result)
        return unified_result

    async def _run_cai(self, workspace: Dict, activity: Optional[Dict]) -> Dict[str, Any]:
        """Run CAI analysis."""
        result = await self.cai.analyze_user_state_with_reasoning(workspace, activity)
        return {"cai": result}

    async def _run_sai(self, workspace: Dict, changes: List[Dict]) -> Dict[str, Any]:
        """Run SAI analysis."""
        result = await self.sai.analyze_environment_with_reasoning(workspace, None, changes)
        return {"sai": result}

    async def _run_uae(self, element_id: str) -> Dict[str, Any]:
        """Run UAE analysis."""
        if self.uae:
            result = await self.uae.get_element_position_with_reasoning(element_id)
            return {"uae": {
                "element_id": element_id,
                "position": result.chosen_position,
                "confidence": result.confidence,
                "reasoning_trace": result.reasoning_trace
            }}
        return {}

    async def _unified_reasoning(self, results: Dict[str, Any]) -> Dict[str, Any]:
        """Perform unified reasoning across all system results."""
        # Use CoT engine for unified reasoning
        context = {
            "cai_emotional_state": results.get("cai", {}).get("emotional_state"),
            "cai_cognitive_load": results.get("cai", {}).get("cognitive_load"),
            "sai_stability": results.get("sai", {}).get("stability_score"),
            "sai_changes": results.get("sai", {}).get("change_significance"),
            "uae_confidence": results.get("uae", {}).get("confidence")
        }

        # Filter out None values
        context = {k: v for k, v in context.items() if v is not None}

        reasoning_result = await self._cot_engine.reason(
            query="What is the overall system state and recommended action?",
            context=context
        )

        return {
            "conclusion": reasoning_result.get("conclusion"),
            "confidence": reasoning_result.get("confidence"),
            "reasoning_trace": reasoning_result.get("reasoning_trace")
        }

    def _calculate_overall_confidence(self, results: Dict[str, Any]) -> float:
        """Calculate overall confidence from all systems."""
        confidences = []

        if results.get("cai"):
            confidences.append(results["cai"].get("confidence", 0))
        if results.get("sai"):
            confidences.append(results["sai"].get("confidence", 0))
        if results.get("uae"):
            confidences.append(results["uae"].get("confidence", 0))

        if not confidences:
            return 0.0

        return sum(confidences) / len(confidences)

    def get_unified_metrics(self) -> Dict[str, Any]:
        """Get unified intelligence metrics."""
        return {
            "total_analyses": len(self._unified_history),
            "avg_confidence": np.mean([h.get("overall_confidence", 0) for h in self._unified_history]) if self._unified_history else 0,
            "systems_available": {
                "uae": self.uae is not None,
                "sai": self.sai is not None,
                "cai": self.cai is not None
            },
            "langgraph_available": LANGGRAPH_AVAILABLE
        }


# ============================================================================
# Factory Functions
# ============================================================================

def create_enhanced_sai(base_sai: Optional[Any] = None) -> EnhancedSAI:
    """Create enhanced SAI."""
    return EnhancedSAI(base_sai=base_sai)


def create_enhanced_cai(base_cai: Optional[Any] = None) -> EnhancedCAI:
    """Create enhanced CAI."""
    return EnhancedCAI(base_cai=base_cai)


def create_unified_orchestrator(
    uae: Optional[Any] = None,
    sai: Optional[EnhancedSAI] = None,
    cai: Optional[EnhancedCAI] = None,
    # v21.0.0: Accept enhanced_* naming convention for compatibility
    enhanced_uae: Optional[Any] = None,
    enhanced_sai: Optional[EnhancedSAI] = None,
    enhanced_cai: Optional[EnhancedCAI] = None
) -> UnifiedIntelligenceOrchestrator:
    """
    Create unified intelligence orchestrator.

    Accepts both naming conventions for flexibility:
    - uae/sai/cai (original)
    - enhanced_uae/enhanced_sai/enhanced_cai (v21.0.0)

    The enhanced_* parameters take precedence if both are provided.
    """
    # Use enhanced_* if provided, otherwise fall back to original names
    final_uae = enhanced_uae if enhanced_uae is not None else uae
    final_sai = enhanced_sai if enhanced_sai is not None else sai
    final_cai = enhanced_cai if enhanced_cai is not None else cai

    return UnifiedIntelligenceOrchestrator(
        enhanced_uae=final_uae,
        enhanced_sai=final_sai,
        enhanced_cai=final_cai
    )


# Global instances
_unified_orchestrator: Optional[UnifiedIntelligenceOrchestrator] = None


async def get_unified_orchestrator() -> UnifiedIntelligenceOrchestrator:
    """Get or create global orchestrator."""
    global _unified_orchestrator
    if _unified_orchestrator is None:
        _unified_orchestrator = create_unified_orchestrator()
    return _unified_orchestrator
