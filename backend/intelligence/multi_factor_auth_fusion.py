"""
Multi-Factor Authentication Fusion Engine

Central orchestrator for combining multiple authentication signals into
unified authentication decisions. Integrates:

1. Voice Biometric Intelligence (VBI) - Speaker verification
2. Network Context Provider - WiFi/location awareness
3. Unlock Pattern Tracker - Temporal behavioral patterns
4. Device State Monitor - Physical device state
5. Voice Drift Detector - Voice evolution tracking

Uses advanced fusion techniques:
- Bayesian probability fusion
- Weighted confidence scoring
- Contextual intelligence boosting
- Anomaly-aware risk assessment
- Multi-factor reasoning chains

Part of JARVIS v5.0 Advanced Voice Biometric Intelligence System

Author: JARVIS AI Agent
Version: 5.0.0
"""

import asyncio
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


class AuthDecision(Enum):
    """Authentication decision types."""
    AUTHENTICATE = "authenticate"      # Strong confidence - grant access
    CHALLENGE = "challenge"            # Moderate confidence - ask security question
    DENY = "deny"                      # Low confidence - deny access
    ESCALATE = "escalate"             # Suspicious - alert and deny


class AuthFactorType(Enum):
    """Types of authentication factors."""
    VOICE_BIOMETRIC = "voice_biometric"
    NETWORK_CONTEXT = "network_context"
    TEMPORAL_PATTERN = "temporal_pattern"
    DEVICE_STATE = "device_state"
    VOICE_DRIFT = "voice_drift"


@dataclass
class AuthFactor:
    """Individual authentication factor result."""
    factor_type: AuthFactorType
    confidence: float  # 0.0-1.0
    trust_score: float  # 0.0-1.0
    weight: float  # Factor weight in fusion
    reasoning: str
    metadata: Dict
    timestamp: datetime


