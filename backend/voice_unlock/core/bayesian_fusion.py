"""
Bayesian Confidence Fusion for Physics-Aware Voice Authentication v2.5

This module implements Bayesian probability fusion for combining multiple
evidence sources in voice authentication decisions:

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
class FusionResult:
    """Result of Bayesian confidence fusion."""
    posterior_authentic: float  # P(authentic|evidence)
    posterior_spoof: float  # P(spoof|evidence)
    decision: DecisionType
    confidence: float  # Overall confidence in decision
    evidence_scores: List[EvidenceScore] = field(default_factory=list)
    reasoning: List[str] = field(default_factory=list)
    dominant_factor: str = ""  # Which factor most influenced decision
    uncertainty: float = 0.0  # Shannon entropy of posterior
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
        Fuse multiple evidence sources using Bayesian inference.

        Args:
            ml_confidence: ML model confidence (0-1)
            physics_confidence: Physics analysis confidence (0-1)
            behavioral_confidence: Behavioral pattern confidence (0-1)
            context_confidence: Contextual factors confidence (0-1)
            *_details: Optional details for each evidence source

        Returns:
            FusionResult with posterior probabilities and decision
        """
        self._fusion_count += 1
        evidence_scores = []
        reasoning = []

        # Collect evidence
        if ml_confidence is not None:
            evidence_scores.append(EvidenceScore(
                source="ml",
                confidence=ml_confidence,
                weight=self.ml_weight,
                details=ml_details or {}
            ))
            reasoning.append(f"ML confidence: {ml_confidence:.1%}")

        if physics_confidence is not None:
            evidence_scores.append(EvidenceScore(
                source="physics",
                confidence=physics_confidence,
                weight=self.physics_weight,
                details=physics_details or {}
            ))
            reasoning.append(f"Physics confidence: {physics_confidence:.1%}")

        if behavioral_confidence is not None:
            evidence_scores.append(EvidenceScore(
                source="behavioral",
                confidence=behavioral_confidence,
                weight=self.behavioral_weight,
                details=behavioral_details or {}
            ))
            reasoning.append(f"Behavioral confidence: {behavioral_confidence:.1%}")

        if context_confidence is not None:
            evidence_scores.append(EvidenceScore(
                source="context",
                confidence=context_confidence,
                weight=self.context_weight,
                details=context_details or {}
            ))
            reasoning.append(f"Context confidence: {context_confidence:.1%}")

        # Compute posteriors
        posterior_authentic, posterior_spoof = self._compute_posteriors(evidence_scores)

        # Determine dominant factor
        dominant_factor = self._find_dominant_factor(evidence_scores)

        # Make decision
        decision = self._make_decision(posterior_authentic, posterior_spoof, evidence_scores)

        # Compute uncertainty (Shannon entropy)
        uncertainty = self._compute_uncertainty(posterior_authentic, posterior_spoof)

        # Overall confidence in the decision
        confidence = max(posterior_authentic, posterior_spoof)

        # Add reasoning for decision
        if decision == DecisionType.AUTHENTICATE:
            reasoning.append(
                f"Decision: AUTHENTICATE (posterior={posterior_authentic:.1%}, "
                f"threshold={self.config.AUTHENTICATE_THRESHOLD:.1%})"
            )
        elif decision == DecisionType.REJECT:
            reasoning.append(
                f"Decision: REJECT (posterior={posterior_authentic:.1%}, "
                f"below threshold={self.config.REJECT_THRESHOLD:.1%})"
            )
        elif decision == DecisionType.CHALLENGE:
            reasoning.append(
                f"Decision: CHALLENGE (posterior={posterior_authentic:.1%} in range "
                f"{self.config.CHALLENGE_RANGE[0]:.1%}-{self.config.CHALLENGE_RANGE[1]:.1%})"
            )
        else:
            reasoning.append(f"Decision: ESCALATE (unusual pattern detected)")

        result = FusionResult(
            posterior_authentic=posterior_authentic,
            posterior_spoof=posterior_spoof,
            decision=decision,
            confidence=confidence,
            evidence_scores=evidence_scores,
            reasoning=reasoning,
            dominant_factor=dominant_factor,
            uncertainty=uncertainty,
            details={
                "prior_authentic": self._prior_authentic,
                "prior_spoof": self._prior_spoof,
                "fusion_id": self._fusion_count,
                "weights": {
                    "ml": self.ml_weight,
                    "physics": self.physics_weight,
                    "behavioral": self.behavioral_weight,
                    "context": self.context_weight
                }
            }
        )

        logger.debug(
            f"Bayesian fusion #{self._fusion_count}: "
            f"P(auth)={posterior_authentic:.3f}, decision={decision.value}, "
            f"dominant={dominant_factor}"
        )

        return result

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
        """Get fusion engine statistics."""
        return {
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
