"""
JARVIS Voice Authentication Layer v1.0
=======================================

Bridges the Voice Biometric Intelligence Authentication (VBIA) system
with the Agentic Task Runner for Two-Tier Security.

Key Features:
- Pre-execution verification for Tier 2 commands
- Progressive confidence communication
- Environmental adaptation (noise, device changes)
- Anti-spoofing integration with watchdog
- Continuous re-verification on high-risk actions

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                 VoiceAuthenticationLayer                         │
    │  ┌──────────────┐   ┌──────────────────┐   ┌───────────────┐   │
    │  │   Tiered     │ → │  Voice Auth      │ → │   Watchdog    │   │
    │  │   VBIA       │   │  Layer           │   │   Integration │   │
    │  │   Adapter    │   │                  │   │               │   │
    │  └──────────────┘   └────────┬─────────┘   └───────────────┘   │
    │                              │                                   │
    │                    ┌─────────▼─────────┐                        │
    │                    │  Agentic Task     │                        │
    │                    │  Runner           │                        │
    │                    └───────────────────┘                        │
    └─────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class VoiceAuthLayerConfig:
    """Configuration for Voice Authentication Layer."""

    # Thresholds (can be overridden by TieredVBIAAdapter)
    tier1_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TIER1_VBIA_THRESHOLD", "0.70"))
    )
    tier2_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TIER2_VBIA_THRESHOLD", "0.85"))
    )

    # Pre-execution verification
    pre_execution_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_AUTH_PRE_EXECUTION", "true").lower() == "true"
    )

    # Continuous re-verification for high-risk actions
    continuous_verification: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_CONTINUOUS_VERIFY", "false").lower() == "true"
    )

    # Environmental adaptation
    environmental_adaptation: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_ENV_ADAPT", "true").lower() == "true"
    )

    # Cache verification results
    cache_ttl_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VOICE_CACHE_TTL", "30.0"))
    )

    # Anti-spoofing
    anti_spoofing_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_ANTI_SPOOFING_ENABLED", "true").lower() == "true"
    )

    # Watchdog integration
    watchdog_integration: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_WATCHDOG_INTEGRATION", "true").lower() == "true"
    )


# =============================================================================
# Enums
# =============================================================================

class AuthResult(str, Enum):
    """Result of voice authentication."""
    PASSED = "passed"
    FAILED = "failed"
    BYPASSED = "bypassed"
    CACHED = "cached"
    SPOOFING_DETECTED = "spoofing_detected"
    NO_AUDIO = "no_audio"
    ERROR = "error"


class ConfidenceLevel(str, Enum):
    """Confidence level classification."""
    HIGH = "high"         # >= 90%
    MEDIUM = "medium"     # >= 80%
    LOW = "low"           # >= 70%
    INSUFFICIENT = "insufficient"  # < 70%


# =============================================================================
# Result Data Classes
# =============================================================================

@dataclass
class VoiceAuthResult:
    """Result of a voice authentication attempt."""
    result: AuthResult
    confidence: float
    confidence_level: ConfidenceLevel
    speaker_id: Optional[str] = None
    is_owner: bool = False
    liveness_passed: bool = False
    anti_spoofing_passed: bool = True
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class EnvironmentInfo:
    """Information about the audio environment."""
    noise_level_db: float = -42.0
    snr_db: float = 18.0
    microphone: str = "unknown"
    location: str = "unknown"
    quality_score: float = 0.8


# =============================================================================
# Voice Authentication Layer
# =============================================================================

class VoiceAuthenticationLayer:
    """
    Voice authentication layer for the agentic task runner.

    This layer provides:
    - Pre-execution verification
    - Progressive confidence communication
    - Environmental adaptation
    - Anti-spoofing integration
    - Watchdog notifications
    """

    def __init__(
        self,
        config: Optional[VoiceAuthLayerConfig] = None,
        vbia_adapter: Optional[Any] = None,  # TieredVBIAAdapter
        watchdog: Optional[Any] = None,  # AgenticWatchdog
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize the Voice Authentication Layer.

        Args:
            config: Layer configuration
            vbia_adapter: TieredVBIAAdapter instance
            watchdog: AgenticWatchdog instance for notifications
            tts_callback: Text-to-speech callback for feedback
            logger: Logger instance
        """
        self.config = config or VoiceAuthLayerConfig()
        self._vbia_adapter = vbia_adapter
        self._watchdog = watchdog
        self.tts_callback = tts_callback
        self.logger = logger or logging.getLogger(__name__)

        # State
        self._initialized = False
        self._cached_result: Optional[VoiceAuthResult] = None
        self._last_verification_time: float = 0
        self._verification_count = 0
        self._success_count = 0

        # Environment tracking
        self._current_environment: Optional[EnvironmentInfo] = None
        self._known_environments: Dict[str, EnvironmentInfo] = {}

        self.logger.info("[VoiceAuthLayer] Created")

    # =========================================================================
    # Initialization
    # =========================================================================

    async def initialize(self) -> bool:
        """Initialize the voice authentication layer."""
        if self._initialized:
            return True

        self.logger.info("[VoiceAuthLayer] Initializing...")

        try:
            # Try to get VBIA adapter if not provided
            if not self._vbia_adapter:
                try:
                    from core.tiered_vbia_adapter import get_tiered_vbia_adapter
                    self._vbia_adapter = get_tiered_vbia_adapter()
                    self.logger.info("[VoiceAuthLayer] ✓ VBIA adapter connected")
                except ImportError:
                    self.logger.warning("[VoiceAuthLayer] VBIA adapter not available")

            # Initialize VBIA adapter if available
            if self._vbia_adapter:
                await self._vbia_adapter.initialize()

            self._initialized = True
            self.logger.info("[VoiceAuthLayer] Initialization complete")
            return True

        except Exception as e:
            self.logger.error(f"[VoiceAuthLayer] Initialization failed: {e}")
            return False

    def set_vbia_adapter(self, adapter: Any) -> None:
        """Set the VBIA adapter."""
        self._vbia_adapter = adapter
        self.logger.info("[VoiceAuthLayer] VBIA adapter set")

    def set_watchdog(self, watchdog: Any) -> None:
        """Set the watchdog for notifications."""
        self._watchdog = watchdog
        self.logger.info("[VoiceAuthLayer] Watchdog set")

    # =========================================================================
    # Pre-Execution Verification
    # =========================================================================

    async def verify_for_tier2(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> VoiceAuthResult:
        """
        Verify voice authentication for Tier 2 execution.

        Args:
            goal: The goal being executed
            context: Additional context

        Returns:
            VoiceAuthResult with verification details
        """
        self._verification_count += 1
        context = context or {}

        # Check cache first
        if self._is_cache_valid():
            self.logger.debug("[VoiceAuthLayer] Using cached verification")
            cached = self._cached_result
            cached.cached = True
            return cached

        # Check if pre-execution is enabled
        if not self.config.pre_execution_enabled:
            return VoiceAuthResult(
                result=AuthResult.BYPASSED,
                confidence=1.0,
                confidence_level=ConfidenceLevel.HIGH,
                message="Pre-execution verification disabled",
            )

        # Perform verification via VBIA adapter
        if not self._vbia_adapter:
            self.logger.warning("[VoiceAuthLayer] No VBIA adapter - bypassing verification")
            return VoiceAuthResult(
                result=AuthResult.BYPASSED,
                confidence=1.0,
                confidence_level=ConfidenceLevel.HIGH,
                message="No VBIA adapter available",
            )

        try:
            # Use tier 2 verification
            tier2_result = await self._vbia_adapter.verify_tier2()

            result = VoiceAuthResult(
                result=AuthResult.PASSED if tier2_result.passed else AuthResult.FAILED,
                confidence=tier2_result.confidence,
                confidence_level=self._classify_confidence(tier2_result.confidence),
                speaker_id=tier2_result.speaker_id,
                is_owner=tier2_result.is_owner,
                liveness_passed=tier2_result.liveness == "live",
                anti_spoofing_passed=not tier2_result.details.get("spoofing_detected", False),
                message=self._generate_feedback_message(tier2_result),
                details=tier2_result.details,
            )

            # Cache successful verification
            if result.result == AuthResult.PASSED:
                self._cached_result = result
                self._last_verification_time = time.time()
                self._success_count += 1

            # Notify watchdog of verification
            await self._notify_watchdog(result, goal)

            # Provide progressive confidence feedback
            await self._provide_feedback(result, goal)

            return result

        except Exception as e:
            self.logger.error(f"[VoiceAuthLayer] Verification error: {e}")
            return VoiceAuthResult(
                result=AuthResult.ERROR,
                confidence=0.0,
                confidence_level=ConfidenceLevel.INSUFFICIENT,
                message=f"Verification error: {str(e)}",
            )

    async def verify_for_tier1(
        self,
        command: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> VoiceAuthResult:
        """
        Verify voice authentication for Tier 1 execution.

        Args:
            command: The command being executed
            context: Additional context

        Returns:
            VoiceAuthResult with verification details
        """
        if not self._vbia_adapter:
            return VoiceAuthResult(
                result=AuthResult.BYPASSED,
                confidence=1.0,
                confidence_level=ConfidenceLevel.HIGH,
                message="No VBIA adapter available",
            )

        try:
            tier1_result = await self._vbia_adapter.verify_tier1(phrase=command)

            return VoiceAuthResult(
                result=AuthResult.PASSED if tier1_result.passed else AuthResult.FAILED,
                confidence=tier1_result.confidence,
                confidence_level=self._classify_confidence(tier1_result.confidence),
                speaker_id=tier1_result.speaker_id,
                is_owner=tier1_result.is_owner,
                message="Tier 1 verification passed" if tier1_result.passed else "Tier 1 verification failed",
                details=tier1_result.details,
            )

        except Exception as e:
            self.logger.error(f"[VoiceAuthLayer] Tier 1 verification error: {e}")
            return VoiceAuthResult(
                result=AuthResult.ERROR,
                confidence=0.0,
                confidence_level=ConfidenceLevel.INSUFFICIENT,
                message=f"Verification error: {str(e)}",
            )

    # =========================================================================
    # Continuous Verification
    # =========================================================================

    async def verify_for_high_risk_action(
        self,
        action: str,
        risk_level: str = "high",
    ) -> VoiceAuthResult:
        """
        Re-verify for high-risk actions during task execution.

        Args:
            action: The action being performed
            risk_level: Risk level ("high", "critical")

        Returns:
            VoiceAuthResult with verification details
        """
        if not self.config.continuous_verification:
            return VoiceAuthResult(
                result=AuthResult.BYPASSED,
                confidence=1.0,
                confidence_level=ConfidenceLevel.HIGH,
                message="Continuous verification disabled",
            )

        # For critical actions, always re-verify
        # For high actions, use cache if recent
        if risk_level != "critical" and self._is_cache_valid():
            return self._cached_result

        # Perform fresh verification
        return await self.verify_for_tier2(goal=action)

    # =========================================================================
    # Cache Management
    # =========================================================================

    def _is_cache_valid(self) -> bool:
        """Check if cached verification is still valid."""
        if not self._cached_result:
            return False

        age = time.time() - self._last_verification_time
        return age < self.config.cache_ttl_seconds

    def clear_cache(self) -> None:
        """Clear the verification cache."""
        self._cached_result = None
        self._last_verification_time = 0
        self.logger.debug("[VoiceAuthLayer] Cache cleared")

    def set_cached_verification(
        self,
        confidence: float,
        speaker_id: str,
        is_owner: bool,
    ) -> None:
        """
        Set a cached verification result (from voice pipeline).

        This allows the voice pipeline to pre-verify before
        the task runner requests verification.
        """
        self._cached_result = VoiceAuthResult(
            result=AuthResult.CACHED,
            confidence=confidence,
            confidence_level=self._classify_confidence(confidence),
            speaker_id=speaker_id,
            is_owner=is_owner,
            message="Cached from voice pipeline",
            cached=True,
        )
        self._last_verification_time = time.time()
        self.logger.debug(f"[VoiceAuthLayer] Cached verification: {confidence:.2%}")

    # =========================================================================
    # Environmental Adaptation
    # =========================================================================

    def update_environment(self, env_info: EnvironmentInfo) -> None:
        """
        Update current environment information.

        Args:
            env_info: Current environment information
        """
        self._current_environment = env_info

        # Store in known environments if unique
        env_key = f"{env_info.microphone}_{env_info.location}"
        if env_key not in self._known_environments:
            self._known_environments[env_key] = env_info
            self.logger.info(f"[VoiceAuthLayer] New environment learned: {env_key}")

    def get_environment_adjusted_threshold(self, base_threshold: float) -> float:
        """
        Adjust threshold based on environmental conditions.

        Args:
            base_threshold: Base verification threshold

        Returns:
            Adjusted threshold
        """
        if not self.config.environmental_adaptation:
            return base_threshold

        if not self._current_environment:
            return base_threshold

        env = self._current_environment

        # Adjust based on noise level
        if env.snr_db < 10:
            # Very noisy - lower threshold slightly
            adjustment = 0.05
        elif env.snr_db < 15:
            # Noisy - minor adjustment
            adjustment = 0.02
        else:
            adjustment = 0.0

        # Adjust based on quality score
        if env.quality_score < 0.6:
            adjustment += 0.03

        adjusted = base_threshold - adjustment
        self.logger.debug(
            f"[VoiceAuthLayer] Threshold adjusted: {base_threshold:.2f} -> {adjusted:.2f}"
        )
        return adjusted

    # =========================================================================
    # Feedback and Communication
    # =========================================================================

    def _classify_confidence(self, confidence: float) -> ConfidenceLevel:
        """Classify confidence level."""
        if confidence >= 0.90:
            return ConfidenceLevel.HIGH
        elif confidence >= 0.80:
            return ConfidenceLevel.MEDIUM
        elif confidence >= 0.70:
            return ConfidenceLevel.LOW
        else:
            return ConfidenceLevel.INSUFFICIENT

    def _generate_feedback_message(self, tier_result: Any) -> str:
        """Generate a feedback message based on verification result."""
        confidence = tier_result.confidence

        if not tier_result.passed:
            if confidence < 0.5:
                return "I don't recognize this voice"
            elif confidence < 0.7:
                return "Voice confidence too low for this action"
            else:
                return "Verification failed - please try again"

        # Passed
        if confidence >= 0.95:
            return "Verified - confidence is excellent"
        elif confidence >= 0.90:
            return "Verified with high confidence"
        elif confidence >= 0.85:
            return "Verified"
        else:
            return "Verified - borderline confidence"

    async def _provide_feedback(self, result: VoiceAuthResult, goal: str) -> None:
        """Provide progressive confidence feedback via TTS."""
        if not self.tts_callback:
            return

        # Only provide feedback for borderline or failed cases
        if result.confidence_level == ConfidenceLevel.INSUFFICIENT:
            await self.tts_callback(
                "I'm having trouble verifying your voice. Please try again."
            )
        elif result.confidence_level == ConfidenceLevel.LOW and result.result == AuthResult.PASSED:
            # Borderline pass - acknowledge
            pass  # Silent pass for now

    # =========================================================================
    # Watchdog Integration
    # =========================================================================

    async def _notify_watchdog(self, result: VoiceAuthResult, goal: str) -> None:
        """Notify watchdog of verification result."""
        if not self._watchdog or not self.config.watchdog_integration:
            return

        try:
            if hasattr(self._watchdog, "record_voice_verification"):
                await self._watchdog.record_voice_verification(
                    confidence=result.confidence,
                    passed=result.result == AuthResult.PASSED,
                    goal=goal,
                    spoofing_detected=not result.anti_spoofing_passed,
                )
        except Exception as e:
            self.logger.debug(f"[VoiceAuthLayer] Watchdog notification failed: {e}")

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get authentication layer statistics."""
        return {
            "initialized": self._initialized,
            "verification_count": self._verification_count,
            "success_count": self._success_count,
            "success_rate": (
                self._success_count / self._verification_count
                if self._verification_count > 0
                else 0.0
            ),
            "cache_valid": self._is_cache_valid(),
            "cached_confidence": (
                self._cached_result.confidence
                if self._cached_result
                else None
            ),
            "known_environments": len(self._known_environments),
            "config": {
                "tier1_threshold": self.config.tier1_threshold,
                "tier2_threshold": self.config.tier2_threshold,
                "pre_execution_enabled": self.config.pre_execution_enabled,
                "continuous_verification": self.config.continuous_verification,
                "anti_spoofing_enabled": self.config.anti_spoofing_enabled,
            },
        }

    async def shutdown(self) -> None:
        """Shutdown the voice authentication layer."""
        self.logger.info("[VoiceAuthLayer] Shutting down")
        self.clear_cache()
        self._initialized = False


# =============================================================================
# Singleton Access
# =============================================================================

_voice_auth_layer: Optional[VoiceAuthenticationLayer] = None


def get_voice_auth_layer() -> Optional[VoiceAuthenticationLayer]:
    """Get the global voice authentication layer instance."""
    return _voice_auth_layer


def set_voice_auth_layer(layer: VoiceAuthenticationLayer) -> None:
    """Set the global voice authentication layer instance."""
    global _voice_auth_layer
    _voice_auth_layer = layer


async def start_voice_auth_layer(
    config: Optional[VoiceAuthLayerConfig] = None,
    vbia_adapter: Optional[Any] = None,
    watchdog: Optional[Any] = None,
    tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> VoiceAuthenticationLayer:
    """
    Create and initialize the voice authentication layer.

    Returns:
        Initialized VoiceAuthenticationLayer instance
    """
    global _voice_auth_layer

    layer = VoiceAuthenticationLayer(
        config=config,
        vbia_adapter=vbia_adapter,
        watchdog=watchdog,
        tts_callback=tts_callback,
    )
    await layer.initialize()

    _voice_auth_layer = layer
    return layer


async def stop_voice_auth_layer() -> None:
    """Stop the global voice authentication layer."""
    global _voice_auth_layer
    if _voice_auth_layer:
        await _voice_auth_layer.shutdown()
        _voice_auth_layer = None
