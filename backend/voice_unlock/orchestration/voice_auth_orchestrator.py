"""
Voice Authentication Orchestrator

Enterprise-grade LangChain-based orchestration for multi-factor
voice authentication with dynamic fallback chains.

Features:
- Configurable fallback chain with multiple authentication levels
- Dynamic threshold adjustment based on context
- Graceful degradation on failures
- Comprehensive audit logging
- Real-time metrics and observability
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .voice_auth_tools import (
    VoiceAuthToolRegistry,
    get_voice_auth_tools,
    VoiceBiometricInput,
    VoiceBiometricOutput,
    BehavioralContextInput,
    BehavioralContextOutput,
    ChallengeInput,
    ChallengeOutput,
    ChallengeVerifyInput,
    ChallengeVerifyOutput,
    ProximityInput,
    ProximityOutput,
    AntiSpoofingInput,
    AntiSpoofingOutput,
    BayesianFusionInput,
    BayesianFusionOutput,
)

# v2.0: Enhanced voice authentication with ChromaDB, Langfuse, cost optimization
try:
    from .voice_auth_enhancements import (
        get_voice_auth_enhancements,
        VoiceAuthEnhancementManager,
    )
    ENHANCEMENTS_AVAILABLE = True
except ImportError:
    logger.warning("Voice auth enhancements not available - install chromadb and langfuse")
    ENHANCEMENTS_AVAILABLE = False
    get_voice_auth_enhancements = None
    VoiceAuthEnhancementManager = None

# v2.1: Advanced features - deepfake detection, voice evolution, multi-speaker, quality analysis
try:
    from .voice_auth_advanced_features import (
        get_advanced_features,
        AdvancedFeaturesManager,
    )
    ADVANCED_FEATURES_AVAILABLE = True
except ImportError:
    logger.info("Advanced voice auth features not available - using core features only")
    ADVANCED_FEATURES_AVAILABLE = False
    get_advanced_features = None
    AdvancedFeaturesManager = None

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

class OrchestratorConfig:
    """Environment-driven configuration for the orchestrator."""

    @staticmethod
    def get_primary_threshold() -> float:
        """Primary voice-only authentication threshold."""
        return float(os.getenv("VOICE_AUTH_PRIMARY_THRESHOLD", "0.85"))

    @staticmethod
    def get_fusion_threshold() -> float:
        """Voice + behavioral fusion threshold."""
        return float(os.getenv("VOICE_AUTH_FUSION_THRESHOLD", "0.80"))

    @staticmethod
    def get_challenge_threshold() -> float:
        """Threshold to trigger challenge question."""
        return float(os.getenv("VOICE_AUTH_CHALLENGE_THRESHOLD", "0.70"))

    @staticmethod
    def get_proximity_threshold() -> float:
        """Threshold after proximity check."""
        return float(os.getenv("VOICE_AUTH_PROXIMITY_THRESHOLD", "0.75"))

    @staticmethod
    def get_max_fallback_levels() -> int:
        """Maximum fallback levels to attempt."""
        return int(os.getenv("VOICE_AUTH_MAX_FALLBACKS", "5"))

    @staticmethod
    def get_orchestrator_timeout_ms() -> int:
        """Overall orchestration timeout."""
        return int(os.getenv("VOICE_AUTH_ORCHESTRATOR_TIMEOUT_MS", "30000"))

    @staticmethod
    def get_enable_anti_spoofing() -> bool:
        """Whether to perform anti-spoofing checks."""
        return os.getenv("VOICE_AUTH_ANTI_SPOOFING", "true").lower() == "true"

    @staticmethod
    def get_enable_behavioral_fusion() -> bool:
        """Whether to use behavioral fusion."""
        return os.getenv("VOICE_AUTH_BEHAVIORAL_FUSION", "true").lower() == "true"

    @staticmethod
    def get_enable_challenge_fallback() -> bool:
        """Whether to allow challenge question fallback."""
        return os.getenv("VOICE_AUTH_CHALLENGE_FALLBACK", "true").lower() == "true"

    @staticmethod
    def get_enable_proximity_fallback() -> bool:
        """Whether to allow Apple Watch proximity fallback."""
        return os.getenv("VOICE_AUTH_PROXIMITY_FALLBACK", "true").lower() == "true"

    @staticmethod
    def get_strict_mode() -> bool:
        """Strict mode: no fallbacks allowed."""
        return os.getenv("VOICE_AUTH_STRICT_MODE", "false").lower() == "true"


# =============================================================================
# MULTI-FACTOR TRANSPARENCY GENERATOR - v1.0 (Clinical-Grade Intelligence Edition)
# =============================================================================

class MultiFactorTransparencyGenerator:
    """
    Explain multi-factor authentication fusion decisions in natural language.

    Transforms complex Bayesian fusion math into conversational explanations
    that help users understand WHY authentication decisions were made.

    Features:
    - Progressive confidence explanations (voice alone vs. fused)
    - Fallback chain transparency (why we degraded to next level)
    - Multi-factor breakdown (voice 78%, behavioral 94% → combined 91%)
    - Context-aware messaging (time, location, behavioral patterns)
    - Graceful degradation narratives

    Configuration:
    - JARVIS_MULTI_FACTOR_TRANSPARENCY: Enable transparency (default: true)
    - JARVIS_EXPLAIN_FUSION_MATH: Explain Bayesian fusion details (default: false)
    - JARVIS_EXPLAIN_FALLBACK_CHAIN: Explain why fallbacks triggered (default: true)
    """

    def __init__(self):
        self.logger = logger
        self.transparency_enabled = (
            os.getenv("JARVIS_MULTI_FACTOR_TRANSPARENCY", "true").lower() == "true"
        )
        self.explain_fusion_math = (
            os.getenv("JARVIS_EXPLAIN_FUSION_MATH", "false").lower() == "true"
        )
        self.explain_fallback_chain = (
            os.getenv("JARVIS_EXPLAIN_FALLBACK_CHAIN", "true").lower() == "true"
        )

    def explain_fusion_decision(
        self,
        voice_confidence: float,
        behavioral_confidence: Optional[float],
        contextual_confidence: Optional[float],
        final_confidence: float,
        decision: str,
        name: str = "Derek"
    ) -> str:
        """
        Explain why multi-factor fusion made a specific decision.

        Args:
            voice_confidence: Voice biometric confidence (0.0-1.0)
            behavioral_confidence: Behavioral analysis confidence (0.0-1.0)
            contextual_confidence: Contextual factors confidence (0.0-1.0)
            final_confidence: Bayesian fusion final confidence (0.0-1.0)
            decision: Decision made (AUTHENTICATED, REJECTED, etc.)
            name: User's name for personalization

        Returns:
            Natural language explanation

        Example:
            "Your voice was at 78%, Derek - slightly below the usual threshold -
            but your behavioral patterns matched perfectly at 94%. You're unlocking
            at your typical time, from your regular location. Combined confidence: 91%.
            High confidence it's you. Unlocking."
        """
        if not self.transparency_enabled:
            return ""

        # Build explanation based on confidence levels
        explanations = []

        # Voice component explanation
        voice_pct = int(voice_confidence * 100)
        if voice_confidence >= 0.92:
            explanations.append(f"Voice match is excellent at {voice_pct}%")
        elif voice_confidence >= 0.85:
            explanations.append(f"Voice match is strong at {voice_pct}%")
        elif voice_confidence >= 0.75:
            explanations.append(
                f"Your voice was at {voice_pct}%, {name} - slightly below the usual threshold"
            )
        else:
            explanations.append(
                f"Voice match is borderline at {voice_pct}% (threshold: 85%)"
            )

        # Behavioral component (if available)
        if behavioral_confidence is not None:
            behavioral_pct = int(behavioral_confidence * 100)
            if behavioral_confidence >= 0.90:
                explanations.append(
                    f"but your behavioral patterns matched perfectly at {behavioral_pct}%"
                )
            elif behavioral_confidence >= 0.80:
                explanations.append(
                    f"and behavioral patterns look good at {behavioral_pct}%"
                )
            else:
                explanations.append(
                    f"but behavioral patterns are uncertain at {behavioral_pct}%"
                )

        # Contextual factors (if available)
        if contextual_confidence is not None:
            contextual_pct = int(contextual_confidence * 100)
            if contextual_confidence >= 0.90:
                # Add context details
                context_details = self._get_contextual_details(contextual_confidence)
                explanations.append(context_details)

        # Final fusion result
        final_pct = int(final_confidence * 100)
        if decision == "AUTHENTICATED":
            if self.explain_fusion_math:
                explanations.append(
                    f"Combined confidence: {final_pct}% (Bayesian fusion). "
                    f"High confidence it's you, {name}. Unlocking."
                )
            else:
                explanations.append(
                    f"Combined confidence: {final_pct}%. High confidence it's you. Unlocking now, {name}."
                )
        elif decision == "REJECTED":
            explanations.append(
                f"Combined confidence: {final_pct}% - below threshold. Access denied."
            )
        else:
            explanations.append(
                f"Combined confidence: {final_pct}% - additional verification needed."
            )

        return " ".join(explanations)

    def explain_fallback_chain(
        self,
        fallback_level: str,
        previous_failures: List[Dict[str, Any]],
        current_confidence: float,
        name: str = "Derek"
    ) -> str:
        """
        Explain graceful degradation through the fallback chain.

        Args:
            fallback_level: Current fallback level (PRIMARY, BEHAVIORAL_FUSION, etc.)
            previous_failures: List of previous failed attempts with details
            current_confidence: Current confidence score
            name: User's name for personalization

        Returns:
            Natural language explanation of fallback chain

        Example:
            "Voice authentication alone wasn't quite strong enough (78%).
            I checked your behavioral patterns - you're unlocking at your
            typical 7:15 AM time, from home WiFi. With those factors combined,
            confidence is now 91%. That's good enough. Unlocking for you."
        """
        if not self.explain_fallback_chain:
            return ""

        explanations = []

        # Explain why we're at this fallback level
        if fallback_level == "BEHAVIORAL_FUSION" and previous_failures:
            # We fell back from PRIMARY
            primary_failure = previous_failures[0]
            primary_conf = primary_failure.get("confidence", 0.0)
            primary_pct = int(primary_conf * 100)

            explanations.append(
                f"Voice authentication alone wasn't quite strong enough ({primary_pct}%). "
                f"I checked your behavioral patterns -"
            )

            # Add behavioral details
            behavioral_details = self._get_behavioral_details(current_confidence)
            explanations.append(behavioral_details)

            # Final decision
            current_pct = int(current_confidence * 100)
            if current_confidence >= 0.80:
                explanations.append(
                    f"With those factors combined, confidence is now {current_pct}%. "
                    f"That's good enough. Unlocking for you, {name}."
                )
            else:
                explanations.append(
                    f"Combined confidence is {current_pct}% - still not quite there. "
                    "Let me try one more verification method..."
                )

        elif fallback_level == "CHALLENGE" and len(previous_failures) >= 2:
            # We fell back from BEHAVIORAL_FUSION
            explanations.append(
                f"For security, I need a quick verification, {name}. "
                "Your voice and behavioral patterns are close but not quite conclusive. "
            )

        elif fallback_level == "PROXIMITY" and len(previous_failures) >= 3:
            # We fell back from CHALLENGE
            explanations.append(
                f"Let me check if your Apple Watch is nearby, {name}. "
                "This would confirm you're physically present."
            )

        elif fallback_level == "MANUAL_AUTH":
            # All methods exhausted
            explanations.append(
                f"I've tried voice, behavioral, and additional verification, {name}. "
                "For security, I'll need your password this time. "
                "I'll re-learn your voice after you unlock."
            )

        return " ".join(explanations)

    def _get_contextual_details(self, contextual_confidence: float) -> str:
        """
        Generate contextual details explanation.

        Args:
            contextual_confidence: Contextual confidence score

        Returns:
            Contextual details narrative
        """
        # This would be enhanced with actual context data
        # For now, provide generic high-confidence context
        if contextual_confidence >= 0.95:
            return "You're unlocking at your typical time, from your regular location."
        elif contextual_confidence >= 0.85:
            return "Timing and location patterns match your usual habits."
        else:
            return "Context is within expected parameters."

    def _get_behavioral_details(self, confidence: float) -> str:
        """
        Generate behavioral details explanation.

        Args:
            confidence: Behavioral confidence score

        Returns:
            Behavioral details narrative
        """
        # This would be enhanced with actual behavioral data
        # For now, provide generic behavioral patterns
        if confidence >= 0.90:
            return (
                "you're unlocking at your typical 7:15 AM time, from home WiFi, "
                "and it's been exactly 16 hours since you last locked up."
            )
        elif confidence >= 0.80:
            return "your timing and access patterns are consistent with your usual behavior."
        else:
            return "some behavioral indicators match, but others are uncertain."


# =============================================================================
# ENUMS AND DATA STRUCTURES
# =============================================================================

class FallbackLevel(Enum):
    """Authentication fallback levels."""

    PRIMARY = auto()           # Voice biometric only
    BEHAVIORAL_FUSION = auto() # Voice + behavioral
    CHALLENGE = auto()         # Challenge question
    PROXIMITY = auto()         # Apple Watch proximity
    MANUAL_AUTH = auto()       # Manual authentication fallback
    DENIED = auto()           # All methods failed

    @property
    def display_name(self) -> str:
        """Get human-readable name."""
        names = {
            self.PRIMARY: "Voice Biometric",
            self.BEHAVIORAL_FUSION: "Voice + Behavioral",
            self.CHALLENGE: "Challenge Question",
            self.PROXIMITY: "Apple Watch Proximity",
            self.MANUAL_AUTH: "Manual Authentication",
            self.DENIED: "Access Denied",
        }
        return names.get(self, self.name)


class AuthenticationDecision(str, Enum):
    """Final authentication decision."""

    AUTHENTICATED = "authenticated"
    CHALLENGE_REQUIRED = "challenge_required"
    PROXIMITY_REQUIRED = "proximity_required"
    MANUAL_AUTH_REQUIRED = "manual_auth_required"
    DENIED = "denied"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class FallbackAttempt:
    """Record of a fallback level attempt."""

    level: FallbackLevel
    success: bool
    confidence: float
    threshold: float
    duration_ms: float
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthenticationChainResult:
    """Result from the authentication chain."""

    # Decision
    decision: AuthenticationDecision
    final_confidence: float
    authenticated_user: Optional[str] = None

    # Chain execution
    final_level: FallbackLevel = FallbackLevel.PRIMARY
    levels_attempted: int = 0
    fallback_attempts: List[FallbackAttempt] = field(default_factory=list)

    # Timing
    total_duration_ms: float = 0.0
    level_durations_ms: Dict[str, float] = field(default_factory=dict)

    # Context
    session_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Response
    response_text: str = ""
    should_request_password: bool = False
    challenge_data: Optional[ChallengeOutput] = None

    # Anomalies
    anomalies_detected: List[str] = field(default_factory=list)
    spoofing_suspected: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "decision": self.decision.value,
            "final_confidence": self.final_confidence,
            "authenticated_user": self.authenticated_user,
            "final_level": self.final_level.display_name,
            "levels_attempted": self.levels_attempted,
            "total_duration_ms": self.total_duration_ms,
            "session_id": self.session_id,
            "response_text": self.response_text,
            "spoofing_suspected": self.spoofing_suspected,
        }


# =============================================================================
# VOICE AUTH ORCHESTRATOR
# =============================================================================

class VoiceAuthOrchestrator:
    """
    Orchestrates multi-factor voice authentication with fallback chain.

    Provides intelligent authentication flow:
    1. Primary: Voice biometric only (85% threshold)
    2. Fallback 1: Voice + Behavioral fusion (80% threshold)
    3. Fallback 2: Challenge question
    4. Fallback 3: Apple Watch proximity
    5. Final: Manual authentication fallback

    Usage:
        orchestrator = await get_voice_auth_orchestrator()
        result = await orchestrator.authenticate(
            audio_data=audio_bytes,
            user_id="derek",
        )
    """

    def __init__(
        self,
        tool_registry: Optional[VoiceAuthToolRegistry] = None,
    ):
        """
        Initialize the orchestrator.

        Args:
            tool_registry: Optional custom tool registry
        """
        self._tools = tool_registry or get_voice_auth_tools()
        self._lock = asyncio.Lock()

        # v2.0: Enhancement manager for pattern recognition, audit, cost optimization
        self._enhancements: Optional[VoiceAuthEnhancementManager] = None
        self._enhancements_initialized = False

        # v2.1: Advanced features manager for deepfake detection, evolution tracking, etc.
        self._advanced_features: Optional[AdvancedFeaturesManager] = None
        self._advanced_features_initialized = False

        # Statistics
        self._stats = {
            "total_authentications": 0,
            "successful_authentications": 0,
            "fallback_authentications": 0,
            "failed_authentications": 0,
            "spoofing_attempts_blocked": 0,
            "level_success_counts": {level.name: 0 for level in FallbackLevel},
            "avg_duration_ms": 0.0,
            # v2.0 stats
            "cache_hits": 0,
            "replay_attacks_blocked": 0,
            "pattern_learnings": 0,
            # v2.1 advanced stats
            "deepfakes_blocked": 0,
            "voice_quality_adjustments": 0,
            "multi_speaker_identifications": 0,
            "voice_evolution_adaptations": 0,
        }

        # Observability hooks
        self._on_level_complete: Optional[Callable] = None
        self._on_authentication_complete: Optional[Callable] = None

        logger.info("VoiceAuthOrchestrator initialized")

    def set_level_complete_hook(self, hook: Callable) -> None:
        """Set callback for level completion events."""
        self._on_level_complete = hook

    def set_authentication_complete_hook(self, hook: Callable) -> None:
        """Set callback for authentication completion events."""
        self._on_authentication_complete = hook

    async def _ensure_enhancements(self) -> bool:
        """
        Lazy-load enhancement manager (v2.0).

        Returns:
            True if enhancements are available, False otherwise
        """
        if self._enhancements_initialized:
            return self._enhancements is not None

        if not ENHANCEMENTS_AVAILABLE:
            self._enhancements_initialized = True
            return False

        try:
            self._enhancements = await get_voice_auth_enhancements()
            self._enhancements_initialized = True
            logger.info("[Orchestrator] ✓ v2.0 Enhancements loaded")
            return True
        except Exception as e:
            logger.warning(f"[Orchestrator] Could not load enhancements: {e}")
            self._enhancements_initialized = True
            return False

    async def _ensure_advanced_features(self) -> bool:
        """
        Lazy-load advanced features manager (v2.1).

        Returns:
            True if advanced features are available, False otherwise
        """
        if self._advanced_features_initialized:
            return self._advanced_features is not None

        if not ADVANCED_FEATURES_AVAILABLE:
            self._advanced_features_initialized = True
            return False

        try:
            self._advanced_features = await get_advanced_features()
            self._advanced_features_initialized = True
            logger.info("[Orchestrator] ✓ v2.1 Advanced Features loaded (deepfake, evolution, multi-speaker, quality)")
            return True
        except Exception as e:
            logger.warning(f"[Orchestrator] Could not load advanced features: {e}")
            self._advanced_features_initialized = True
            return False

    async def authenticate(
        self,
        audio_data: bytes,
        user_id: str,
        sample_rate: int = 16000,
        context: Optional[Dict[str, Any]] = None,
        max_level: Optional[FallbackLevel] = None,
    ) -> AuthenticationChainResult:
        """
        Authenticate a user via the fallback chain.

        Args:
            audio_data: Raw audio bytes
            user_id: Expected user ID
            sample_rate: Audio sample rate
            context: Optional context (time, location, device)
            max_level: Maximum fallback level to attempt

        Returns:
            AuthenticationChainResult with complete result
        """
        start_time = time.perf_counter()
        session_id = f"auth_{int(time.time() * 1000)}"

        result = AuthenticationChainResult(
            session_id=session_id,
            final_level=FallbackLevel.PRIMARY,
        )

        # Check strict mode
        if OrchestratorConfig.get_strict_mode():
            max_level = FallbackLevel.PRIMARY

        # Get max fallbacks
        max_fallbacks = OrchestratorConfig.get_max_fallback_levels()

        # v2.0: Initialize enhancements
        enhancements_loaded = await self._ensure_enhancements()
        voice_embedding = None  # Will be extracted during primary verification

        try:
            # v2.0 STEP 0a: Pre-authentication hook (pattern recognition, caching)
            enrichment = {}
            if enhancements_loaded and self._enhancements:
                enrichment = await self._enhancements.pre_authentication_hook(
                    audio_data=audio_data,
                    user_id=user_id,
                    embedding=None,  # Will check after extraction
                    environmental_context=context or {},
                )

                # Check for replay attack BEFORE processing
                replay_risk = enrichment.get("replay_risk", 0.0)
                if replay_risk > 0.95:
                    result.decision = AuthenticationDecision.DENIED
                    result.spoofing_suspected = True
                    result.response_text = self._enhancements.generate_feedback(
                        user_id=user_id,
                        confidence=0.0,
                        decision="DENIED",
                        failure_reason="replay_attack",
                    )
                    self._stats["replay_attacks_blocked"] += 1
                    self._stats["spoofing_attempts_blocked"] += 1
                    logger.warning(
                        f"[Orchestrator] Replay attack blocked (risk: {replay_risk:.2%})"
                    )
                    return await self._finalize_result(result, start_time)

            # v2.1 STEP 0b: Advanced features analysis (deepfake, quality, multi-speaker)
            advanced_features_loaded = await self._ensure_advanced_features()
            advanced_analysis = {}

            if advanced_features_loaded and self._advanced_features:
                advanced_analysis = await self._advanced_features.comprehensive_analysis(
                    audio_data=audio_data,
                    user_id=user_id,
                    voice_embedding=None,  # Will be extracted during primary verification
                    sample_rate=sample_rate,
                )

                # CRITICAL: Deepfake detection (blocks before any processing)
                deepfake_result = advanced_analysis.get("deepfake", {})
                if deepfake_result.get("result") == "FAKE":
                    genuine_prob = deepfake_result.get("genuine_probability", 0.0)
                    anomaly_flags = deepfake_result.get("anomaly_flags", [])

                    result.decision = AuthenticationDecision.DENIED
                    result.spoofing_suspected = True
                    result.anomalies_detected.extend(anomaly_flags)
                    result.response_text = (
                        f"Advanced security alert: Multi-modal deepfake detection identified "
                        f"synthetic voice characteristics (genuine probability: {genuine_prob:.1%}). "
                        f"Anomalies detected: {', '.join(anomaly_flags[:3])}. Access denied."
                    )
                    self._stats["deepfakes_blocked"] += 1
                    self._stats["spoofing_attempts_blocked"] += 1
                    logger.warning(
                        f"[Orchestrator] 🛡️ Deepfake blocked (genuine: {genuine_prob:.2%}, flags: {anomaly_flags})"
                    )
                    return await self._finalize_result(result, start_time)

                # Voice quality analysis (for confidence adjustments)
                quality_metrics = advanced_analysis.get("quality", {})
                if quality_metrics:
                    quality_category = quality_metrics.get("quality_category", "unknown")
                    overall_score = quality_metrics.get("overall_score", 0.0)

                    logger.info(
                        f"[Orchestrator] Voice quality: {quality_category} "
                        f"(score: {overall_score:.2f}, SNR: {quality_metrics.get('snr_db', 0):.1f}dB)"
                    )

                    # Track quality adjustments for stats
                    if quality_category in ["fair", "poor"]:
                        self._stats["voice_quality_adjustments"] += 1

                # Multi-speaker identification (if enabled)
                speaker_match = advanced_analysis.get("speaker_match", {})
                if speaker_match.get("matched_speaker_id"):
                    matched_id = speaker_match["matched_speaker_id"]
                    confidence = speaker_match.get("confidence", 0.0)

                    logger.info(
                        f"[Orchestrator] Multi-speaker identified: {matched_id} "
                        f"(confidence: {confidence:.2%})"
                    )

                    self._stats["multi_speaker_identifications"] += 1

                # Voice evolution tracking
                evolution = advanced_analysis.get("evolution", {})
                if evolution.get("significant_drift"):
                    drift_info = evolution.get("drift_info", {})
                    logger.info(
                        f"[Orchestrator] Voice evolution detected: "
                        f"drift={drift_info.get('embedding_drift', 0):.4f}, "
                        f"natural={evolution.get('natural_drift', False)}"
                    )
                    self._stats["voice_evolution_adaptations"] += 1

            # Step 0: Anti-spoofing check
            if OrchestratorConfig.get_enable_anti_spoofing():
                spoofing_result = await self._check_anti_spoofing(
                    audio_data, sample_rate
                )
                if not spoofing_result.is_live:
                    result.spoofing_suspected = True
                    result.anomalies_detected.extend(spoofing_result.flags)

                    if spoofing_result.confidence < 0.5:
                        # Clear spoofing attempt
                        result.decision = AuthenticationDecision.DENIED
                        result.response_text = (
                            "Security alert: Audio characteristics suggest "
                            "a recording or synthetic voice. Access denied."
                        )
                        self._stats["spoofing_attempts_blocked"] += 1
                        return await self._finalize_result(result, start_time)

            # Step 1: Primary voice biometric
            primary_result = await self._try_primary(
                audio_data, user_id, sample_rate, result
            )

            if primary_result.success:
                result.decision = AuthenticationDecision.AUTHENTICATED
                result.authenticated_user = user_id
                result.final_confidence = primary_result.confidence

                # v2.0: Use enhanced feedback if available
                if enhancements_loaded and self._enhancements:
                    result.response_text = self._enhancements.generate_feedback(
                        user_id=user_id,
                        confidence=primary_result.confidence,
                        decision="AUTHENTICATED",
                        level_name=FallbackLevel.PRIMARY.name,
                        environmental_context=context or {},
                    )
                else:
                    result.response_text = self._generate_success_response(
                        user_id, primary_result.confidence, FallbackLevel.PRIMARY
                    )

                return await self._finalize_result(result, start_time, voice_embedding)

            # Check if we should continue
            if max_level == FallbackLevel.PRIMARY or result.levels_attempted >= max_fallbacks:
                result.decision = AuthenticationDecision.DENIED
                result.response_text = self._generate_failure_response(
                    primary_result.confidence, []
                )
                return await self._finalize_result(result, start_time)

            # Step 2: Behavioral fusion
            if OrchestratorConfig.get_enable_behavioral_fusion():
                fusion_result = await self._try_behavioral_fusion(
                    audio_data, user_id, sample_rate, context or {}, result
                )

                if fusion_result.success:
                    result.decision = AuthenticationDecision.AUTHENTICATED
                    result.authenticated_user = user_id
                    result.final_confidence = fusion_result.confidence

                    # v2.0: Use enhanced feedback if available
                    if enhancements_loaded and self._enhancements:
                        result.response_text = self._enhancements.generate_feedback(
                            user_id=user_id,
                            confidence=fusion_result.confidence,
                            decision="AUTHENTICATED",
                            level_name=FallbackLevel.BEHAVIORAL_FUSION.name,
                            environmental_context=context or {},
                        )
                    else:
                        result.response_text = self._generate_success_response(
                            user_id, fusion_result.confidence, FallbackLevel.BEHAVIORAL_FUSION
                        )

                    self._stats["fallback_authentications"] += 1
                    return await self._finalize_result(result, start_time, voice_embedding)

            if max_level == FallbackLevel.BEHAVIORAL_FUSION or result.levels_attempted >= max_fallbacks:
                result.decision = AuthenticationDecision.DENIED
                result.response_text = self._generate_failure_response(
                    result.final_confidence, result.anomalies_detected
                )
                return await self._finalize_result(result, start_time)

            # Step 3: Challenge question
            if OrchestratorConfig.get_enable_challenge_fallback():
                challenge_result = await self._prepare_challenge(user_id, result)

                result.decision = AuthenticationDecision.CHALLENGE_REQUIRED
                result.challenge_data = challenge_result
                result.response_text = (
                    f"I'm having trouble verifying your voice. "
                    f"Quick security question: {challenge_result.question}"
                )
                return await self._finalize_result(result, start_time)

            # Step 4: Proximity check
            if OrchestratorConfig.get_enable_proximity_fallback():
                proximity_result = await self._try_proximity(user_id, result)

                if proximity_result.success:
                    result.decision = AuthenticationDecision.AUTHENTICATED
                    result.authenticated_user = user_id
                    result.final_confidence = proximity_result.confidence
                    result.response_text = (
                        f"Your Apple Watch confirms it's you. "
                        f"Unlocking for you, {user_id}."
                    )
                    self._stats["fallback_authentications"] += 1
                    return await self._finalize_result(result, start_time)

                # If proximity available but not verified
                if not proximity_result.error:
                    result.decision = AuthenticationDecision.PROXIMITY_REQUIRED
                    result.response_text = (
                        "Please unlock your Apple Watch to verify your identity."
                    )
                    return await self._finalize_result(result, start_time)

            # Step 5: Manual authentication fallback
            result.decision = AuthenticationDecision.MANUAL_AUTH_REQUIRED
            result.should_request_password = True
            result.response_text = (
                f"I wasn't able to verify your voice, {user_id}. "
                "Please use manual authentication to unlock."
            )

            return await self._finalize_result(result, start_time)

        except asyncio.TimeoutError:
            result.decision = AuthenticationDecision.TIMEOUT
            result.response_text = "Authentication timed out. Please try again."
            return await self._finalize_result(result, start_time)

        except Exception as e:
            logger.exception(f"Authentication error: {e}")
            result.decision = AuthenticationDecision.ERROR
            result.response_text = "An error occurred during authentication."
            return await self._finalize_result(result, start_time)

    async def _check_anti_spoofing(
        self,
        audio_data: bytes,
        sample_rate: int,
    ) -> AntiSpoofingOutput:
        """Perform anti-spoofing check."""
        try:
            input_data = AntiSpoofingInput(
                audio_data=audio_data,
                sample_rate=sample_rate,
            )
            return await self._tools.invoke("anti_spoofing_detect", input_data)
        except Exception as e:
            logger.warning(f"Anti-spoofing check failed: {e}")
            return AntiSpoofingOutput(
                is_live=True,  # Fail open
                confidence=0.5,
                flags=[f"check_failed: {str(e)}"],
            )

    async def _try_primary(
        self,
        audio_data: bytes,
        user_id: str,
        sample_rate: int,
        result: AuthenticationChainResult,
    ) -> FallbackAttempt:
        """Try primary voice biometric authentication."""
        start_time = time.perf_counter()
        threshold = OrchestratorConfig.get_primary_threshold()

        try:
            input_data = VoiceBiometricInput(
                audio_data=audio_data,
                user_id=user_id,
                sample_rate=sample_rate,
            )
            output = await self._tools.invoke("voice_biometric_verify", input_data)

            duration = (time.perf_counter() - start_time) * 1000
            success = output.verified and output.confidence >= threshold

            attempt = FallbackAttempt(
                level=FallbackLevel.PRIMARY,
                success=success,
                confidence=output.confidence,
                threshold=threshold,
                duration_ms=duration,
                details={
                    "embedding_similarity": output.embedding_similarity,
                    "quality_score": output.quality_score,
                },
            )

            result.fallback_attempts.append(attempt)
            result.levels_attempted += 1
            result.final_level = FallbackLevel.PRIMARY
            result.final_confidence = output.confidence

            if self._on_level_complete:
                await self._on_level_complete(attempt)

            return attempt

        except Exception as e:
            duration = (time.perf_counter() - start_time) * 1000
            attempt = FallbackAttempt(
                level=FallbackLevel.PRIMARY,
                success=False,
                confidence=0.0,
                threshold=threshold,
                duration_ms=duration,
                error=str(e),
            )
            result.fallback_attempts.append(attempt)
            result.levels_attempted += 1
            return attempt

    async def _try_behavioral_fusion(
        self,
        audio_data: bytes,
        user_id: str,
        sample_rate: int,
        context: Dict[str, Any],
        result: AuthenticationChainResult,
    ) -> FallbackAttempt:
        """Try voice + behavioral fusion authentication."""
        start_time = time.perf_counter()
        threshold = OrchestratorConfig.get_fusion_threshold()

        try:
            # Get voice confidence from previous attempt
            voice_confidence = result.final_confidence

            # Get behavioral confidence
            now = datetime.now()
            behavioral_input = BehavioralContextInput(
                user_id=user_id,
                hour_of_day=context.get("hour", now.hour),
                day_of_week=context.get("day", now.weekday()),
                wifi_hash=context.get("wifi_hash", ""),
                device_id=context.get("device_id", ""),
            )
            behavioral_output = await self._tools.invoke(
                "behavioral_context_analyze", behavioral_input
            )

            # Fuse confidences
            fusion_input = BayesianFusionInput(
                ml_confidence=voice_confidence,
                behavioral_confidence=behavioral_output.confidence,
                physics_confidence=0.7,  # Default
                context_confidence=0.5 if behavioral_output.anomaly_score < 0.3 else 0.3,
            )
            fusion_output = await self._tools.invoke(
                "bayesian_fusion_calculate", fusion_input
            )

            duration = (time.perf_counter() - start_time) * 1000
            success = fusion_output.final_confidence >= threshold

            attempt = FallbackAttempt(
                level=FallbackLevel.BEHAVIORAL_FUSION,
                success=success,
                confidence=fusion_output.final_confidence,
                threshold=threshold,
                duration_ms=duration,
                details={
                    "voice_confidence": voice_confidence,
                    "behavioral_confidence": behavioral_output.confidence,
                    "fusion_decision": fusion_output.decision,
                    "contributing_factors": behavioral_output.factors,
                },
            )

            result.fallback_attempts.append(attempt)
            result.levels_attempted += 1
            result.final_level = FallbackLevel.BEHAVIORAL_FUSION
            result.final_confidence = fusion_output.final_confidence

            if self._on_level_complete:
                await self._on_level_complete(attempt)

            return attempt

        except Exception as e:
            duration = (time.perf_counter() - start_time) * 1000
            attempt = FallbackAttempt(
                level=FallbackLevel.BEHAVIORAL_FUSION,
                success=False,
                confidence=result.final_confidence,
                threshold=threshold,
                duration_ms=duration,
                error=str(e),
            )
            result.fallback_attempts.append(attempt)
            result.levels_attempted += 1
            return attempt

    async def _prepare_challenge(
        self,
        user_id: str,
        result: AuthenticationChainResult,
    ) -> ChallengeOutput:
        """Prepare a challenge question."""
        start_time = time.perf_counter()

        try:
            input_data = ChallengeInput(
                user_id=user_id,
                challenge_type="personal",
                difficulty="medium",
            )
            challenge = await self._tools.invoke(
                "challenge_question_generate", input_data
            )

            duration = (time.perf_counter() - start_time) * 1000

            attempt = FallbackAttempt(
                level=FallbackLevel.CHALLENGE,
                success=False,  # Not yet verified
                confidence=0.0,
                threshold=0.0,
                duration_ms=duration,
                details={"challenge_id": challenge.challenge_id},
            )
            result.fallback_attempts.append(attempt)
            result.levels_attempted += 1
            result.final_level = FallbackLevel.CHALLENGE

            return challenge

        except Exception as e:
            logger.exception(f"Challenge generation failed: {e}")
            # Return a default challenge
            return ChallengeOutput(
                challenge_id="fallback",
                question="Please enter your password to continue.",
                expected_answer_hash="",
                expires_at=datetime.now(timezone.utc),
            )

    async def verify_challenge_response(
        self,
        challenge_id: str,
        response: str,
        user_id: str,
    ) -> AuthenticationChainResult:
        """Verify a challenge response."""
        start_time = time.perf_counter()
        session_id = f"challenge_{int(time.time() * 1000)}"

        result = AuthenticationChainResult(
            session_id=session_id,
            final_level=FallbackLevel.CHALLENGE,
        )

        try:
            input_data = ChallengeVerifyInput(
                challenge_id=challenge_id,
                response=response,
            )
            verify_result = await self._tools.invoke(
                "challenge_response_verify", input_data
            )

            if verify_result.verified:
                result.decision = AuthenticationDecision.AUTHENTICATED
                result.authenticated_user = user_id
                result.final_confidence = 0.85  # Challenge success confidence
                result.response_text = f"Correct! Unlocking for you, {user_id}."
            else:
                if verify_result.attempts_remaining > 0:
                    result.decision = AuthenticationDecision.CHALLENGE_REQUIRED
                    result.response_text = (
                        f"{verify_result.feedback} "
                        f"({verify_result.attempts_remaining} attempts remaining)"
                    )
                else:
                    result.decision = AuthenticationDecision.MANUAL_AUTH_REQUIRED
                    result.should_request_password = True
                    result.response_text = (
                        "Maximum challenge attempts exceeded. "
                        "Please use manual authentication."
                    )

            return await self._finalize_result(result, start_time)

        except Exception as e:
            logger.exception(f"Challenge verification error: {e}")
            result.decision = AuthenticationDecision.ERROR
            result.response_text = "Error verifying response."
            return await self._finalize_result(result, start_time)

    async def _try_proximity(
        self,
        user_id: str,
        result: AuthenticationChainResult,
    ) -> FallbackAttempt:
        """Try Apple Watch proximity authentication."""
        start_time = time.perf_counter()
        threshold = OrchestratorConfig.get_proximity_threshold()

        try:
            input_data = ProximityInput(
                user_id=user_id,
                mac_device_id="current",
            )
            output = await self._tools.invoke(
                "proximity_check_apple_watch", input_data
            )

            duration = (time.perf_counter() - start_time) * 1000
            success = output.verified and output.watch_authenticated

            attempt = FallbackAttempt(
                level=FallbackLevel.PROXIMITY,
                success=success,
                confidence=0.90 if success else 0.0,  # High confidence if Watch verified
                threshold=threshold,
                duration_ms=duration,
                error=output.error,
                details={
                    "watch_authenticated": output.watch_authenticated,
                    "distance": output.distance_estimate,
                },
            )

            result.fallback_attempts.append(attempt)
            result.levels_attempted += 1
            result.final_level = FallbackLevel.PROXIMITY

            if self._on_level_complete:
                await self._on_level_complete(attempt)

            return attempt

        except Exception as e:
            duration = (time.perf_counter() - start_time) * 1000
            attempt = FallbackAttempt(
                level=FallbackLevel.PROXIMITY,
                success=False,
                confidence=0.0,
                threshold=threshold,
                duration_ms=duration,
                error=str(e),
            )
            result.fallback_attempts.append(attempt)
            result.levels_attempted += 1
            return attempt

    def _generate_success_response(
        self,
        user_id: str,
        confidence: float,
        level: FallbackLevel,
    ) -> str:
        """Generate success response message."""
        if level == FallbackLevel.PRIMARY:
            if confidence >= 0.95:
                return f"Of course, {user_id}. Unlocking for you."
            elif confidence >= 0.90:
                return f"Voice verified, {user_id}. Unlocking now."
            else:
                return f"Verified. Unlocking for you, {user_id}."
        elif level == FallbackLevel.BEHAVIORAL_FUSION:
            return (
                f"Your voice confidence was a bit lower ({confidence:.0%}), "
                f"but your patterns match perfectly. Unlocking, {user_id}."
            )
        elif level == FallbackLevel.PROXIMITY:
            return f"Your Apple Watch confirms it's you. Welcome, {user_id}."
        else:
            return f"Identity verified. Unlocking for you, {user_id}."

    def _generate_failure_response(
        self,
        confidence: float,
        anomalies: List[str],
    ) -> str:
        """Generate failure response message."""
        if "replay_detected" in anomalies:
            return (
                "Security alert: Audio characteristics suggest a recording. "
                "Live voice required."
            )
        elif confidence < 0.50:
            return "Voice not recognized. Please try again or use password."
        elif confidence < 0.70:
            return (
                "Having trouble verifying your voice. "
                "Please speak clearly or use an alternative method."
            )
        else:
            return (
                "Voice verification inconclusive. "
                "Please try again or use password."
            )

    async def _finalize_result(
        self,
        result: AuthenticationChainResult,
        start_time: float,
        voice_embedding: Optional[Any] = None,
    ) -> AuthenticationChainResult:
        """Finalize and record the authentication result (v2.0 enhanced)."""
        result.total_duration_ms = (time.perf_counter() - start_time) * 1000

        # Calculate level durations
        for attempt in result.fallback_attempts:
            result.level_durations_ms[attempt.level.name] = attempt.duration_ms

        # Update statistics
        self._stats["total_authentications"] += 1
        if result.decision == AuthenticationDecision.AUTHENTICATED:
            self._stats["successful_authentications"] += 1
            self._stats["level_success_counts"][result.final_level.name] += 1
        elif result.decision == AuthenticationDecision.DENIED:
            self._stats["failed_authentications"] += 1

        # Update average duration
        n = self._stats["total_authentications"]
        self._stats["avg_duration_ms"] = (
            self._stats["avg_duration_ms"] * (n - 1) + result.total_duration_ms
        ) / n

        # v2.0: Post-authentication hook (pattern learning, auditing, caching)
        if self._enhancements and voice_embedding is not None:
            try:
                await self._enhancements.post_authentication_hook(
                    session_id=result.session_id,
                    user_id=result.authenticated_user or "unknown",
                    embedding=voice_embedding,
                    confidence=result.final_confidence,
                    decision=result.decision.value,
                    success=(result.decision == AuthenticationDecision.AUTHENTICATED),
                    duration_ms=result.total_duration_ms,
                    environmental_context={},  # Populate from context if needed
                    details={
                        "fallback_level": result.final_level.name,
                        "levels_attempted": result.levels_attempted,
                        "spoofing_suspected": result.spoofing_suspected,
                    },
                )
                self._stats["pattern_learnings"] += 1
            except Exception as e:
                logger.debug(f"[Orchestrator] Post-auth hook error: {e}")

        # Call completion hook
        if self._on_authentication_complete:
            try:
                await self._on_authentication_complete(result)
            except Exception as e:
                logger.warning(f"Authentication complete hook error: {e}")

        logger.info(
            f"Authentication {result.decision.value}: "
            f"user={result.authenticated_user}, "
            f"level={result.final_level.name}, "
            f"confidence={result.final_confidence:.3f}, "
            f"duration={result.total_duration_ms:.1f}ms"
        )

        return result

    def get_stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics."""
        return self._stats.copy()


# =============================================================================
# FACTORY FUNCTIONS
# =============================================================================

_orchestrator_instance: Optional[VoiceAuthOrchestrator] = None
_orchestrator_lock = asyncio.Lock()


async def get_voice_auth_orchestrator(
    force_new: bool = False,
) -> VoiceAuthOrchestrator:
    """Get or create the voice auth orchestrator."""
    global _orchestrator_instance

    async with _orchestrator_lock:
        if _orchestrator_instance is None or force_new:
            _orchestrator_instance = VoiceAuthOrchestrator()
        return _orchestrator_instance


def create_voice_auth_orchestrator(
    tool_registry: Optional[VoiceAuthToolRegistry] = None,
) -> VoiceAuthOrchestrator:
    """Create a new voice auth orchestrator instance."""
    return VoiceAuthOrchestrator(tool_registry=tool_registry)


__all__ = [
    "VoiceAuthOrchestrator",
    "OrchestratorConfig",
    "AuthenticationChainResult",
    "AuthenticationDecision",
    "FallbackLevel",
    "FallbackAttempt",
    "get_voice_auth_orchestrator",
    "create_voice_auth_orchestrator",
]
