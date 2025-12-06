"""
Bayesian Confidence Fusion for Physics-Aware Voice Authentication v2.6

This module implements Bayesian probability fusion for combining multiple
evidence sources in voice authentication decisions.

v2.6 Enhancements:
- Adaptive exclusion of unavailable evidence (confidence <= MIN_VALID_CONFIDENCE)
- Weight renormalization when sources are excluded
- Dynamic threshold adjustment based on available evidence
- Graceful degradation mode with comprehensive diagnostics

Evidence sources:

- ML confidence (voice biometric embedding similarity)
- Physics confidence (VTL, RT60, Doppler analysis)
- Behavioral confidence (time patterns, location, device)
- Context confidence (environmental factors, recent activity)

The Bayesian approach allows:
- Proper uncertainty quantification
- Evidence-weighted decision making
- Principled combination of heterogeneous confidence scores
- Adaptive thresholds based on prior probabilities

Theory:
    P(authentic|evidence) = P(evidence|authentic) * P(authentic) / P(evidence)

Where:
    P(authentic) = Prior probability (historical auth success rate)
    P(evidence|authentic) = Likelihood of seeing this evidence if authentic
    P(evidence) = Marginal probability (normalization)
"""

import logging
import os
import math
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration - Environment-Driven
# =============================================================================

class BayesianFusionConfig:
    """Configuration for Bayesian confidence fusion from environment."""

    # Prior probabilities (calibrated from historical data)
    PRIOR_AUTHENTIC = float(os.getenv("BAYESIAN_PRIOR_AUTHENTIC", "0.85"))
    PRIOR_SPOOF = float(os.getenv("BAYESIAN_PRIOR_SPOOF", "0.15"))

    # Evidence weights (sum should equal 1.0)
    ML_WEIGHT = float(os.getenv("BAYESIAN_ML_WEIGHT", "0.40"))
    PHYSICS_WEIGHT = float(os.getenv("BAYESIAN_PHYSICS_WEIGHT", "0.30"))
    BEHAVIORAL_WEIGHT = float(os.getenv("BAYESIAN_BEHAVIORAL_WEIGHT", "0.20"))
    CONTEXT_WEIGHT = float(os.getenv("BAYESIAN_CONTEXT_WEIGHT", "0.10"))

    # Decision thresholds
    AUTHENTICATE_THRESHOLD = float(os.getenv("BAYESIAN_AUTH_THRESHOLD", "0.85"))
    REJECT_THRESHOLD = float(os.getenv("BAYESIAN_REJECT_THRESHOLD", "0.40"))
    CHALLENGE_RANGE = (
        float(os.getenv("BAYESIAN_CHALLENGE_LOW", "0.40")),
        float(os.getenv("BAYESIAN_CHALLENGE_HIGH", "0.85"))
    )

    # Adaptive learning
    LEARNING_ENABLED = os.getenv("BAYESIAN_LEARNING_ENABLED", "true").lower() == "true"
    PRIOR_UPDATE_RATE = float(os.getenv("BAYESIAN_PRIOR_UPDATE_RATE", "0.01"))
    MIN_PRIOR = float(os.getenv("BAYESIAN_MIN_PRIOR", "0.05"))
    MAX_PRIOR = float(os.getenv("BAYESIAN_MAX_PRIOR", "0.95"))

    # Adaptive Exclusion Configuration (v2.6)
    # Minimum valid confidence - below this, evidence is considered unavailable
    MIN_VALID_CONFIDENCE = float(os.getenv("BAYESIAN_MIN_VALID_CONFIDENCE", "0.02"))

    # Enable adaptive exclusion of unavailable evidence sources
    ADAPTIVE_EXCLUSION_ENABLED = os.getenv("BAYESIAN_ADAPTIVE_EXCLUSION", "true").lower() == "true"

    # Threshold adjustment when ML is unavailable (reduce requirement)
    ML_UNAVAILABLE_THRESHOLD_REDUCTION = float(os.getenv("BAYESIAN_ML_UNAVAILABLE_REDUCTION", "0.10"))

    # Minimum sources required for authentication (graceful degradation)
    MIN_SOURCES_FOR_AUTH = int(os.getenv("BAYESIAN_MIN_SOURCES", "2"))

    # Weight redistribution strategy: "proportional" or "equal"
    WEIGHT_REDISTRIBUTION = os.getenv("BAYESIAN_WEIGHT_REDISTRIBUTION", "proportional")