@dataclass
class FusionResult:
    """Multi-factor authentication fusion result."""
    # Decision
    decision: AuthDecision
    final_confidence: float  # 0.0-1.0
    risk_score: float  # 0.0-1.0 (0 = no risk, 1 = high risk)

    # Individual factors
    voice_confidence: float
    network_trust: float
    temporal_confidence: float
    device_trust: float
    drift_adjustment: float

    # Fusion details
    factors: List[AuthFactor]
    fusion_method: str  # "bayesian", "weighted_average", "unanimous"
    confidence_breakdown: Dict[str, float]

    # Reasoning
    reasoning: List[str]
    anomalies: List[str]
    security_alerts: List[str]

    # Recommendations
    should_learn: bool  # Should this verification update the profile?
    recommended_actions: List[str]

    timestamp: datetime

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'decision': self.decision.value,
            'final_confidence': self.final_confidence,
            'risk_score': self.risk_score,
            'voice_confidence': self.voice_confidence,
            'network_trust': self.network_trust,
            'temporal_confidence': self.temporal_confidence,
            'device_trust': self.device_trust,
            'drift_adjustment': self.drift_adjustment,
            'fusion_method': self.fusion_method,
            'confidence_breakdown': self.confidence_breakdown,
            'reasoning': self.reasoning,
            'anomalies': self.anomalies,
            'security_alerts': self.security_alerts,
            'should_learn': self.should_learn,
            'recommended_actions': self.recommended_actions,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class FusionConfig:
    """Configuration for multi-factor fusion."""
    # Decision thresholds
    authenticate_threshold: float = 0.85  # Grant access above this
    challenge_threshold: float = 0.70     # Challenge between this and authenticate
    deny_threshold: float = 0.70          # Deny below this

    # Factor weights (must sum to 1.0)
    voice_weight: float = 0.50            # Voice biometric is primary
    network_weight: float = 0.15          # Network context secondary
    temporal_weight: float = 0.15         # Temporal patterns secondary
    device_weight: float = 0.12           # Device state tertiary
    drift_weight: float = 0.08            # Drift adjustment minor

    # Risk assessment
    enable_risk_assessment: bool = True
    high_risk_threshold: float = 0.70

    # Learning
    enable_continuous_learning: bool = True
    min_confidence_for_learning: float = 0.90

    # Fusion methods
    primary_fusion_method: str = "bayesian"  # "bayesian", "weighted", "unanimous"
    enable_unanimous_veto: bool = True  # Any factor can veto if very low
    unanimous_veto_threshold: float = 0.30

    @classmethod
    def from_env(cls) -> 'FusionConfig':
        """Load configuration from environment variables."""
        return cls(
            authenticate_threshold=float(os.getenv('AUTH_FUSION_AUTH_THRESHOLD', '0.85')),
            challenge_threshold=float(os.getenv('AUTH_FUSION_CHALLENGE_THRESHOLD', '0.70')),
            deny_threshold=float(os.getenv('AUTH_FUSION_DENY_THRESHOLD', '0.70')),
            voice_weight=float(os.getenv('AUTH_FUSION_VOICE_WEIGHT', '0.50')),
            network_weight=float(os.getenv('AUTH_FUSION_NETWORK_WEIGHT', '0.15')),
            temporal_weight=float(os.getenv('AUTH_FUSION_TEMPORAL_WEIGHT', '0.15')),
            device_weight=float(os.getenv('AUTH_FUSION_DEVICE_WEIGHT', '0.12')),
            drift_weight=float(os.getenv('AUTH_FUSION_DRIFT_WEIGHT', '0.08')),
            enable_risk_assessment=os.getenv('AUTH_FUSION_RISK_ASSESSMENT', 'true').lower() == 'true',
            high_risk_threshold=float(os.getenv('AUTH_FUSION_HIGH_RISK_THRESHOLD', '0.70')),
            enable_continuous_learning=os.getenv('AUTH_FUSION_CONTINUOUS_LEARNING', 'true').lower() == 'true',
            min_confidence_for_learning=float(os.getenv('AUTH_FUSION_MIN_LEARN_CONF', '0.90')),
            primary_fusion_method=os.getenv('AUTH_FUSION_METHOD', 'bayesian'),
            enable_unanimous_veto=os.getenv('AUTH_FUSION_UNANIMOUS_VETO', 'true').lower() == 'true',
            unanimous_veto_threshold=float(os.getenv('AUTH_FUSION_VETO_THRESHOLD', '0.30'))
        )


