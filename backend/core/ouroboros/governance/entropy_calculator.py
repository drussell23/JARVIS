"""
Entropy Calculator — Shannon-inspired composite ignorance measurement.

Pillar 4 (Synthetic Soul): "When it mathematically detects a gap in its own
knowledge via Shannon Entropy, it 'feels' this ignorance and signals the
biological drive to evolve."

This module computes a CompositeEntropySignal from two deterministic sources:

  ACUTE IGNORANCE (per-generation):
    Variance in the current generation's quality signals — validation
    pass/fail, critique severity distribution, shadow harness confidence.
    Measures: "How uncertain is THIS generation?"

  CHRONIC IGNORANCE (historical domain):
    Failure rate for the current capability domain from LearningBridge
    history. Measures: "How uncertain are we IN GENERAL about this
    type of task?"

  SYSTEMIC ENTROPY SCORE = f(acute, chronic) in [0.0, 1.0]

The 4-Quadrant Decision Matrix:
  High Acute + High Chronic = IMMEDIATE TRIGGER (we're failing and always fail)
  High Acute + Low Chronic  = WARNING/RETRY (usually good, bad prompt?)
  Low Acute  + High Chronic = FALSE CONFIDENCE (looks good but we usually fail)
  Low Acute  + Low Chronic  = HEALTHY (no action needed)

Boundary Principle:
  ALL computation here is deterministic. Shannon entropy H(X) = -Sum(p*log2(p))
  is a mathematical function over observed frequencies. No model inference.
  The DECISION of what to do with the score is deterministic (threshold-based).
  The RESPONSE to a CognitiveInefficiencyEvent is agentic (handled by Ouroboros).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds — deterministic policy, env-overridable
# ---------------------------------------------------------------------------

import os

_ACUTE_THRESHOLD = float(os.environ.get("JARVIS_ENTROPY_ACUTE_THRESHOLD", "0.6"))
_CHRONIC_THRESHOLD = float(os.environ.get("JARVIS_ENTROPY_CHRONIC_THRESHOLD", "0.5"))
_SYSTEMIC_TRIGGER_THRESHOLD = float(
    os.environ.get("JARVIS_ENTROPY_SYSTEMIC_THRESHOLD", "0.7")
)
# Minimum historical observations before chronic signal is meaningful
_CHRONIC_MIN_OBSERVATIONS = int(
    os.environ.get("JARVIS_ENTROPY_CHRONIC_MIN_OBS", "3")
)
# Weight balance between acute and chronic in the composite
_ACUTE_WEIGHT = float(os.environ.get("JARVIS_ENTROPY_ACUTE_WEIGHT", "0.6"))
_CHRONIC_WEIGHT = float(os.environ.get("JARVIS_ENTROPY_CHRONIC_WEIGHT", "0.4"))


class EntropyQuadrant(str, Enum):
    """Decision quadrant from the composite entropy signal."""
    HEALTHY = "healthy"                     # Low acute, low chronic
    WARNING_RETRY = "warning_retry"         # High acute, low chronic
    FALSE_CONFIDENCE = "false_confidence"   # Low acute, high chronic
    IMMEDIATE_TRIGGER = "immediate_trigger" # High acute, high chronic


@dataclass(frozen=True)
class AcuteEntropySignal:
    """Per-generation uncertainty measurement.

    Computed from the CURRENT operation's quality indicators:
    - Validation pass/fail
    - Critique severity distribution (Shannon entropy over ERROR/WARNING/INFO)
    - Shadow harness confidence score
    - Number of retry attempts consumed
    """
    validation_passed: bool
    critique_entropy: float        # H(severity distribution) in [0, log2(3)]
    shadow_confidence: float       # [0.0, 1.0], 1.0 = perfect match
    retry_fraction: float          # retries_used / max_retries, [0.0, 1.0]
    normalized_score: float        # Combined acute score in [0.0, 1.0]


@dataclass(frozen=True)
class ChronicEntropySignal:
    """Historical domain uncertainty measurement.

    Computed from LearningBridge outcome history for the capability domain:
    - Failure rate over recent operations
    - Shannon entropy over outcome distribution (success/fail/noop)
    - Domain identified by (goal_category, primary_target_file_extension)
    """
    domain_key: str                # e.g., "code_gen::.py" or "refactor::.ts"
    total_observations: int
    failure_count: int
    success_count: int
    noop_count: int
    outcome_entropy: float         # H(outcome distribution), max = log2(3)
    failure_rate: float            # failure_count / total, [0.0, 1.0]
    normalized_score: float        # Combined chronic score in [0.0, 1.0]


@dataclass(frozen=True)
class CognitiveInefficiencyEvent:
    """Emitted when systemic entropy exceeds threshold.

    Consumed by the CapabilityGapSensor or directly by the orchestrator
    to trigger Ouroboros neuroplasticity (Pillar 6).
    """
    op_id: str
    domain_key: str
    systemic_score: float          # [0.0, 1.0]
    acute_score: float
    chronic_score: float
    quadrant: EntropyQuadrant
    recommendation: str            # Human-readable action recommendation
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class CompositeEntropySignal:
    """The fused systemic ignorance measurement."""
    acute: AcuteEntropySignal
    chronic: ChronicEntropySignal
    systemic_score: float          # [0.0, 1.0]
    quadrant: EntropyQuadrant
    should_trigger: bool           # systemic_score >= threshold


# ---------------------------------------------------------------------------
# Pure math — Shannon entropy and signal computation
# ---------------------------------------------------------------------------

def shannon_entropy(frequencies: Tuple[int, ...]) -> float:
    """Compute Shannon entropy H(X) = -Sum(p * log2(p)) over a frequency distribution.

    Parameters
    ----------
    frequencies:
        Tuple of non-negative integer counts (e.g., (5, 3, 2) for 3 categories).

    Returns
    -------
    float
        Entropy in bits. 0.0 = perfect certainty (one category dominates).
        log2(N) = maximum uncertainty (uniform distribution over N categories).
    """
    total = sum(frequencies)
    if total == 0:
        return 0.0

    entropy = 0.0
    for count in frequencies:
        if count > 0:
            p = count / total
            entropy -= p * math.log2(p)

    return entropy


def normalize_entropy(raw_entropy: float, max_categories: int) -> float:
    """Normalize entropy to [0.0, 1.0] by dividing by max possible entropy.

    max entropy = log2(max_categories) when distribution is uniform.
    """
    if max_categories <= 1:
        return 0.0
    max_entropy = math.log2(max_categories)
    if max_entropy == 0:
        return 0.0
    return min(1.0, raw_entropy / max_entropy)


# ---------------------------------------------------------------------------
# Acute Ignorance Signal — per-generation
# ---------------------------------------------------------------------------

def compute_acute_signal(
    validation_passed: bool,
    critique_errors: int = 0,
    critique_warnings: int = 0,
    critique_infos: int = 0,
    shadow_confidence: float = 1.0,
    retries_used: int = 0,
    max_retries: int = 3,
) -> AcuteEntropySignal:
    """Compute acute (per-generation) entropy from current operation quality signals.

    All inputs are deterministic observations from the pipeline.
    No model inference.

    The acute score combines 4 independent signals:
      1. Validation outcome (binary: 0.0 or 1.0)
      2. Critique severity entropy (distribution over ERROR/WARNING/INFO)
      3. Shadow confidence (inverse: low confidence = high entropy)
      4. Retry exhaustion (fraction of retries consumed)

    Weighted combination:
      acute = 0.35 * validation_penalty
            + 0.25 * critique_entropy_normalized
            + 0.20 * (1 - shadow_confidence)
            + 0.20 * retry_fraction
    """
    # 1. Validation penalty: binary signal
    validation_penalty = 0.0 if validation_passed else 1.0

    # 2. Critique severity entropy: H(ERROR, WARNING, INFO)
    critique_freqs = (critique_errors, critique_warnings, critique_infos)
    raw_critique_entropy = shannon_entropy(critique_freqs)
    critique_entropy_norm = normalize_entropy(raw_critique_entropy, 3)

    # If there are only errors and no warnings/infos, that's actually
    # low entropy (certain it's bad) but high severity. Weight the
    # raw error count into the signal.
    total_critiques = sum(critique_freqs)
    if total_critiques > 0:
        error_dominance = critique_errors / total_critiques
        # Blend: entropy captures uncertainty, error_dominance captures severity
        critique_signal = 0.5 * critique_entropy_norm + 0.5 * error_dominance
    else:
        critique_signal = 0.0

    # 3. Shadow confidence (inverse: low confidence = high uncertainty)
    shadow_uncertainty = 1.0 - max(0.0, min(1.0, shadow_confidence))

    # 4. Retry exhaustion
    retry_fraction = min(1.0, retries_used / max(1, max_retries))

    # Weighted combination
    acute_score = (
        0.35 * validation_penalty
        + 0.25 * critique_signal
        + 0.20 * shadow_uncertainty
        + 0.20 * retry_fraction
    )

    return AcuteEntropySignal(
        validation_passed=validation_passed,
        critique_entropy=raw_critique_entropy,
        shadow_confidence=shadow_confidence,
        retry_fraction=retry_fraction,
        normalized_score=min(1.0, acute_score),
    )


# ---------------------------------------------------------------------------
# Chronic Ignorance Signal — historical domain
# ---------------------------------------------------------------------------

def compute_chronic_signal(
    domain_key: str,
    outcomes: List[str],
) -> ChronicEntropySignal:
    """Compute chronic (historical) entropy for a capability domain.

    Parameters
    ----------
    domain_key:
        Identifier for the capability domain (e.g., "code_gen::.py").
    outcomes:
        List of outcome strings from LearningBridge history.
        Expected values: "success", "failed", "noop", or OperationState names.

    The chronic score combines:
      1. Failure rate (direct measure of historical incompetence)
      2. Outcome entropy (Shannon entropy over success/fail/noop distribution)

    Weighted: chronic = 0.6 * failure_rate + 0.4 * outcome_entropy_normalized
    """
    total = len(outcomes)
    if total < _CHRONIC_MIN_OBSERVATIONS:
        # Insufficient data — return neutral (no signal, not zero)
        return ChronicEntropySignal(
            domain_key=domain_key,
            total_observations=total,
            failure_count=0,
            success_count=0,
            noop_count=0,
            outcome_entropy=0.0,
            failure_rate=0.0,
            normalized_score=0.0,
        )

    # Count outcomes
    success_count = sum(1 for o in outcomes if o in ("success", "APPLIED", "COMPLETE"))
    failure_count = sum(1 for o in outcomes if o in ("failed", "FAILED", "CANCELLED"))
    noop_count = total - success_count - failure_count

    # Shannon entropy over 3-category distribution
    outcome_freqs = (success_count, failure_count, noop_count)
    raw_entropy = shannon_entropy(outcome_freqs)
    entropy_norm = normalize_entropy(raw_entropy, 3)

    # Failure rate
    failure_rate = failure_count / total

    # Weighted combination
    chronic_score = 0.6 * failure_rate + 0.4 * entropy_norm

    return ChronicEntropySignal(
        domain_key=domain_key,
        total_observations=total,
        failure_count=failure_count,
        success_count=success_count,
        noop_count=noop_count,
        outcome_entropy=raw_entropy,
        failure_rate=failure_rate,
        normalized_score=min(1.0, chronic_score),
    )


# ---------------------------------------------------------------------------
# Composite fusion — the Shannon Synthesis
# ---------------------------------------------------------------------------

def compute_systemic_entropy(
    acute: AcuteEntropySignal,
    chronic: ChronicEntropySignal,
) -> CompositeEntropySignal:
    """Fuse acute and chronic signals into a SystemicEntropyScore.

    The Math:
      systemic = (ACUTE_WEIGHT * acute.normalized_score)
               + (CHRONIC_WEIGHT * chronic.normalized_score)

    Default weights: 0.6 acute + 0.4 chronic
    (Acute dominates because immediate failure is more actionable than
    historical trends. The chronic signal modulates — it can elevate a
    borderline acute signal or suppress a false positive.)

    The 4-Quadrant Decision Matrix:
      acute >= threshold AND chronic >= threshold → IMMEDIATE_TRIGGER
      acute >= threshold AND chronic <  threshold → WARNING_RETRY
      acute <  threshold AND chronic >= threshold → FALSE_CONFIDENCE
      acute <  threshold AND chronic <  threshold → HEALTHY
    """
    systemic_score = (
        _ACUTE_WEIGHT * acute.normalized_score
        + _CHRONIC_WEIGHT * chronic.normalized_score
    )
    systemic_score = min(1.0, systemic_score)

    # Quadrant classification
    acute_high = acute.normalized_score >= _ACUTE_THRESHOLD
    chronic_high = chronic.normalized_score >= _CHRONIC_THRESHOLD

    if acute_high and chronic_high:
        quadrant = EntropyQuadrant.IMMEDIATE_TRIGGER
    elif acute_high and not chronic_high:
        quadrant = EntropyQuadrant.WARNING_RETRY
    elif not acute_high and chronic_high:
        quadrant = EntropyQuadrant.FALSE_CONFIDENCE
    else:
        quadrant = EntropyQuadrant.HEALTHY

    return CompositeEntropySignal(
        acute=acute,
        chronic=chronic,
        systemic_score=systemic_score,
        quadrant=quadrant,
        should_trigger=systemic_score >= _SYSTEMIC_TRIGGER_THRESHOLD,
    )


# ---------------------------------------------------------------------------
# Event emission — bridge to Ouroboros neuroplasticity
# ---------------------------------------------------------------------------

_QUADRANT_RECOMMENDATIONS: Dict[EntropyQuadrant, str] = {
    EntropyQuadrant.HEALTHY: "No action needed. System operating within normal parameters.",
    EntropyQuadrant.WARNING_RETRY: (
        "Current generation is uncertain but historical performance is strong. "
        "Retry with adjusted prompt or escalate to higher-capability provider."
    ),
    EntropyQuadrant.FALSE_CONFIDENCE: (
        "Current generation appears successful but domain has high historical "
        "failure rate. Route to sandbox validation before applying. "
        "Do not trust the output at face value."
    ),
    EntropyQuadrant.IMMEDIATE_TRIGGER: (
        "Both current and historical signals indicate capability gap. "
        "Trigger Ouroboros neuroplasticity: synthesize new capability, "
        "escalate to Tier 0, or flag for human review."
    ),
}


def build_cognitive_inefficiency_event(
    op_id: str,
    composite: CompositeEntropySignal,
) -> CognitiveInefficiencyEvent:
    """Build a CognitiveInefficiencyEvent from a composite signal.

    This event is consumed by the GapSignalBus to trigger Pillar 6
    (Neuroplasticity) when the system detects its own ignorance.
    """
    return CognitiveInefficiencyEvent(
        op_id=op_id,
        domain_key=composite.chronic.domain_key,
        systemic_score=composite.systemic_score,
        acute_score=composite.acute.normalized_score,
        chronic_score=composite.chronic.normalized_score,
        quadrant=composite.quadrant,
        recommendation=_QUADRANT_RECOMMENDATIONS[composite.quadrant],
    )


# ---------------------------------------------------------------------------
# Domain key extraction — deterministic classification
# ---------------------------------------------------------------------------

def extract_domain_key(
    target_files: Tuple[str, ...],
    description: str = "",
) -> str:
    """Extract a capability domain key from operation context.

    The domain key groups operations into capability categories for
    chronic entropy tracking. Deterministic — based on file extensions
    and structural patterns, not semantic analysis.

    Format: "{category}::{primary_extension}"
    Examples: "code_gen::.py", "config::.yaml", "test::.py"
    """
    if not target_files:
        return "unknown::unknown"

    # Primary extension from first target file
    primary = target_files[0]
    ext = "." + primary.rsplit(".", 1)[-1] if "." in primary else ".unknown"

    # Category from path structure (deterministic)
    if any("test" in f.lower() for f in target_files):
        category = "test_fix"
    elif any(f.endswith((".yaml", ".yml", ".json", ".toml", ".env")) for f in target_files):
        category = "config"
    elif any("requirements" in f.lower() for f in target_files):
        category = "dependency"
    elif any(f.endswith((".md", ".rst", ".txt")) for f in target_files):
        category = "documentation"
    else:
        category = "code_gen"

    return f"{category}::{ext}"