class DecisionType(str, Enum):
    """Authentication decision types."""
    AUTHENTICATE = "authenticate"  # High confidence - grant access
    REJECT = "reject"  # High confidence - deny access
    CHALLENGE = "challenge"  # Medium confidence - require additional verification
    ESCALATE = "escalate"  # Unusual pattern - notify security


@dataclass
class EvidenceScore:
    """Individual evidence score with metadata."""
    source: str  # ml, physics, behavioral, context
    confidence: float  # 0.0 to 1.0
    weight: float  # Weight in fusion calculation
    reliability: float = 1.0  # How reliable this evidence source is (0-1)
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ExcludedEvidence:
    """Information about excluded evidence sources."""
    source: str
    original_confidence: float
    original_weight: float
    reason: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class FusionResult:
    """Result of Bayesian confidence fusion."""
    posterior_authentic: float  # P(authentic|evidence)
    posterior_spoof: float  # P(spoof|evidence)
    decision: DecisionType
    confidence: float  # Overall confidence in decision
    evidence_scores: List[EvidenceScore] = field(default_factory=list)
    excluded_evidence: List[ExcludedEvidence] = field(default_factory=list)
    reasoning: List[str] = field(default_factory=list)
    dominant_factor: str = ""  # Which factor most influenced decision
    uncertainty: float = 0.0  # Shannon entropy of posterior
    effective_threshold: float = 0.0  # Actual threshold used (may be adjusted)
    degradation_mode: bool = False  # True if operating with reduced evidence
    available_sources: int = 0  # Number of valid evidence sources used
    details: Dict[str, Any] = field(default_factory=dict)


