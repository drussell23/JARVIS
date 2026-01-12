"""
MetaCognitiveEngine v100.0 - Self-Aware Reasoning and Introspection
====================================================================

Advanced meta-cognition system that enables JARVIS to:
1. Introspect on its own reasoning processes
2. Identify blind spots and biases in decision-making
3. Self-correct erroneous assumptions
4. Assess the limits of its own knowledge
5. Detect overconfidence and underconfidence patterns
6. Learn from reasoning failures

This is a critical AGI component that provides self-awareness capabilities.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                    MetaCognitiveEngine                          │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  IntrospectionLayer                                        │ │
    │  │  - Reasoning chain analysis                                │ │
    │  │  - Decision audit trails                                   │ │
    │  │  - Confidence calibration                                  │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  BiasDetector                                              │ │
    │  │  - Recency bias detection                                  │ │
    │  │  - Confirmation bias detection                             │ │
    │  │  - Anchoring bias detection                                │ │
    │  │  - Availability heuristic detection                        │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  BlindSpotAnalyzer                                         │ │
    │  │  - Knowledge gap identification                            │ │
    │  │  - Assumption tracking                                     │ │
    │  │  - Uncertainty quantification                              │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  SelfCorrectionModule                                      │ │
    │  │  - Error pattern detection                                 │ │
    │  │  - Automatic correction strategies                         │ │
    │  │  - Learning from mistakes                                  │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  CapabilityAssessor                                        │ │
    │  │  - Task difficulty estimation                              │ │
    │  │  - Competence boundaries                                   │ │
    │  │  - Confidence-accuracy correlation                         │ │
    │  └────────────────────────────────────────────────────────────┘ │
    └─────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import statistics
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

# Environment-driven configuration
META_COGNITIVE_DATA_DIR = Path(os.getenv(
    "META_COGNITIVE_DATA_DIR",
    str(Path.home() / ".jarvis" / "meta_cognition")
))
INTROSPECTION_INTERVAL_SECONDS = float(os.getenv("INTROSPECTION_INTERVAL_SECONDS", "300"))
CONFIDENCE_CALIBRATION_WINDOW = int(os.getenv("CONFIDENCE_CALIBRATION_WINDOW", "100"))
BIAS_DETECTION_THRESHOLD = float(os.getenv("BIAS_DETECTION_THRESHOLD", "0.7"))
BLIND_SPOT_SENSITIVITY = float(os.getenv("BLIND_SPOT_SENSITIVITY", "0.5"))
SELF_CORRECTION_ENABLED = os.getenv("SELF_CORRECTION_ENABLED", "true").lower() == "true"
MAX_REASONING_CHAIN_DEPTH = int(os.getenv("MAX_REASONING_CHAIN_DEPTH", "50"))


class ReasoningOutcome(Enum):
    """Outcomes of reasoning processes."""
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    UNCERTAIN = "uncertain"
    CORRECTED = "corrected"


class BiasType(Enum):
    """Types of cognitive biases to detect."""
    RECENCY = "recency"  # Over-weighting recent information
    CONFIRMATION = "confirmation"  # Favoring confirming evidence
    ANCHORING = "anchoring"  # Over-relying on first information
    AVAILABILITY = "availability"  # Overweighting easily recalled info
    OVERCONFIDENCE = "overconfidence"  # Excessive certainty
    UNDERCONFIDENCE = "underconfidence"  # Excessive doubt
    SUNK_COST = "sunk_cost"  # Continuing due to past investment
    BANDWAGON = "bandwagon"  # Following popular patterns
    HINDSIGHT = "hindsight"  # Believing past events were predictable


class BlindSpotType(Enum):
    """Types of blind spots in reasoning."""
    KNOWLEDGE_GAP = "knowledge_gap"  # Missing domain knowledge
    ASSUMPTION_ERROR = "assumption_error"  # Incorrect assumptions
    SCOPE_LIMITATION = "scope_limitation"  # Not considering all factors
    TEMPORAL_BLINDNESS = "temporal_blindness"  # Ignoring time effects
    CONTEXT_INSENSITIVITY = "context_insensitivity"  # Missing context
    EDGE_CASE_NEGLECT = "edge_case_neglect"  # Not handling edge cases


class IntrospectionLevel(Enum):
    """Depth of introspection analysis."""
    SURFACE = "surface"  # Quick checks
    MODERATE = "moderate"  # Standard analysis
    DEEP = "deep"  # Thorough examination
    EXHAUSTIVE = "exhaustive"  # Complete audit


@dataclass
class ReasoningStep:
    """A single step in a reasoning chain."""
    step_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Content
    thought: str = ""
    evidence: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)

    # Confidence
    confidence: float = 0.5  # 0.0 to 1.0
    uncertainty_reasons: List[str] = field(default_factory=list)

    # Outcomes
    outcome: Optional[ReasoningOutcome] = None
    actual_result: Optional[str] = None

    # Meta-data
    parent_step_id: Optional[str] = None
    child_step_ids: List[str] = field(default_factory=list)
    domain: str = "general"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "step_id": self.step_id,
            "timestamp": self.timestamp,
            "thought": self.thought,
            "evidence": self.evidence,
            "assumptions": self.assumptions,
            "confidence": self.confidence,
            "uncertainty_reasons": self.uncertainty_reasons,
            "outcome": self.outcome.value if self.outcome else None,
            "actual_result": self.actual_result,
            "parent_step_id": self.parent_step_id,
            "child_step_ids": self.child_step_ids,
            "domain": self.domain,
        }


@dataclass
class ReasoningChain:
    """A complete reasoning chain with steps."""
    chain_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)

    # Content
    goal: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    steps: List[ReasoningStep] = field(default_factory=list)

    # Final outcome
    final_decision: Optional[str] = None
    final_confidence: float = 0.5
    outcome: Optional[ReasoningOutcome] = None

    # Meta-analysis
    total_duration_ms: float = 0.0
    corrections_applied: int = 0
    biases_detected: List[BiasType] = field(default_factory=list)
    blind_spots_found: List[BlindSpotType] = field(default_factory=list)

    def add_step(self, step: ReasoningStep) -> None:
        """Add a step to the chain."""
        if self.steps:
            step.parent_step_id = self.steps[-1].step_id
            self.steps[-1].child_step_ids.append(step.step_id)
        self.steps.append(step)


@dataclass
class BiasDetection:
    """Result of bias detection analysis."""
    bias_type: BiasType
    confidence: float  # How confident we are this bias exists
    evidence: List[str]  # Evidence supporting the detection
    affected_steps: List[str]  # Step IDs affected by this bias
    severity: float  # 0.0 to 1.0
    mitigation_suggestion: str = ""


@dataclass
class BlindSpotDetection:
    """Result of blind spot analysis."""
    blind_spot_type: BlindSpotType
    description: str
    missing_considerations: List[str]
    suggested_questions: List[str]
    confidence: float
    domain: str = "general"


@dataclass
class ConfidenceCalibration:
    """Confidence calibration metrics."""
    predicted_confidence: float
    actual_accuracy: float
    calibration_error: float  # abs(predicted - actual)
    sample_size: int
    time_period_days: int
    domain: str = "general"

    @property
    def is_overconfident(self) -> bool:
        """Check if system is overconfident."""
        return self.predicted_confidence > self.actual_accuracy + 0.1

    @property
    def is_underconfident(self) -> bool:
        """Check if system is underconfident."""
        return self.predicted_confidence < self.actual_accuracy - 0.1


@dataclass
class CapabilityBoundary:
    """Boundary of system capability in a domain."""
    domain: str
    competence_level: float  # 0.0 to 1.0
    success_rate: float
    sample_size: int
    common_failures: List[str]
    recommended_confidence_range: Tuple[float, float]
    last_updated: float = field(default_factory=time.time)


@dataclass
class SelfCorrectionResult:
    """Result of a self-correction attempt."""
    original_decision: str
    corrected_decision: str
    correction_reason: str
    confidence_change: float
    biases_mitigated: List[BiasType]
    blind_spots_addressed: List[BlindSpotType]
    success: bool


@dataclass
class IntrospectionReport:
    """Complete introspection report."""
    report_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    generated_at: float = field(default_factory=time.time)

    # Analyzed data
    chains_analyzed: int = 0
    time_period_days: int = 7

    # Findings
    biases_detected: List[BiasDetection] = field(default_factory=list)
    blind_spots_found: List[BlindSpotDetection] = field(default_factory=list)
    calibration_metrics: List[ConfidenceCalibration] = field(default_factory=list)
    capability_boundaries: List[CapabilityBoundary] = field(default_factory=list)

    # Recommendations
    recommendations: List[str] = field(default_factory=list)
    high_risk_patterns: List[str] = field(default_factory=list)

    # Overall health
    reasoning_health_score: float = 0.5  # 0.0 to 1.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "chains_analyzed": self.chains_analyzed,
            "time_period_days": self.time_period_days,
            "biases_detected": [
                {
                    "type": b.bias_type.value,
                    "confidence": b.confidence,
                    "severity": b.severity,
                    "mitigation": b.mitigation_suggestion,
                }
                for b in self.biases_detected
            ],
            "blind_spots_found": [
                {
                    "type": bs.blind_spot_type.value,
                    "description": bs.description,
                    "missing": bs.missing_considerations,
                }
                for bs in self.blind_spots_found
            ],
            "calibration_metrics": [
                {
                    "domain": c.domain,
                    "predicted": c.predicted_confidence,
                    "actual": c.actual_accuracy,
                    "error": c.calibration_error,
                }
                for c in self.calibration_metrics
            ],
            "capability_boundaries": [
                {
                    "domain": cb.domain,
                    "competence": cb.competence_level,
                    "success_rate": cb.success_rate,
                }
                for cb in self.capability_boundaries
            ],
            "recommendations": self.recommendations,
            "high_risk_patterns": self.high_risk_patterns,
            "reasoning_health_score": self.reasoning_health_score,
        }


class BiasDetector:
    """Detects cognitive biases in reasoning chains."""

    def __init__(self, threshold: float = BIAS_DETECTION_THRESHOLD):
        self.threshold = threshold
        self.logger = logging.getLogger("BiasDetector")

        # Historical data for pattern detection
        self._decision_history: deque = deque(maxlen=1000)
        self._evidence_usage: Dict[str, int] = defaultdict(int)
        self._first_impressions: Dict[str, Any] = {}

    async def detect_biases(
        self,
        chain: ReasoningChain,
        historical_chains: List[ReasoningChain]
    ) -> List[BiasDetection]:
        """Detect all biases in a reasoning chain."""
        detections = []

        # Run all bias detectors in parallel
        detection_tasks = [
            self._detect_recency_bias(chain, historical_chains),
            self._detect_confirmation_bias(chain),
            self._detect_anchoring_bias(chain),
            self._detect_availability_bias(chain, historical_chains),
            self._detect_overconfidence(chain, historical_chains),
            self._detect_underconfidence(chain, historical_chains),
        ]

        results = await asyncio.gather(*detection_tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, BiasDetection) and result.confidence >= self.threshold:
                detections.append(result)
            elif isinstance(result, Exception):
                self.logger.warning(f"Bias detection error: {result}")

        return detections

    async def _detect_recency_bias(
        self,
        chain: ReasoningChain,
        historical_chains: List[ReasoningChain]
    ) -> Optional[BiasDetection]:
        """Detect if recent information is over-weighted."""
        if not chain.steps:
            return None

        # Analyze evidence timestamps
        evidence_ages = []
        total_evidence_weight = 0.0
        recent_evidence_weight = 0.0

        cutoff_time = time.time() - 86400  # 24 hours

        for step in chain.steps:
            for evidence in step.evidence:
                # Assume evidence contains timestamp info or is recent
                # Weight by confidence contribution
                weight = step.confidence
                total_evidence_weight += weight

                # Check if this evidence appears in recent chains more
                recent_usage = sum(
                    1 for hc in historical_chains[-10:]
                    for s in hc.steps
                    if evidence in s.evidence
                )
                if recent_usage > 0:
                    recent_evidence_weight += weight

        if total_evidence_weight == 0:
            return None

        recency_ratio = recent_evidence_weight / total_evidence_weight

        if recency_ratio > 0.8:  # 80%+ weight on recent info
            return BiasDetection(
                bias_type=BiasType.RECENCY,
                confidence=min(recency_ratio, 1.0),
                evidence=[
                    f"Recency ratio: {recency_ratio:.2%}",
                    f"Recent evidence weight: {recent_evidence_weight:.2f}",
                ],
                affected_steps=[s.step_id for s in chain.steps],
                severity=recency_ratio - 0.5,
                mitigation_suggestion="Consider older, established patterns alongside recent data",
            )

        return None

    async def _detect_confirmation_bias(
        self,
        chain: ReasoningChain
    ) -> Optional[BiasDetection]:
        """Detect if contradicting evidence is being ignored."""
        if len(chain.steps) < 2:
            return None

        # Check if early hypothesis aligns with final decision
        early_hypothesis = chain.steps[0].thought if chain.steps else ""
        final_decision = chain.final_decision or ""

        # Check for contradicting evidence that was ignored
        confirming_count = 0
        contradicting_count = 0

        for step in chain.steps:
            # Simple heuristic: if confidence decreased, evidence might be contradicting
            if step.parent_step_id:
                parent = next((s for s in chain.steps if s.step_id == step.parent_step_id), None)
                if parent:
                    if step.confidence < parent.confidence - 0.1:
                        contradicting_count += 1
                    elif step.confidence >= parent.confidence:
                        confirming_count += 1

        if confirming_count + contradicting_count == 0:
            return None

        confirmation_ratio = confirming_count / (confirming_count + contradicting_count)

        if confirmation_ratio > 0.9 and contradicting_count > 0:
            return BiasDetection(
                bias_type=BiasType.CONFIRMATION,
                confidence=confirmation_ratio * 0.8,
                evidence=[
                    f"Confirming/contradicting ratio: {confirming_count}/{contradicting_count}",
                    f"High alignment with initial hypothesis",
                ],
                affected_steps=[s.step_id for s in chain.steps],
                severity=confirmation_ratio - 0.7,
                mitigation_suggestion="Actively seek and consider contradicting evidence",
            )

        return None

    async def _detect_anchoring_bias(
        self,
        chain: ReasoningChain
    ) -> Optional[BiasDetection]:
        """Detect if first information is over-weighted."""
        if len(chain.steps) < 3:
            return None

        # Check if final decision is too similar to first step
        first_step = chain.steps[0]

        # Calculate how much the conclusion changed from first impression
        confidence_drift = abs(chain.final_confidence - first_step.confidence)

        # If confidence barely moved and we had multiple steps, might be anchoring
        if confidence_drift < 0.1 and len(chain.steps) > 5:
            return BiasDetection(
                bias_type=BiasType.ANCHORING,
                confidence=1.0 - confidence_drift,
                evidence=[
                    f"Initial confidence: {first_step.confidence:.2f}",
                    f"Final confidence: {chain.final_confidence:.2f}",
                    f"Steps taken: {len(chain.steps)}",
                ],
                affected_steps=[chain.steps[0].step_id, chain.steps[-1].step_id],
                severity=0.5 - confidence_drift,
                mitigation_suggestion="Re-evaluate conclusion independent of first impression",
            )

        return None

    async def _detect_availability_bias(
        self,
        chain: ReasoningChain,
        historical_chains: List[ReasoningChain]
    ) -> Optional[BiasDetection]:
        """Detect if easily recalled patterns are over-used."""
        if not historical_chains:
            return None

        # Find patterns that appear very frequently
        pattern_frequency: Dict[str, int] = defaultdict(int)

        for hc in historical_chains:
            for step in hc.steps:
                # Use thought as pattern key
                pattern_key = step.thought[:100] if step.thought else ""
                if pattern_key:
                    pattern_frequency[pattern_key] += 1

        # Check if current chain uses high-frequency patterns exclusively
        current_patterns = [s.thought[:100] for s in chain.steps if s.thought]
        if not current_patterns:
            return None

        high_freq_usage = sum(
            1 for p in current_patterns
            if pattern_frequency.get(p, 0) > len(historical_chains) * 0.3
        )

        availability_ratio = high_freq_usage / len(current_patterns)

        if availability_ratio > 0.7:
            return BiasDetection(
                bias_type=BiasType.AVAILABILITY,
                confidence=availability_ratio,
                evidence=[
                    f"High-frequency pattern usage: {availability_ratio:.2%}",
                    f"Unique approaches: {len(current_patterns) - high_freq_usage}",
                ],
                affected_steps=[s.step_id for s in chain.steps],
                severity=availability_ratio - 0.5,
                mitigation_suggestion="Consider less common but potentially relevant approaches",
            )

        return None

    async def _detect_overconfidence(
        self,
        chain: ReasoningChain,
        historical_chains: List[ReasoningChain]
    ) -> Optional[BiasDetection]:
        """Detect if confidence is systematically too high."""
        # Calculate historical calibration
        high_confidence_chains = [
            hc for hc in historical_chains
            if hc.final_confidence > 0.8 and hc.outcome is not None
        ]

        if len(high_confidence_chains) < 10:
            return None

        # Calculate actual success rate for high-confidence decisions
        success_count = sum(
            1 for hc in high_confidence_chains
            if hc.outcome in (ReasoningOutcome.SUCCESS, ReasoningOutcome.PARTIAL_SUCCESS)
        )
        actual_success_rate = success_count / len(high_confidence_chains)

        # If claiming 80%+ confidence but achieving < 70% success
        if actual_success_rate < 0.7 and chain.final_confidence > 0.8:
            return BiasDetection(
                bias_type=BiasType.OVERCONFIDENCE,
                confidence=0.9,
                evidence=[
                    f"Stated confidence: {chain.final_confidence:.2%}",
                    f"Historical success rate at this confidence: {actual_success_rate:.2%}",
                    f"Sample size: {len(high_confidence_chains)}",
                ],
                affected_steps=[chain.steps[-1].step_id] if chain.steps else [],
                severity=chain.final_confidence - actual_success_rate,
                mitigation_suggestion=f"Recommended confidence: {actual_success_rate:.2%}",
            )

        return None

    async def _detect_underconfidence(
        self,
        chain: ReasoningChain,
        historical_chains: List[ReasoningChain]
    ) -> Optional[BiasDetection]:
        """Detect if confidence is systematically too low."""
        # Calculate historical calibration for low-confidence decisions
        low_confidence_chains = [
            hc for hc in historical_chains
            if hc.final_confidence < 0.5 and hc.outcome is not None
        ]

        if len(low_confidence_chains) < 10:
            return None

        # Calculate actual success rate
        success_count = sum(
            1 for hc in low_confidence_chains
            if hc.outcome in (ReasoningOutcome.SUCCESS, ReasoningOutcome.PARTIAL_SUCCESS)
        )
        actual_success_rate = success_count / len(low_confidence_chains)

        # If claiming < 50% confidence but achieving > 70% success
        if actual_success_rate > 0.7 and chain.final_confidence < 0.5:
            return BiasDetection(
                bias_type=BiasType.UNDERCONFIDENCE,
                confidence=0.85,
                evidence=[
                    f"Stated confidence: {chain.final_confidence:.2%}",
                    f"Historical success rate: {actual_success_rate:.2%}",
                    f"Sample size: {len(low_confidence_chains)}",
                ],
                affected_steps=[chain.steps[-1].step_id] if chain.steps else [],
                severity=actual_success_rate - chain.final_confidence,
                mitigation_suggestion=f"Recommended confidence: {actual_success_rate:.2%}",
            )

        return None


class BlindSpotAnalyzer:
    """Analyzes reasoning chains for blind spots and knowledge gaps."""

    def __init__(self, sensitivity: float = BLIND_SPOT_SENSITIVITY):
        self.sensitivity = sensitivity
        self.logger = logging.getLogger("BlindSpotAnalyzer")

        # Track known domains and their coverage
        self._domain_coverage: Dict[str, Set[str]] = defaultdict(set)
        self._failed_assumptions: List[Tuple[str, str]] = []  # (assumption, domain)

    async def analyze_blind_spots(
        self,
        chain: ReasoningChain,
        domain_knowledge: Dict[str, List[str]]
    ) -> List[BlindSpotDetection]:
        """Identify blind spots in reasoning."""
        detections = []

        # Run all analyzers in parallel
        analysis_tasks = [
            self._check_knowledge_gaps(chain, domain_knowledge),
            self._check_assumption_errors(chain),
            self._check_scope_limitations(chain),
            self._check_temporal_blindness(chain),
            self._check_context_insensitivity(chain),
            self._check_edge_case_neglect(chain),
        ]

        results = await asyncio.gather(*analysis_tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, BlindSpotDetection) and result.confidence >= self.sensitivity:
                detections.append(result)
            elif isinstance(result, list):
                detections.extend([
                    d for d in result
                    if isinstance(d, BlindSpotDetection) and d.confidence >= self.sensitivity
                ])
            elif isinstance(result, Exception):
                self.logger.warning(f"Blind spot analysis error: {result}")

        return detections

    async def _check_knowledge_gaps(
        self,
        chain: ReasoningChain,
        domain_knowledge: Dict[str, List[str]]
    ) -> List[BlindSpotDetection]:
        """Check for gaps in domain knowledge coverage."""
        detections = []

        domain = chain.context.get("domain", "general")
        expected_considerations = domain_knowledge.get(domain, [])

        if not expected_considerations:
            return detections

        # Check which expected considerations were addressed
        addressed = set()
        for step in chain.steps:
            thought_lower = step.thought.lower()
            for consideration in expected_considerations:
                if consideration.lower() in thought_lower:
                    addressed.add(consideration)

        missing = set(expected_considerations) - addressed

        if missing and len(missing) > len(expected_considerations) * 0.3:
            detections.append(BlindSpotDetection(
                blind_spot_type=BlindSpotType.KNOWLEDGE_GAP,
                description=f"Missing {len(missing)} key considerations for {domain}",
                missing_considerations=list(missing),
                suggested_questions=[
                    f"What about {c}?" for c in list(missing)[:3]
                ],
                confidence=len(missing) / len(expected_considerations),
                domain=domain,
            ))

        return detections

    async def _check_assumption_errors(
        self,
        chain: ReasoningChain
    ) -> Optional[BlindSpotDetection]:
        """Check for potentially incorrect assumptions."""
        all_assumptions = []
        for step in chain.steps:
            all_assumptions.extend(step.assumptions)

        if not all_assumptions:
            return None

        # Check against known failed assumptions
        risky_assumptions = [
            a for a in all_assumptions
            for fa, _ in self._failed_assumptions
            if self._assumption_similarity(a, fa) > 0.8
        ]

        if risky_assumptions:
            return BlindSpotDetection(
                blind_spot_type=BlindSpotType.ASSUMPTION_ERROR,
                description="Assumptions similar to previously failed ones",
                missing_considerations=[
                    f"Verify assumption: {a}" for a in risky_assumptions
                ],
                suggested_questions=[
                    f"Is '{a}' actually true in this context?" for a in risky_assumptions[:3]
                ],
                confidence=len(risky_assumptions) / len(all_assumptions),
            )

        return None

    def _assumption_similarity(self, a1: str, a2: str) -> float:
        """Calculate similarity between two assumptions."""
        # Simple word overlap similarity
        words1 = set(a1.lower().split())
        words2 = set(a2.lower().split())
        if not words1 or not words2:
            return 0.0
        intersection = words1 & words2
        union = words1 | words2
        return len(intersection) / len(union)

    async def _check_scope_limitations(
        self,
        chain: ReasoningChain
    ) -> Optional[BlindSpotDetection]:
        """Check if reasoning scope is too narrow."""
        if len(chain.steps) < 2:
            return None

        # Check variety of evidence sources
        evidence_sources: Set[str] = set()
        for step in chain.steps:
            for evidence in step.evidence:
                # Extract source type from evidence
                if ":" in evidence:
                    source = evidence.split(":")[0]
                    evidence_sources.add(source)

        # If very few evidence types, scope might be limited
        if len(evidence_sources) < 2 and len(chain.steps) > 3:
            return BlindSpotDetection(
                blind_spot_type=BlindSpotType.SCOPE_LIMITATION,
                description="Reasoning relies on limited evidence sources",
                missing_considerations=[
                    "Consider additional data sources",
                    "Seek diverse perspectives",
                    "Cross-validate with independent sources",
                ],
                suggested_questions=[
                    "What other sources could inform this decision?",
                    "Who else might have relevant input?",
                    "What data haven't we considered?",
                ],
                confidence=0.7 - (len(evidence_sources) * 0.1),
            )

        return None

    async def _check_temporal_blindness(
        self,
        chain: ReasoningChain
    ) -> Optional[BlindSpotDetection]:
        """Check if time-related factors are being ignored."""
        temporal_keywords = [
            "time", "when", "before", "after", "duration", "deadline",
            "schedule", "timing", "historical", "future", "trend",
        ]

        has_temporal_consideration = any(
            any(kw in step.thought.lower() for kw in temporal_keywords)
            for step in chain.steps
        )

        # Check if context suggests temporal relevance
        context_str = json.dumps(chain.context).lower()
        context_suggests_temporal = any(kw in context_str for kw in temporal_keywords)

        if context_suggests_temporal and not has_temporal_consideration:
            return BlindSpotDetection(
                blind_spot_type=BlindSpotType.TEMPORAL_BLINDNESS,
                description="Time-related factors may not be adequately considered",
                missing_considerations=[
                    "Historical patterns",
                    "Future implications",
                    "Timing constraints",
                ],
                suggested_questions=[
                    "How has this changed over time?",
                    "What are the timing implications?",
                    "Are there seasonal or cyclical factors?",
                ],
                confidence=0.6,
            )

        return None

    async def _check_context_insensitivity(
        self,
        chain: ReasoningChain
    ) -> Optional[BlindSpotDetection]:
        """Check if context-specific factors are being ignored."""
        context_keys = set(chain.context.keys())

        # Check how many context factors are mentioned in reasoning
        context_usage_count = 0
        for key in context_keys:
            for step in chain.steps:
                if key.lower() in step.thought.lower():
                    context_usage_count += 1
                    break

        if context_keys and context_usage_count < len(context_keys) * 0.5:
            unused_context = [
                k for k in context_keys
                if not any(k.lower() in s.thought.lower() for s in chain.steps)
            ]

            return BlindSpotDetection(
                blind_spot_type=BlindSpotType.CONTEXT_INSENSITIVITY,
                description="Some context factors may not be considered",
                missing_considerations=unused_context[:5],
                suggested_questions=[
                    f"How does {c} affect the decision?" for c in unused_context[:3]
                ],
                confidence=1.0 - (context_usage_count / len(context_keys)),
            )

        return None

    async def _check_edge_case_neglect(
        self,
        chain: ReasoningChain
    ) -> Optional[BlindSpotDetection]:
        """Check if edge cases are being considered."""
        edge_case_indicators = [
            "edge case", "exception", "unusual", "rare", "corner case",
            "what if", "unless", "however", "but", "except",
        ]

        has_edge_case_thinking = any(
            any(ind in step.thought.lower() for ind in edge_case_indicators)
            for step in chain.steps
        )

        # If high confidence decision without edge case thinking
        if chain.final_confidence > 0.8 and not has_edge_case_thinking:
            return BlindSpotDetection(
                blind_spot_type=BlindSpotType.EDGE_CASE_NEGLECT,
                description="High-confidence decision without explicit edge case consideration",
                missing_considerations=[
                    "Unusual scenarios",
                    "Exception conditions",
                    "Failure modes",
                ],
                suggested_questions=[
                    "What could go wrong?",
                    "What unusual situations might break this?",
                    "What assumptions might fail?",
                ],
                confidence=chain.final_confidence - 0.3,
            )

        return None

    def record_failed_assumption(self, assumption: str, domain: str) -> None:
        """Record an assumption that turned out to be false."""
        self._failed_assumptions.append((assumption, domain))
        # Keep only recent failures
        if len(self._failed_assumptions) > 1000:
            self._failed_assumptions = self._failed_assumptions[-500:]


class SelfCorrectionModule:
    """Applies self-correction to reasoning based on detected issues."""

    def __init__(self, enabled: bool = SELF_CORRECTION_ENABLED):
        self.enabled = enabled
        self.logger = logging.getLogger("SelfCorrectionModule")
        self._correction_history: deque = deque(maxlen=500)

    async def apply_corrections(
        self,
        chain: ReasoningChain,
        biases: List[BiasDetection],
        blind_spots: List[BlindSpotDetection]
    ) -> SelfCorrectionResult:
        """Apply corrections to a reasoning chain."""
        if not self.enabled:
            return SelfCorrectionResult(
                original_decision=chain.final_decision or "",
                corrected_decision=chain.final_decision or "",
                correction_reason="Self-correction disabled",
                confidence_change=0.0,
                biases_mitigated=[],
                blind_spots_addressed=[],
                success=False,
            )

        original_decision = chain.final_decision or ""
        original_confidence = chain.final_confidence
        corrected_decision = original_decision
        corrected_confidence = original_confidence

        biases_mitigated = []
        blind_spots_addressed = []
        correction_reasons = []

        # Apply bias corrections
        for bias in biases:
            if bias.bias_type == BiasType.OVERCONFIDENCE:
                # Reduce confidence based on historical accuracy
                suggested_conf = float(bias.mitigation_suggestion.split(": ")[-1].rstrip("%")) / 100
                corrected_confidence = min(corrected_confidence, suggested_conf + 0.1)
                biases_mitigated.append(bias.bias_type)
                correction_reasons.append(f"Reduced confidence due to overconfidence bias")

            elif bias.bias_type == BiasType.UNDERCONFIDENCE:
                # Increase confidence based on historical accuracy
                suggested_conf = float(bias.mitigation_suggestion.split(": ")[-1].rstrip("%")) / 100
                corrected_confidence = max(corrected_confidence, suggested_conf - 0.1)
                biases_mitigated.append(bias.bias_type)
                correction_reasons.append(f"Increased confidence due to underconfidence bias")

            elif bias.bias_type == BiasType.CONFIRMATION:
                # Add uncertainty due to confirmation bias
                corrected_confidence *= 0.9
                biases_mitigated.append(bias.bias_type)
                correction_reasons.append("Added uncertainty due to confirmation bias")

            elif bias.bias_type == BiasType.ANCHORING:
                # Add uncertainty due to anchoring
                corrected_confidence *= 0.95
                biases_mitigated.append(bias.bias_type)
                correction_reasons.append("Added uncertainty due to anchoring bias")

        # Apply blind spot corrections
        for blind_spot in blind_spots:
            if blind_spot.blind_spot_type == BlindSpotType.EDGE_CASE_NEGLECT:
                # Reduce confidence for high-confidence without edge case thinking
                corrected_confidence = min(corrected_confidence, 0.75)
                blind_spots_addressed.append(blind_spot.blind_spot_type)
                correction_reasons.append("Capped confidence due to edge case neglect")

            elif blind_spot.blind_spot_type == BlindSpotType.KNOWLEDGE_GAP:
                # Reduce confidence proportional to missing knowledge
                reduction = blind_spot.confidence * 0.2
                corrected_confidence -= reduction
                blind_spots_addressed.append(blind_spot.blind_spot_type)
                correction_reasons.append(f"Reduced confidence by {reduction:.1%} due to knowledge gap")

        # Ensure confidence stays in valid range
        corrected_confidence = max(0.1, min(0.99, corrected_confidence))

        # Record correction
        result = SelfCorrectionResult(
            original_decision=original_decision,
            corrected_decision=corrected_decision,
            correction_reason="; ".join(correction_reasons) if correction_reasons else "No corrections needed",
            confidence_change=corrected_confidence - original_confidence,
            biases_mitigated=biases_mitigated,
            blind_spots_addressed=blind_spots_addressed,
            success=bool(biases_mitigated or blind_spots_addressed),
        )

        self._correction_history.append({
            "timestamp": time.time(),
            "chain_id": chain.chain_id,
            "original_confidence": original_confidence,
            "corrected_confidence": corrected_confidence,
            "biases": [b.value for b in biases_mitigated],
            "blind_spots": [bs.value for bs in blind_spots_addressed],
        })

        return result


class CapabilityAssessor:
    """Assesses system capabilities and boundaries."""

    def __init__(self):
        self.logger = logging.getLogger("CapabilityAssessor")
        self._domain_performance: Dict[str, List[Tuple[float, bool]]] = defaultdict(list)
        self._capability_cache: Dict[str, CapabilityBoundary] = {}

    async def assess_capabilities(
        self,
        historical_chains: List[ReasoningChain],
        domains: List[str]
    ) -> List[CapabilityBoundary]:
        """Assess capability boundaries for each domain."""
        boundaries = []

        for domain in domains:
            # Filter chains by domain
            domain_chains = [
                c for c in historical_chains
                if c.context.get("domain") == domain and c.outcome is not None
            ]

            if len(domain_chains) < 5:
                continue

            # Calculate metrics
            successes = [
                c for c in domain_chains
                if c.outcome in (ReasoningOutcome.SUCCESS, ReasoningOutcome.PARTIAL_SUCCESS)
            ]
            success_rate = len(successes) / len(domain_chains)

            # Calculate average confidence vs success
            avg_confidence = statistics.mean(c.final_confidence for c in domain_chains)

            # Identify common failures
            failures = [
                c for c in domain_chains
                if c.outcome in (ReasoningOutcome.FAILURE, ReasoningOutcome.TIMEOUT)
            ]
            common_failures = self._identify_common_patterns(failures)

            # Calculate recommended confidence range
            success_confidences = [c.final_confidence for c in successes]
            if success_confidences:
                min_conf = max(0.3, min(success_confidences) - 0.1)
                max_conf = min(0.95, max(success_confidences) + 0.05)
            else:
                min_conf, max_conf = 0.3, 0.6

            boundary = CapabilityBoundary(
                domain=domain,
                competence_level=success_rate,
                success_rate=success_rate,
                sample_size=len(domain_chains),
                common_failures=common_failures[:5],
                recommended_confidence_range=(min_conf, max_conf),
            )

            boundaries.append(boundary)
            self._capability_cache[domain] = boundary

        return boundaries

    def _identify_common_patterns(self, chains: List[ReasoningChain]) -> List[str]:
        """Identify common patterns in failed chains."""
        patterns: Dict[str, int] = defaultdict(int)

        for chain in chains:
            # Look for common themes in failures
            if chain.biases_detected:
                for bias in chain.biases_detected:
                    patterns[f"Bias: {bias.value}"] += 1

            if chain.blind_spots_found:
                for bs in chain.blind_spots_found:
                    patterns[f"Blind spot: {bs.value}"] += 1

            # Analyze final step assumptions
            if chain.steps:
                for assumption in chain.steps[-1].assumptions:
                    # Truncate long assumptions
                    key = f"Assumption: {assumption[:50]}"
                    patterns[key] += 1

        # Sort by frequency
        sorted_patterns = sorted(patterns.items(), key=lambda x: x[1], reverse=True)
        return [p[0] for p in sorted_patterns]

    def get_recommended_confidence(self, domain: str) -> Tuple[float, float]:
        """Get recommended confidence range for a domain."""
        if domain in self._capability_cache:
            return self._capability_cache[domain].recommended_confidence_range
        return (0.3, 0.7)  # Default conservative range


class MetaCognitiveEngine:
    """
    Main engine for meta-cognitive capabilities.

    Provides self-awareness, introspection, and self-correction for JARVIS.
    """

    def __init__(self):
        self.logger = logging.getLogger("MetaCognitiveEngine")

        # Initialize components
        self.bias_detector = BiasDetector()
        self.blind_spot_analyzer = BlindSpotAnalyzer()
        self.self_correction = SelfCorrectionModule()
        self.capability_assessor = CapabilityAssessor()

        # Storage
        self._reasoning_chains: deque = deque(maxlen=1000)
        self._introspection_reports: deque = deque(maxlen=100)

        # Domain knowledge (can be extended)
        self._domain_knowledge: Dict[str, List[str]] = {
            "security": [
                "authentication", "authorization", "encryption",
                "threat model", "attack vectors", "vulnerabilities",
            ],
            "voice": [
                "speaker verification", "noise handling", "audio quality",
                "anti-spoofing", "confidence calibration", "user experience",
            ],
            "automation": [
                "error handling", "timeout management", "retry logic",
                "state management", "rollback strategy", "user feedback",
            ],
        }

        # Background task
        self._introspection_task: Optional[asyncio.Task] = None
        self._running = False

        # Ensure data directory exists
        META_COGNITIVE_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the meta-cognitive engine."""
        if self._running:
            return

        self._running = True
        self.logger.info("MetaCognitiveEngine starting...")

        # Load historical data
        await self._load_historical_data()

        # Start background introspection
        self._introspection_task = asyncio.create_task(self._introspection_loop())

        self.logger.info("MetaCognitiveEngine started")

    async def stop(self) -> None:
        """Stop the meta-cognitive engine."""
        self._running = False

        if self._introspection_task:
            self._introspection_task.cancel()
            try:
                await self._introspection_task
            except asyncio.CancelledError:
                pass

        # Save data
        await self._save_historical_data()

        self.logger.info("MetaCognitiveEngine stopped")

    async def record_reasoning_chain(self, chain: ReasoningChain) -> None:
        """Record a completed reasoning chain for analysis."""
        self._reasoning_chains.append(chain)

        # Persist periodically
        if len(self._reasoning_chains) % 10 == 0:
            await self._save_historical_data()

    async def analyze_reasoning(
        self,
        chain: ReasoningChain,
        level: IntrospectionLevel = IntrospectionLevel.MODERATE
    ) -> Tuple[List[BiasDetection], List[BlindSpotDetection], Optional[SelfCorrectionResult]]:
        """Analyze a reasoning chain for biases and blind spots."""
        historical = list(self._reasoning_chains)

        # Detect biases
        biases = await self.bias_detector.detect_biases(chain, historical)

        # Analyze blind spots
        blind_spots = await self.blind_spot_analyzer.analyze_blind_spots(
            chain, self._domain_knowledge
        )

        # Apply self-correction if issues found
        correction = None
        if biases or blind_spots:
            correction = await self.self_correction.apply_corrections(
                chain, biases, blind_spots
            )

            # Update chain
            chain.biases_detected = [b.bias_type for b in biases]
            chain.blind_spots_found = [bs.blind_spot_type for bs in blind_spots]
            if correction.success:
                chain.corrections_applied += 1

        return biases, blind_spots, correction

    async def get_recommended_confidence(
        self,
        domain: str,
        initial_confidence: float
    ) -> float:
        """Get calibrated confidence based on historical performance."""
        min_conf, max_conf = self.capability_assessor.get_recommended_confidence(domain)

        # Adjust initial confidence to fit within recommended range
        calibrated = max(min_conf, min(max_conf, initial_confidence))

        return calibrated

    async def generate_introspection_report(
        self,
        time_period_days: int = 7
    ) -> IntrospectionReport:
        """Generate a comprehensive introspection report."""
        cutoff = time.time() - (time_period_days * 86400)

        # Filter chains within time period
        recent_chains = [
            c for c in self._reasoning_chains
            if c.created_at >= cutoff
        ]

        report = IntrospectionReport(
            chains_analyzed=len(recent_chains),
            time_period_days=time_period_days,
        )

        if not recent_chains:
            report.recommendations.append("Insufficient data for analysis")
            return report

        # Aggregate biases
        all_biases: List[BiasDetection] = []
        all_blind_spots: List[BlindSpotDetection] = []

        for chain in recent_chains:
            biases, blind_spots, _ = await self.analyze_reasoning(chain)
            all_biases.extend(biases)
            all_blind_spots.extend(blind_spots)

        # Aggregate by type
        bias_counts: Dict[BiasType, int] = defaultdict(int)
        for bias in all_biases:
            bias_counts[bias.bias_type] += 1

        # Report most common biases
        for bias_type, count in sorted(bias_counts.items(), key=lambda x: x[1], reverse=True):
            if count >= 3:  # At least 3 occurrences
                report.biases_detected.append(BiasDetection(
                    bias_type=bias_type,
                    confidence=count / len(recent_chains),
                    evidence=[f"Detected {count} times in {len(recent_chains)} chains"],
                    affected_steps=[],
                    severity=count / len(recent_chains),
                ))

        # Report most common blind spots
        blind_spot_counts: Dict[BlindSpotType, int] = defaultdict(int)
        for bs in all_blind_spots:
            blind_spot_counts[bs.blind_spot_type] += 1

        for bs_type, count in sorted(blind_spot_counts.items(), key=lambda x: x[1], reverse=True):
            if count >= 3:
                report.blind_spots_found.append(BlindSpotDetection(
                    blind_spot_type=bs_type,
                    description=f"Occurred {count} times",
                    missing_considerations=[],
                    suggested_questions=[],
                    confidence=count / len(recent_chains),
                ))

        # Calculate calibration
        domains = list(set(c.context.get("domain", "general") for c in recent_chains))
        for domain in domains:
            domain_chains = [c for c in recent_chains if c.context.get("domain") == domain]
            if len(domain_chains) >= 5:
                avg_confidence = statistics.mean(c.final_confidence for c in domain_chains)
                success_rate = sum(
                    1 for c in domain_chains
                    if c.outcome in (ReasoningOutcome.SUCCESS, ReasoningOutcome.PARTIAL_SUCCESS)
                ) / len(domain_chains)

                report.calibration_metrics.append(ConfidenceCalibration(
                    predicted_confidence=avg_confidence,
                    actual_accuracy=success_rate,
                    calibration_error=abs(avg_confidence - success_rate),
                    sample_size=len(domain_chains),
                    time_period_days=time_period_days,
                    domain=domain,
                ))

        # Assess capabilities
        report.capability_boundaries = await self.capability_assessor.assess_capabilities(
            recent_chains, domains
        )

        # Generate recommendations
        if report.biases_detected:
            top_bias = report.biases_detected[0]
            report.recommendations.append(
                f"Address {top_bias.bias_type.value} bias (severity: {top_bias.severity:.1%})"
            )

        if report.blind_spots_found:
            top_bs = report.blind_spots_found[0]
            report.recommendations.append(
                f"Improve {top_bs.blind_spot_type.value} awareness"
            )

        for cal in report.calibration_metrics:
            if cal.is_overconfident:
                report.recommendations.append(
                    f"Reduce confidence in {cal.domain} domain (calibration error: {cal.calibration_error:.1%})"
                )
            elif cal.is_underconfident:
                report.recommendations.append(
                    f"Increase confidence in {cal.domain} domain (calibration error: {cal.calibration_error:.1%})"
                )

        # Calculate overall health score
        total_issues = len(report.biases_detected) + len(report.blind_spots_found)
        avg_calibration_error = statistics.mean(
            c.calibration_error for c in report.calibration_metrics
        ) if report.calibration_metrics else 0.5

        report.reasoning_health_score = max(0.1, 1.0 - (total_issues * 0.1) - avg_calibration_error)

        # Store report
        self._introspection_reports.append(report)

        return report

    async def _introspection_loop(self) -> None:
        """Background loop for periodic introspection."""
        while self._running:
            try:
                await asyncio.sleep(INTROSPECTION_INTERVAL_SECONDS)

                if self._reasoning_chains:
                    report = await self.generate_introspection_report()

                    if report.reasoning_health_score < 0.5:
                        self.logger.warning(
                            f"Low reasoning health score: {report.reasoning_health_score:.1%}"
                        )
                        for rec in report.recommendations:
                            self.logger.warning(f"  Recommendation: {rec}")
                    else:
                        self.logger.info(
                            f"Introspection complete: health score {report.reasoning_health_score:.1%}"
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Introspection loop error: {e}")
                await asyncio.sleep(60)  # Wait before retry

    async def _load_historical_data(self) -> None:
        """Load historical reasoning chains from disk."""
        chains_file = META_COGNITIVE_DATA_DIR / "reasoning_chains.json"

        if chains_file.exists():
            try:
                with open(chains_file) as f:
                    data = json.load(f)

                for chain_data in data[-500:]:  # Load last 500
                    chain = ReasoningChain(
                        chain_id=chain_data.get("chain_id", str(uuid.uuid4())),
                        created_at=chain_data.get("created_at", time.time()),
                        goal=chain_data.get("goal", ""),
                        context=chain_data.get("context", {}),
                        final_decision=chain_data.get("final_decision"),
                        final_confidence=chain_data.get("final_confidence", 0.5),
                    )
                    if chain_data.get("outcome"):
                        chain.outcome = ReasoningOutcome(chain_data["outcome"])

                    self._reasoning_chains.append(chain)

                self.logger.info(f"Loaded {len(self._reasoning_chains)} historical chains")
            except Exception as e:
                self.logger.warning(f"Failed to load historical data: {e}")

    async def _save_historical_data(self) -> None:
        """Save reasoning chains to disk."""
        chains_file = META_COGNITIVE_DATA_DIR / "reasoning_chains.json"

        try:
            data = [
                {
                    "chain_id": c.chain_id,
                    "created_at": c.created_at,
                    "goal": c.goal,
                    "context": c.context,
                    "final_decision": c.final_decision,
                    "final_confidence": c.final_confidence,
                    "outcome": c.outcome.value if c.outcome else None,
                }
                for c in self._reasoning_chains
            ]

            with open(chains_file, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            self.logger.error(f"Failed to save historical data: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        return {
            "total_chains": len(self._reasoning_chains),
            "total_reports": len(self._introspection_reports),
            "running": self._running,
            "domains_tracked": list(self._domain_knowledge.keys()),
        }


# Global instance
_meta_cognitive_engine: Optional[MetaCognitiveEngine] = None
_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_meta_cognitive_engine() -> MetaCognitiveEngine:
    """Get the global MetaCognitiveEngine instance."""
    global _meta_cognitive_engine

    async with _lock:
        if _meta_cognitive_engine is None:
            _meta_cognitive_engine = MetaCognitiveEngine()
            await _meta_cognitive_engine.start()

        return _meta_cognitive_engine