class MultiFactorAuthFusion:
    """
    Multi-Factor Authentication Fusion Engine.

    Orchestrates multiple authentication factors to make unified decisions
    with enhanced security, transparency, and adaptability.

    Usage:
        fusion = await get_fusion_engine()
        result = await fusion.fuse_and_decide(
            voice_confidence=0.92,
            network_context={...},
            temporal_context={...},
            device_context={...}
        )
    """

    def __init__(self, config: Optional[FusionConfig] = None):
        self.config = config or FusionConfig.from_env()
        self._lock = asyncio.Lock()

        # Statistics
        self._stats = {
            'total_authentications': 0,
            'authenticated': 0,
            'challenged': 0,
            'denied': 0,
            'escalated': 0,
            'avg_confidence': 0.0,
            'avg_risk_score': 0.0
        }

        logger.info("MultiFactorAuthFusion engine initialized")

    async def fuse_and_decide(
        self,
        user_id: str,
        voice_confidence: float,
        voice_reasoning: str = "",
        network_context: Optional[Dict] = None,
        temporal_context: Optional[Dict] = None,
        device_context: Optional[Dict] = None,
        drift_context: Optional[Dict] = None,
    ) -> FusionResult:
        """
        Fuse multiple authentication factors and make decision.

        Args:
            user_id: User identifier
            voice_confidence: Voice biometric confidence (0.0-1.0)
            voice_reasoning: Voice verification reasoning
            network_context: Network/location context from NetworkContextProvider
            temporal_context: Temporal patterns from UnlockPatternTracker
            device_context: Device state from DeviceStateMonitor
            drift_context: Voice drift analysis from VoiceDriftDetector

        Returns:
            FusionResult with decision, confidence, and detailed reasoning
        """
        async with self._lock:
            try:
                timestamp = datetime.now()

                # Gather all authentication factors
                factors = []

                # Voice biometric factor (primary)
                voice_factor = AuthFactor(
                    factor_type=AuthFactorType.VOICE_BIOMETRIC,
                    confidence=voice_confidence,
                    trust_score=voice_confidence,
                    weight=self.config.voice_weight,
                    reasoning=voice_reasoning or f"Voice match: {voice_confidence:.1%}",
                    metadata={'confidence': voice_confidence},
                    timestamp=timestamp
                )
                factors.append(voice_factor)

                # Network context factor
                network_trust = 0.5  # Neutral default
                network_reasoning = "No network context"
                if network_context:
                    network_trust = network_context.get('trust_score', 0.5)
                    network_confidence = network_context.get('confidence', 0.5)
                    ssid_trust = network_context.get('ssid_trust_level', 'unknown')
                    network_reasoning = network_context.get('reasoning', 'Network context available')

                    network_factor = AuthFactor(
                        factor_type=AuthFactorType.NETWORK_CONTEXT,
                        confidence=network_confidence,
                        trust_score=network_trust,
                        weight=self.config.network_weight,
                        reasoning=f"Network: {ssid_trust} ({network_trust:.0%} trust)",
                        metadata=network_context,
                        timestamp=timestamp
                    )
                    factors.append(network_factor)

                # Temporal pattern factor
                temporal_confidence = 0.5  # Neutral default
                temporal_reasoning = "No temporal context"
                if temporal_context:
                    temporal_confidence = temporal_context.get('confidence', 0.5)
                    is_typical = temporal_context.get('is_typical_time', False)
                    anomaly_score = temporal_context.get('anomaly_score', 0.0)
                    temporal_reasoning = temporal_context.get('reasoning', 'Temporal context available')

                    temporal_factor = AuthFactor(
                        factor_type=AuthFactorType.TEMPORAL_PATTERN,
                        confidence=temporal_confidence,
                        trust_score=temporal_confidence,
                        weight=self.config.temporal_weight,
                        reasoning=f"Timing: {'typical' if is_typical else 'unusual'} (anomaly: {anomaly_score:.0%})",
                        metadata=temporal_context,
                        timestamp=timestamp
                    )
                    factors.append(temporal_factor)

                # Device state factor
                device_trust = 0.5  # Neutral default
                device_reasoning = "No device context"
                if device_context:
                    device_trust = device_context.get('trust_score', 0.5)
                    device_confidence = device_context.get('confidence', 0.5)
                    device_state = device_context.get('state', 'unknown')
                    device_reasoning = device_context.get('reasoning', 'Device context available')

                    device_factor = AuthFactor(
                        factor_type=AuthFactorType.DEVICE_STATE,
                        confidence=device_confidence,
                        trust_score=device_trust,
                        weight=self.config.device_weight,
                        reasoning=f"Device: {device_state} ({device_trust:.0%} trust)",
                        metadata=device_context,
                        timestamp=timestamp
                    )
                    factors.append(device_factor)

                # Voice drift factor (adjustment only)
                drift_adjustment = 0.0
                drift_reasoning = "No drift analysis"
                if drift_context:
                    drift_adjustment = drift_context.get('confidence_adjustment', 0.0)
                    drift_reasoning = drift_context.get('reasoning', 'Drift analysis available')

                    # Drift is an adjustment, not a standalone factor
                    drift_factor = AuthFactor(
                        factor_type=AuthFactorType.VOICE_DRIFT,
                        confidence=0.5 + drift_adjustment,  # Convert adjustment to confidence
                        trust_score=0.5 + drift_adjustment,
                        weight=self.config.drift_weight,
                        reasoning=f"Drift: {drift_adjustment:+.0%} adjustment",
                        metadata=drift_context,
                        timestamp=timestamp
                    )
                    factors.append(drift_factor)

                # Perform fusion
                if self.config.primary_fusion_method == "bayesian":
                    final_confidence = await self._bayesian_fusion(factors)
                    fusion_method = "bayesian"
                elif self.config.primary_fusion_method == "unanimous":
                    final_confidence = await self._unanimous_fusion(factors)
                    fusion_method = "unanimous"
                else:
                    final_confidence = await self._weighted_fusion(factors)
                    fusion_method = "weighted_average"

                # Apply drift adjustment
                final_confidence = max(0.0, min(1.0, final_confidence + drift_adjustment))

                # Check unanimous veto
                if self.config.enable_unanimous_veto:
                    veto_result = self._check_unanimous_veto(factors)
                    if veto_result['vetoed']:
                        final_confidence = min(final_confidence, self.config.challenge_threshold - 0.01)
                        logger.warning(f"Unanimous veto triggered: {veto_result['reason']}")

                # Calculate risk score
                risk_score = await self._calculate_risk_score(factors, final_confidence)

                # Make decision
                decision = self._make_decision(final_confidence, risk_score)

                # Generate reasoning and anomalies
                reasoning, anomalies, alerts = self._generate_reasoning(
                    factors, final_confidence, risk_score, decision
                )

                # Determine if we should learn from this
                should_learn = (
                    self.config.enable_continuous_learning and
                    decision == AuthDecision.AUTHENTICATE and
                    final_confidence >= self.config.min_confidence_for_learning
                )

                # Generate recommended actions
                recommended_actions = self._generate_recommendations(
                    decision, risk_score, factors, anomalies
                )

                # Build confidence breakdown
                confidence_breakdown = {
                    factor.factor_type.value: factor.confidence * factor.weight
                    for factor in factors
                }

                # Build result
                result = FusionResult(
                    decision=decision,
                    final_confidence=final_confidence,
                    risk_score=risk_score,
                    voice_confidence=voice_confidence,
                    network_trust=network_trust,
                    temporal_confidence=temporal_confidence,
                    device_trust=device_trust,
                    drift_adjustment=drift_adjustment,
                    factors=factors,
                    fusion_method=fusion_method,
                    confidence_breakdown=confidence_breakdown,
                    reasoning=reasoning,
                    anomalies=anomalies,
                    security_alerts=alerts,
                    should_learn=should_learn,
                    recommended_actions=recommended_actions,
                    timestamp=timestamp
                )

                # Update statistics
                self._update_stats(result)

                logger.info(
                    f"Auth fusion for {user_id}: {decision.value}, "
                    f"confidence={final_confidence:.1%}, risk={risk_score:.1%}"
                )

                return result

            except Exception as e:
                logger.error(f"Error in multi-factor fusion: {e}", exc_info=True)
                # Return safe denial on error
                return self._get_error_result(str(e))

    async def _bayesian_fusion(self, factors: List[AuthFactor]) -> float:
        """
        Bayesian probability fusion of multiple factors.

        Uses Bayes' theorem to combine independent probability estimates.
        """
        # Start with neutral prior
        log_odds = 0.0

        for factor in factors:
            # Convert confidence to probability
            p = factor.confidence * factor.trust_score

            # Clamp to avoid log(0)
            p = max(0.01, min(0.99, p))

            # Convert to log odds and weight
            factor_log_odds = np.log(p / (1 - p))
            log_odds += factor_log_odds * factor.weight

        # Convert back to probability
        odds = np.exp(log_odds)
        probability = odds / (1 + odds)

        return float(probability)

    async def _weighted_fusion(self, factors: List[AuthFactor]) -> float:
        """Simple weighted average of factor confidences."""
        total_confidence = 0.0
        total_weight = 0.0

        for factor in factors:
            # Combine confidence and trust score
            effective_confidence = factor.confidence * factor.trust_score
            total_confidence += effective_confidence * factor.weight
            total_weight += factor.weight

        if total_weight == 0:
            return 0.5  # Neutral

        return total_confidence / total_weight

    async def _unanimous_fusion(self, factors: List[AuthFactor]) -> float:
        """
        Unanimous fusion - all factors must agree.
        Takes minimum confidence among all factors.
        """
        if not factors:
            return 0.5

        min_confidence = min(
            factor.confidence * factor.trust_score
            for factor in factors
        )

        # Weight by average
        avg_confidence = sum(
            factor.confidence * factor.trust_score * factor.weight
            for factor in factors
        ) / sum(factor.weight for factor in factors)

        # Return weighted combination of min and average
        return 0.7 * min_confidence + 0.3 * avg_confidence

    def _check_unanimous_veto(self, factors: List[AuthFactor]) -> Dict:
        """Check if any factor should veto the authentication."""
        for factor in factors:
            effective_confidence = factor.confidence * factor.trust_score

            if effective_confidence < self.config.unanimous_veto_threshold:
                return {
                    'vetoed': True,
                    'factor': factor.factor_type.value,
                    'confidence': effective_confidence,
                    'reason': f"{factor.factor_type.value} confidence too low ({effective_confidence:.1%})"
                }

        return {'vetoed': False}

    async def _calculate_risk_score(
        self,
        factors: List[AuthFactor],
        final_confidence: float
    ) -> float:
        """
        Calculate risk score based on factors and confidence.

        Risk indicators:
        - Low voice confidence
        - Unknown network
        - Unusual timing
        - Device movement
        - Voice drift
        - Confidence variance among factors
        """
        if not self.config.enable_risk_assessment:
            return 0.0

        risk = 0.0

        # Base risk from low confidence
        risk += max(0, 1.0 - final_confidence) * 0.4

        # Factor-specific risks
        for factor in factors:
            if factor.factor_type == AuthFactorType.VOICE_BIOMETRIC:
                if factor.confidence < 0.85:
                    risk += 0.15

            elif factor.factor_type == AuthFactorType.NETWORK_CONTEXT:
                if 'ssid_trust_level' in factor.metadata:
                    if factor.metadata['ssid_trust_level'] == 'unknown':
                        risk += 0.15

            elif factor.factor_type == AuthFactorType.TEMPORAL_PATTERN:
                anomaly = factor.metadata.get('anomaly_score', 0.0)
                risk += anomaly * 0.20

            elif factor.factor_type == AuthFactorType.DEVICE_STATE:
                if not factor.metadata.get('is_stationary', True):
                    risk += 0.10

        # Variance risk (factors disagree)
        if len(factors) > 1:
            confidences = [f.confidence * f.trust_score for f in factors]
            variance = np.var(confidences)
            risk += min(0.15, variance * 0.5)

        return max(0.0, min(1.0, risk))

    def _make_decision(
        self,
        final_confidence: float,
        risk_score: float
    ) -> AuthDecision:
        """Make authentication decision from confidence and risk."""
        # Escalate if high risk regardless of confidence
        if risk_score >= self.config.high_risk_threshold:
            return AuthDecision.ESCALATE

        # Standard threshold-based decision
        if final_confidence >= self.config.authenticate_threshold:
            return AuthDecision.AUTHENTICATE
        elif final_confidence >= self.config.challenge_threshold:
            return AuthDecision.CHALLENGE
        else:
            return AuthDecision.DENY

    def _generate_reasoning(
        self,
        factors: List[AuthFactor],
        final_confidence: float,
        risk_score: float,
        decision: AuthDecision
    ) -> Tuple[List[str], List[str], List[str]]:
        """Generate human-readable reasoning, anomalies, and alerts."""
        reasoning = []
        anomalies = []
        alerts = []

        # Add decision reasoning
        reasoning.append(
            f"Final decision: {decision.value} "
            f"(confidence: {final_confidence:.1%}, risk: {risk_score:.1%})"
        )

        # Add factor reasoning
        for factor in factors:
            reasoning.append(factor.reasoning)

            # Check for anomalies
            if factor.factor_type == AuthFactorType.VOICE_BIOMETRIC:
                if factor.confidence < 0.70:
                    anomalies.append(f"Low voice confidence: {factor.confidence:.1%}")

            elif factor.factor_type == AuthFactorType.NETWORK_CONTEXT:
                if factor.metadata.get('ssid_trust_level') == 'unknown':
                    anomalies.append("Authentication from unknown network")

            elif factor.factor_type == AuthFactorType.TEMPORAL_PATTERN:
                anomaly_score = factor.metadata.get('anomaly_score', 0.0)
                if anomaly_score > 0.7:
                    anomalies.append(f"Unusual unlock time (anomaly: {anomaly_score:.0%})")

            elif factor.factor_type == AuthFactorType.DEVICE_STATE:
                if not factor.metadata.get('is_stationary', True):
                    anomalies.append("Device in motion during unlock")

                if factor.metadata.get('just_woke', False):
                    reasoning.append("Device recently woke from sleep")

        # Generate security alerts
        if risk_score >= self.config.high_risk_threshold:
            alerts.append(
                f"HIGH RISK: Authentication risk score is {risk_score:.0%}. "
                "Multiple factors indicate potential security threat."
            )

        if len(anomalies) >= 3:
            alerts.append(
                f"Multiple anomalies detected ({len(anomalies)}). "
                "Recommend additional verification."
            )

        return reasoning, anomalies, alerts

    def _generate_recommendations(
        self,
        decision: AuthDecision,
        risk_score: float,
        factors: List[AuthFactor],
        anomalies: List[str]
    ) -> List[str]:
        """Generate recommended actions based on decision."""
        recommendations = []

        if decision == AuthDecision.AUTHENTICATE:
            if risk_score > 0.3:
                recommendations.append("Monitor for suspicious activity")

        elif decision == AuthDecision.CHALLENGE:
            recommendations.append("Ask security question for additional verification")
            recommendations.append("Consider enrolling more voice samples if pattern continues")

        elif decision == AuthDecision.DENY:
            recommendations.append("Require password authentication")
            if any("voice" in a.lower() for a in anomalies):
                recommendations.append("Voice confidence low - check audio quality")

        elif decision == AuthDecision.ESCALATE:
            recommendations.append("LOG SECURITY EVENT - Potential unauthorized access")
            recommendations.append("Require multi-factor authentication")
            recommendations.append("Alert user of suspicious activity")

        return recommendations

    def _get_error_result(self, error: str) -> FusionResult:
        """Return safe error result."""
        return FusionResult(
            decision=AuthDecision.DENY,
            final_confidence=0.0,
            risk_score=1.0,
            voice_confidence=0.0,
            network_trust=0.0,
            temporal_confidence=0.0,
            device_trust=0.0,
            drift_adjustment=0.0,
            factors=[],
            fusion_method="error",
            confidence_breakdown={},
            reasoning=[f"Error in authentication fusion: {error}"],
            anomalies=["Fusion engine error"],
            security_alerts=["Authentication system error - denying access"],
            should_learn=False,
            recommended_actions=["Require password authentication", "Check system logs"],
            timestamp=datetime.now()
        )

    def _update_stats(self, result: FusionResult):
        """Update internal statistics."""
        self._stats['total_authentications'] += 1

        if result.decision == AuthDecision.AUTHENTICATE:
            self._stats['authenticated'] += 1
        elif result.decision == AuthDecision.CHALLENGE:
            self._stats['challenged'] += 1
        elif result.decision == AuthDecision.DENY:
            self._stats['denied'] += 1
        elif result.decision == AuthDecision.ESCALATE:
            self._stats['escalated'] += 1

        # Update rolling averages
        n = self._stats['total_authentications']
        self._stats['avg_confidence'] = (
            self._stats['avg_confidence'] * (n - 1) + result.final_confidence
        ) / n
        self._stats['avg_risk_score'] = (
            self._stats['avg_risk_score'] * (n - 1) + result.risk_score
        ) / n

    def get_stats(self) -> Dict:
        """Get fusion engine statistics."""
        total = self._stats['total_authentications']
        if total == 0:
            return self._stats

        return {
            **self._stats,
            'authenticate_rate': self._stats['authenticated'] / total,
            'challenge_rate': self._stats['challenged'] / total,
            'deny_rate': self._stats['denied'] / total,
            'escalate_rate': self._stats['escalated'] / total,
        }