class BayesianConfidenceFusion:
    """
    Bayesian Confidence Fusion for multi-factor authentication.

    Combines evidence from ML, physics, behavioral, and contextual
    sources using Bayesian probability theory.

    Key Features:
    - Proper uncertainty handling
    - Adaptive prior updates based on history
    - Configurable weights per evidence source
    - Detailed reasoning trail for audit

    Usage:
        fusion = get_bayesian_fusion()
        result = fusion.fuse(
            ml_confidence=0.92,
            physics_confidence=0.88,
            behavioral_confidence=0.95,
            context_confidence=0.90
        )
        if result.decision == DecisionType.AUTHENTICATE:
            grant_access()
    """

    def __init__(self):
        """Initialize Bayesian fusion engine."""
        self.config = BayesianFusionConfig

        # Current priors (adaptive)
        self._prior_authentic = self.config.PRIOR_AUTHENTIC
        self._prior_spoof = self.config.PRIOR_SPOOF

        # Weights
        self.ml_weight = self.config.ML_WEIGHT
        self.physics_weight = self.config.PHYSICS_WEIGHT
        self.behavioral_weight = self.config.BEHAVIORAL_WEIGHT
        self.context_weight = self.config.CONTEXT_WEIGHT

        # History for adaptive learning
        self._auth_history: List[Tuple[bool, float]] = []  # (was_authentic, posterior)
        self._spoof_history: List[Tuple[bool, float]] = []

        # Statistics
        self._fusion_count = 0
        self._total_authentic = 0
        self._total_spoof = 0

        logger.info(
            f"BayesianConfidenceFusion initialized: "
            f"prior_auth={self._prior_authentic:.2f}, "
            f"weights=[ML:{self.ml_weight}, Physics:{self.physics_weight}, "
            f"Behavioral:{self.behavioral_weight}, Context:{self.context_weight}]"
        )

    def fuse(
        self,
        ml_confidence: Optional[float] = None,
        physics_confidence: Optional[float] = None,
        behavioral_confidence: Optional[float] = None,
        context_confidence: Optional[float] = None,
        ml_details: Optional[Dict[str, Any]] = None,
        physics_details: Optional[Dict[str, Any]] = None,
        behavioral_details: Optional[Dict[str, Any]] = None,
        context_details: Optional[Dict[str, Any]] = None
    ) -> FusionResult:
        """
        Fuse multiple evidence sources using Bayesian inference with adaptive exclusion.

        v2.6 Enhancement: Automatically excludes unavailable evidence sources
        (confidence <= MIN_VALID_CONFIDENCE) and renormalizes weights.

        Args:
            ml_confidence: ML model confidence (0-1), None or <= 0.02 means unavailable
            physics_confidence: Physics analysis confidence (0-1)
            behavioral_confidence: Behavioral pattern confidence (0-1)
            context_confidence: Contextual factors confidence (0-1)
            *_details: Optional details for each evidence source

        Returns:
            FusionResult with posterior probabilities, decision, and degradation info
        """
        self._fusion_count += 1
        raw_evidence = []
        excluded_evidence = []
        reasoning = []

        # Build raw evidence list with all provided sources
        evidence_map = [
            ("ml", ml_confidence, self.ml_weight, ml_details),
            ("physics", physics_confidence, self.physics_weight, physics_details),
            ("behavioral", behavioral_confidence, self.behavioral_weight, behavioral_details),
            ("context", context_confidence, self.context_weight, context_details),
        ]

        for source, conf, weight, details in evidence_map:
            if conf is not None:
                raw_evidence.append(EvidenceScore(
                    source=source,
                    confidence=conf,
                    weight=weight,
                    details=details or {}
                ))

        # Adaptive exclusion: filter out unavailable evidence sources
        valid_evidence, excluded = self._filter_and_renormalize(raw_evidence)

        excluded_evidence = excluded

        # Track degradation mode
        ml_unavailable = any(e.source == "ml" for e in excluded_evidence)
        degradation_mode = len(excluded_evidence) > 0

        # Build reasoning for included evidence
        for evidence in valid_evidence:
            reasoning.append(f"{evidence.source.upper()} confidence: {evidence.confidence:.1%} (weight: {evidence.weight:.1%})")

        # Add reasoning for excluded evidence
        for excluded in excluded_evidence:
            reasoning.append(f"⚠️ {excluded.source.upper()} EXCLUDED: {excluded.reason}")

        # Compute adaptive threshold
        effective_threshold = self._compute_adaptive_threshold(
            valid_evidence, ml_unavailable
        )

        # Compute posteriors with valid evidence only
        posterior_authentic, posterior_spoof = self._compute_posteriors(valid_evidence)

        # Determine dominant factor from valid evidence
        dominant_factor = self._find_dominant_factor(valid_evidence)

        # Make decision with adaptive threshold
        decision = self._make_decision_adaptive(
            posterior_authentic, posterior_spoof, valid_evidence, effective_threshold
        )

        # Compute uncertainty (Shannon entropy)
        uncertainty = self._compute_uncertainty(posterior_authentic, posterior_spoof)

        # Overall confidence in the decision
        confidence = max(posterior_authentic, posterior_spoof)

        # Add decision reasoning
        if decision == DecisionType.AUTHENTICATE:
            if degradation_mode:
                reasoning.append(
                    f"Decision: AUTHENTICATE in degradation mode "
                    f"(posterior={posterior_authentic:.1%}, adjusted_threshold={effective_threshold:.1%})"
                )
            else:
                reasoning.append(
                    f"Decision: AUTHENTICATE (posterior={posterior_authentic:.1%}, "
                    f"threshold={effective_threshold:.1%})"
                )
        elif decision == DecisionType.REJECT:
            reasoning.append(
                f"Decision: REJECT (posterior={posterior_authentic:.1%}, "
                f"below threshold={self.config.REJECT_THRESHOLD:.1%})"
            )
        elif decision == DecisionType.CHALLENGE:
            reasoning.append(
                f"Decision: CHALLENGE (posterior={posterior_authentic:.1%} in range "
                f"{self.config.REJECT_THRESHOLD:.1%}-{effective_threshold:.1%})"
            )
        else:
            reasoning.append(f"Decision: ESCALATE (unusual pattern or insufficient sources)")

        result = FusionResult(
            posterior_authentic=posterior_authentic,
            posterior_spoof=posterior_spoof,
            decision=decision,
            confidence=confidence,
            evidence_scores=valid_evidence,
            excluded_evidence=excluded_evidence,
            reasoning=reasoning,
            dominant_factor=dominant_factor,
            uncertainty=uncertainty,
            effective_threshold=effective_threshold,
            degradation_mode=degradation_mode,
            available_sources=len(valid_evidence),
            details={
                "prior_authentic": self._prior_authentic,
                "prior_spoof": self._prior_spoof,
                "fusion_id": self._fusion_count,
                "original_weights": {
                    "ml": self.ml_weight,
                    "physics": self.physics_weight,
                    "behavioral": self.behavioral_weight,
                    "context": self.context_weight
                },
                "effective_weights": {e.source: e.weight for e in valid_evidence},
                "ml_unavailable": ml_unavailable,
                "adaptive_exclusion_enabled": self.config.ADAPTIVE_EXCLUSION_ENABLED
            }
        )

        logger.info(
            f"Bayesian fusion #{self._fusion_count}: "
            f"P(auth)={posterior_authentic:.3f}, decision={decision.value}, "
            f"dominant={dominant_factor}, sources={len(valid_evidence)}/{len(raw_evidence)}, "
            f"degradation={degradation_mode}"
        )

        return result

    def _filter_and_renormalize(
        self, evidence_scores: List[EvidenceScore]
    ) -> Tuple[List[EvidenceScore], List[ExcludedEvidence]]:
        """
        Filter out unavailable evidence and renormalize weights.

        Evidence with confidence <= MIN_VALID_CONFIDENCE is considered unavailable.
        Weights are redistributed proportionally or equally based on config.

        Returns:
            Tuple of (valid_evidence, excluded_evidence)
        """
        if not self.config.ADAPTIVE_EXCLUSION_ENABLED:
            # No filtering - use all evidence as-is
            return evidence_scores, []

        valid = []
        excluded = []
        min_conf = self.config.MIN_VALID_CONFIDENCE

        for evidence in evidence_scores:
            if evidence.confidence is None or evidence.confidence <= min_conf:
                reason = "unavailable" if evidence.confidence is None else f"below threshold ({evidence.confidence:.3f} <= {min_conf})"
                excluded.append(ExcludedEvidence(
                    source=evidence.source,
                    original_confidence=evidence.confidence or 0.0,
                    original_weight=evidence.weight,
                    reason=reason
                ))
            else:
                valid.append(evidence)

        # Renormalize weights if we have valid evidence
        if valid and excluded:
            total_valid_weight = sum(e.weight for e in valid)

            if self.config.WEIGHT_REDISTRIBUTION == "proportional" and total_valid_weight > 0:
                # Proportional: scale up weights to sum to 1.0
                scale_factor = 1.0 / total_valid_weight
                for e in valid:
                    e.weight = e.weight * scale_factor
            else:
                # Equal: distribute weight equally among valid sources
                equal_weight = 1.0 / len(valid)
                for e in valid:
                    e.weight = equal_weight

            logger.debug(
                f"Weight renormalization: excluded={[e.source for e in excluded]}, "
                f"new_weights={{e.source: e.weight for e in valid}}"
            )

        return valid, excluded

    def _compute_adaptive_threshold(
        self, valid_evidence: List[EvidenceScore], ml_unavailable: bool
    ) -> float:
        """
        Compute adaptive authentication threshold based on available evidence.

        When ML is unavailable, we lower the threshold since we have less information.
        When fewer sources are available, we also adjust accordingly.

        Returns:
            Adjusted threshold for authentication
        """
        base_threshold = self.config.AUTHENTICATE_THRESHOLD

        # Reduce threshold when ML is unavailable
        if ml_unavailable:
            base_threshold -= self.config.ML_UNAVAILABLE_THRESHOLD_REDUCTION

        # Further adjust based on number of sources
        source_count = len(valid_evidence)
        if source_count == 1:
            # Single source: be more lenient but still require high confidence
            base_threshold = max(0.70, base_threshold - 0.05)
        elif source_count == 2:
            # Two sources: slight reduction
            base_threshold = max(0.72, base_threshold - 0.03)

        # Clamp to valid range
        return max(0.50, min(0.95, base_threshold))

    def _make_decision_adaptive(
        self,
        posterior_authentic: float,
        posterior_spoof: float,
        evidence_scores: List[EvidenceScore],
        effective_threshold: float
    ) -> DecisionType:
        """Make authentication decision with adaptive threshold and source count check."""

        # Check minimum sources requirement
        if len(evidence_scores) < self.config.MIN_SOURCES_FOR_AUTH:
            logger.warning(
                f"Insufficient evidence sources: {len(evidence_scores)} < "
                f"{self.config.MIN_SOURCES_FOR_AUTH} required"
            )
            return DecisionType.ESCALATE

        # Check for unusual patterns that warrant escalation
        if self._detect_anomaly(evidence_scores):
            return DecisionType.ESCALATE

        # Standard threshold-based decision with adaptive threshold
        if posterior_authentic >= effective_threshold:
            return DecisionType.AUTHENTICATE
        elif posterior_authentic < self.config.REJECT_THRESHOLD:
            return DecisionType.REJECT
        else:
            return DecisionType.CHALLENGE

    def _compute_posteriors(
        self,
        evidence_scores: List[EvidenceScore]
    ) -> Tuple[float, float]:
        """
        Compute posterior probabilities using Bayesian inference.

        Uses weighted likelihood combination:
        P(auth|E) ∝ P(E|auth) * P(auth)

        Where P(E|auth) is estimated from evidence confidence scores.
        """
        if not evidence_scores:
            return self._prior_authentic, self._prior_spoof

        # Compute weighted combined likelihood
        # P(E|authentic) - evidence supports authenticity
        # P(E|spoof) - evidence supports spoofing

        # Normalize weights for available evidence
        total_weight = sum(e.weight for e in evidence_scores)

        log_likelihood_authentic = 0.0
        log_likelihood_spoof = 0.0

        for evidence in evidence_scores:
            normalized_weight = evidence.weight / total_weight if total_weight > 0 else 1.0
            conf = evidence.confidence

            # Clamp to avoid log(0)
            conf = max(0.001, min(0.999, conf))
            anti_conf = 1.0 - conf

            # Log likelihood contribution
            # High confidence -> high P(E|authentic), low P(E|spoof)
            log_likelihood_authentic += normalized_weight * math.log(conf)
            log_likelihood_spoof += normalized_weight * math.log(anti_conf)

        # Convert back from log space
        likelihood_authentic = math.exp(log_likelihood_authentic)
        likelihood_spoof = math.exp(log_likelihood_spoof)

        # Bayes' rule (unnormalized)
        posterior_authentic_unnorm = likelihood_authentic * self._prior_authentic
        posterior_spoof_unnorm = likelihood_spoof * self._prior_spoof

        # Normalize
        total = posterior_authentic_unnorm + posterior_spoof_unnorm
        if total > 0:
            posterior_authentic = posterior_authentic_unnorm / total
            posterior_spoof = posterior_spoof_unnorm / total
        else:
            posterior_authentic = self._prior_authentic
            posterior_spoof = self._prior_spoof

        return posterior_authentic, posterior_spoof

    def _find_dominant_factor(self, evidence_scores: List[EvidenceScore]) -> str:
        """Find which evidence source most influenced the decision."""
        if not evidence_scores:
            return "none"

        # Find evidence with highest weighted impact
        max_impact = -1.0
        dominant = "none"

        for evidence in evidence_scores:
            # Impact = weight * absolute deviation from 0.5
            impact = evidence.weight * abs(evidence.confidence - 0.5)
            if impact > max_impact:
                max_impact = impact
                dominant = evidence.source

        return dominant

    def _make_decision(
        self,
        posterior_authentic: float,
        posterior_spoof: float,
        evidence_scores: List[EvidenceScore]
    ) -> DecisionType:
        """Make authentication decision based on posteriors."""

        # Check for unusual patterns that warrant escalation
        if self._detect_anomaly(evidence_scores):
            return DecisionType.ESCALATE

        # Standard threshold-based decision
        if posterior_authentic >= self.config.AUTHENTICATE_THRESHOLD:
            return DecisionType.AUTHENTICATE
        elif posterior_authentic < self.config.REJECT_THRESHOLD:
            return DecisionType.REJECT
        else:
            return DecisionType.CHALLENGE

    def _detect_anomaly(self, evidence_scores: List[EvidenceScore]) -> bool:
        """
        Detect anomalous patterns that may indicate sophisticated attacks.

        Anomalies:
        - High disagreement between evidence sources
        - Evidence values at extremes (all 0.99 or all 0.01)
        - Unusual combinations (high ML but very low physics)
        """
        if len(evidence_scores) < 2:
            return False

        confidences = [e.confidence for e in evidence_scores]

        # Check for high disagreement
        conf_range = max(confidences) - min(confidences)
        if conf_range > 0.5:  # More than 50% disagreement
            logger.warning(
                f"Anomaly: High disagreement between evidence sources "
                f"(range={conf_range:.2f})"
            )
            return True

        # Check for suspiciously perfect scores
        if all(c > 0.99 for c in confidences) or all(c < 0.01 for c in confidences):
            logger.warning("Anomaly: Suspiciously uniform evidence scores")
            return True

        return False

    def _compute_uncertainty(
        self,
        posterior_authentic: float,
        posterior_spoof: float
    ) -> float:
        """
        Compute Shannon entropy as measure of decision uncertainty.

        H = -sum(p * log(p)) for all outcomes
        Max entropy = 1.0 (50/50 split)
        Min entropy = 0.0 (100% confident)
        """
        entropy = 0.0

        for p in [posterior_authentic, posterior_spoof]:
            if p > 0:
                entropy -= p * math.log2(p)

        # Normalize to 0-1 range (max entropy is log2(2) = 1)
        return entropy

    def update_priors(self, was_authentic: bool, posterior: float):
        """
        Update prior probabilities based on verified outcome.

        Uses exponential moving average for smooth adaptation.

        Args:
            was_authentic: True if the authentication was verified authentic
            posterior: The posterior probability at decision time
        """
        if not self.config.LEARNING_ENABLED:
            return

        if was_authentic:
            self._total_authentic += 1
            self._auth_history.append((True, posterior))
        else:
            self._total_spoof += 1
            self._spoof_history.append((False, posterior))

        # Update priors with exponential moving average
        total = self._total_authentic + self._total_spoof
        if total >= 10:  # Minimum samples before adapting
            empirical_rate = self._total_authentic / total

            # Blend empirical rate with current prior
            new_prior = (
                (1 - self.config.PRIOR_UPDATE_RATE) * self._prior_authentic +
                self.config.PRIOR_UPDATE_RATE * empirical_rate
            )

            # Clamp to valid range
            self._prior_authentic = max(
                self.config.MIN_PRIOR,
                min(self.config.MAX_PRIOR, new_prior)
            )
            self._prior_spoof = 1.0 - self._prior_authentic

            logger.debug(
                f"Priors updated: P(auth)={self._prior_authentic:.3f}, "
                f"P(spoof)={self._prior_spoof:.3f} (n={total})"
            )

    def get_statistics(self) -> Dict[str, Any]:
        """Get fusion engine statistics including adaptive configuration."""
        return {
            "version": "2.6",
            "fusion_count": self._fusion_count,
            "total_authentic": self._total_authentic,
            "total_spoof": self._total_spoof,
            "current_prior_authentic": self._prior_authentic,
            "current_prior_spoof": self._prior_spoof,
            "weights": {
                "ml": self.ml_weight,
                "physics": self.physics_weight,
                "behavioral": self.behavioral_weight,
                "context": self.context_weight
            },
            "thresholds": {
                "authenticate": self.config.AUTHENTICATE_THRESHOLD,
                "reject": self.config.REJECT_THRESHOLD,
                "challenge_range": self.config.CHALLENGE_RANGE
            },
            "adaptive_config": {
                "exclusion_enabled": self.config.ADAPTIVE_EXCLUSION_ENABLED,
                "min_valid_confidence": self.config.MIN_VALID_CONFIDENCE,
                "ml_unavailable_threshold_reduction": self.config.ML_UNAVAILABLE_THRESHOLD_REDUCTION,
                "min_sources_for_auth": self.config.MIN_SOURCES_FOR_AUTH,
                "weight_redistribution": self.config.WEIGHT_REDISTRIBUTION
            },
            "learning_enabled": self.config.LEARNING_ENABLED
        }

    def reset_history(self):
        """Reset learning history (useful for testing or recalibration)."""
        self._auth_history.clear()
        self._spoof_history.clear()
        self._total_authentic = 0
        self._total_spoof = 0
        self._prior_authentic = self.config.PRIOR_AUTHENTIC
        self._prior_spoof = self.config.PRIOR_SPOOF
        logger.info("Bayesian fusion history reset")


# =============================================================================
# Global Instance Management
# =============================================================================

_bayesian_fusion: Optional[BayesianConfidenceFusion] = None


def get_bayesian_fusion() -> BayesianConfidenceFusion:
    """
    Get global Bayesian Confidence Fusion instance.

    Uses lazy initialization with singleton pattern.

    Returns:
        BayesianConfidenceFusion: Global fusion engine instance
    """
    global _bayesian_fusion
    if _bayesian_fusion is None:
        _bayesian_fusion = BayesianConfidenceFusion()
    return _bayesian_fusion