# Singleton instance
_fusion_instance: Optional[MultiFactorAuthFusion] = None
_fusion_lock = asyncio.Lock()


async def get_fusion_engine(config: Optional[FusionConfig] = None) -> MultiFactorAuthFusion:
    """
    Get singleton MultiFactorAuthFusion instance.

    Args:
        config: Optional configuration (only used on first call)

    Returns:
        MultiFactorAuthFusion instance
    """
    global _fusion_instance

    async with _fusion_lock:
        if _fusion_instance is None:
            _fusion_instance = MultiFactorAuthFusion(config)
            logger.info("Multi-factor auth fusion engine singleton initialized")

        return _fusion_instance


# CLI testing
if __name__ == "__main__":
    import sys

    async def main():
        """Test multi-factor fusion."""
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        fusion = await get_fusion_engine()

        print("\n" + "="*80)
        print("JARVIS Multi-Factor Authentication Fusion - Test")
        print("="*80 + "\n")

        # Test case 1: High confidence all factors
        print("Test 1: High confidence authentication")
        result = await fusion.fuse_and_decide(
            user_id="derek",
            voice_confidence=0.94,
            voice_reasoning="Strong voice match",
            network_context={
                'trust_score': 0.95,
                'confidence': 0.90,
                'ssid_trust_level': 'trusted',
                'reasoning': 'Home network'
            },
            temporal_context={
                'confidence': 0.88,
                'is_typical_time': True,
                'anomaly_score': 0.0,
                'reasoning': 'Typical morning unlock'
            },
            device_context={
                'trust_score': 0.92,
                'confidence': 0.90,
                'state': 'stationary',
                'is_stationary': True,
                'is_docked': True,
                'reasoning': 'Docked workstation'
            }
        )

        print(f"Decision: {result.decision.value.upper()}")
        print(f"Confidence: {result.final_confidence:.1%}")
        print(f"Risk Score: {result.risk_score:.1%}")
        print("\nReasoning:")
        for reason in result.reasoning:
            print(f"  - {reason}")

        # Test case 2: Suspicious - unknown network
        print("\n" + "="*80)
        print("Test 2: Suspicious authentication (unknown network)")
        result2 = await fusion.fuse_and_decide(
            user_id="derek",
            voice_confidence=0.78,
            voice_reasoning="Moderate voice match",
            network_context={
                'trust_score': 0.50,
                'confidence': 0.60,
                'ssid_trust_level': 'unknown',
                'reasoning': 'Unknown network'
            },
            temporal_context={
                'confidence': 0.45,
                'is_typical_time': False,
                'anomaly_score': 0.85,
                'reasoning': 'Unusual time (3:47 AM)'
            },
            device_context={
                'trust_score': 0.55,
                'confidence': 0.60,
                'state': 'portable',
                'is_stationary': False,
                'just_woke': False,
                'reasoning': 'Device recently moved'
            }
        )

        print(f"Decision: {result2.decision.value.upper()}")
        print(f"Confidence: {result2.final_confidence:.1%}")
        print(f"Risk Score: {result2.risk_score:.1%}")
        print("\nAnomalies:")
        for anomaly in result2.anomalies:
            print(f"  ⚠️  {anomaly}")
        print("\nRecommendations:")
        for rec in result2.recommended_actions:
            print(f"  → {rec}")

        # Statistics
        print("\n" + "="*80)
        stats = fusion.get_stats()
        print("Statistics:")
        print(f"  Total: {stats['total_authentications']}")
        print(f"  Authenticated: {stats['authenticated']}")
        print(f"  Challenged: {stats['challenged']}")
        print(f"  Denied: {stats['denied']}")
        print(f"  Avg Confidence: {stats['avg_confidence']:.1%}")
        print(f"  Avg Risk: {stats['avg_risk_score']:.1%}")
        print("\n" + "="*80 + "\n")

    asyncio.run(main())
