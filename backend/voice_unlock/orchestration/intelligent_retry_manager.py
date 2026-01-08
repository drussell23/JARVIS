"""
Intelligent Retry Manager - Adaptive Retry Strategy Based on Failure Cause.
============================================================================

Provides intelligent retry logic that adapts based on WHY authentication
failed, not just that it failed. Enables smarter recovery suggestions.

Features:
1. Failure cause analysis (noise, mic change, voice different, security)
2. Adaptive retry strategies per cause
3. Progressive guidance for users
4. Microphone adaptation learning
5. Environment-specific adjustments
6. Smart timeout handling

Per CLAUDE.md:
    "I'm having trouble hearing you clearly - there's some background
    noise. Could you try again, maybe speak a bit louder and closer
    to the microphone?"

Author: JARVIS Trinity v81.0 - Intelligent Retry Intelligence
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Types and Enums
# =============================================================================

class FailureCause(Enum):
    """Root causes for authentication failure."""
    BACKGROUND_NOISE = "background_noise"
    LOW_AUDIO_QUALITY = "low_audio_quality"
    DIFFERENT_MICROPHONE = "different_microphone"
    VOICE_DIFFERENT = "voice_different"
    VOICE_HEALTH = "voice_health"
    SPEECH_TOO_SHORT = "speech_too_short"
    SPEECH_TOO_QUIET = "speech_too_quiet"
    UNKNOWN_SPEAKER = "unknown_speaker"
    REPLAY_DETECTED = "replay_detected"
    SPOOFING_DETECTED = "spoofing_detected"
    TIMEOUT = "timeout"
    SYSTEM_ERROR = "system_error"
    UNKNOWN = "unknown"


class RetryStrategy(Enum):
    """Strategies for retry attempts."""
    IMMEDIATE_RETRY = "immediate_retry"          # Try again immediately
    GUIDED_RETRY = "guided_retry"                # Retry with specific guidance
    ADJUST_ENVIRONMENT = "adjust_environment"    # Ask user to adjust
    MICROPHONE_CALIBRATION = "microphone_calibration"  # Learn new mic
    ALTERNATIVE_AUTH = "alternative_auth"        # Switch to different method
    SECURITY_LOCKOUT = "security_lockout"        # Block due to security concern
    ESCALATE_TO_MANUAL = "escalate_to_manual"   # Require password


class RetryUrgency(Enum):
    """How urgently retry should proceed."""
    IMMEDIATE = "immediate"      # Retry now
    AFTER_ADJUSTMENT = "after_adjustment"  # Wait for user action
    DELAYED = "delayed"          # Wait a moment
    NOT_RECOMMENDED = "not_recommended"  # Don't retry


@dataclass
class FailureAnalysis:
    """Analysis of an authentication failure."""
    cause: FailureCause
    confidence: float = 0.0  # How confident in this diagnosis
    contributing_factors: List[str] = field(default_factory=list)
    audio_metrics: Dict[str, float] = field(default_factory=dict)
    suggested_adjustments: List[str] = field(default_factory=list)


@dataclass
class RetryDecision:
    """Decision on how to handle retry."""
    should_retry: bool
    strategy: RetryStrategy
    urgency: RetryUrgency
    max_attempts: int = 3
    delay_seconds: float = 0.0
    guidance_text: str = ""
    alternative_method: Optional[str] = None
    adjustments_needed: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "should_retry": self.should_retry,
            "strategy": self.strategy.value,
            "urgency": self.urgency.value,
            "max_attempts": self.max_attempts,
            "delay_seconds": self.delay_seconds,
            "guidance_text": self.guidance_text,
            "alternative_method": self.alternative_method,
            "adjustments_needed": self.adjustments_needed,
        }


@dataclass
class MicrophoneProfile:
    """Profile for a learned microphone."""
    device_id: str
    device_name: str
    frequency_response_adjustment: float = 0.0
    noise_floor: float = 0.0
    gain_adjustment: float = 0.0
    samples_collected: int = 0
    last_updated: float = 0.0


# =============================================================================
# Intelligent Retry Manager
# =============================================================================

class IntelligentRetryManager:
    """
    Manages intelligent retry logic for voice authentication.

    Analyzes WHY authentication failed and provides targeted
    guidance for successful retry.

    Usage:
        manager = IntelligentRetryManager()
        analysis = await manager.analyze_failure(auth_result, audio_data)
        decision = await manager.determine_retry_strategy(analysis, attempt=1)

        print(decision.guidance_text)
        # "I'm having trouble hearing you clearly - there's some background
        #  noise. Could you try again, speak a bit louder and closer to
        #  the microphone?"
    """

    # Thresholds for failure cause detection
    SNR_NOISE_THRESHOLD = 12.0  # dB - below this is noisy
    ENERGY_LOW_THRESHOLD = 0.05
    DURATION_MIN_SECONDS = 0.5
    CONFIDENCE_MISMATCH_THRESHOLD = 0.5

    def __init__(self):
        """Initialize the retry manager."""
        # Learned microphone profiles
        self._microphone_profiles: Dict[str, MicrophoneProfile] = {}

        # Retry state tracking
        self._retry_history: Dict[str, List[FailureAnalysis]] = {}
        self._lock = asyncio.Lock()

        logger.info("[IntelligentRetryManager] Initialized")

    async def analyze_failure(
        self,
        confidence: float,
        audio_data: Optional[bytes] = None,
        audio_metrics: Optional[Dict[str, float]] = None,
        error_message: Optional[str] = None,
        spoofing_flags: Optional[List[str]] = None,
    ) -> FailureAnalysis:
        """
        Analyze why authentication failed.

        Args:
            confidence: Achieved confidence score
            audio_data: Raw audio bytes (optional)
            audio_metrics: Extracted audio metrics (SNR, energy, etc.)
            error_message: Any error message from the system
            spoofing_flags: Any spoofing detection flags

        Returns:
            FailureAnalysis with diagnosed cause
        """
        analysis = FailureAnalysis(cause=FailureCause.UNKNOWN)
        metrics = audio_metrics or {}

        # Check for security-related failures first
        if spoofing_flags:
            if "replay_detected" in spoofing_flags:
                analysis.cause = FailureCause.REPLAY_DETECTED
                analysis.confidence = 0.95
                return analysis
            elif any("spoof" in flag.lower() for flag in spoofing_flags):
                analysis.cause = FailureCause.SPOOFING_DETECTED
                analysis.confidence = 0.90
                return analysis

        # Check for timeout
        if error_message and "timeout" in error_message.lower():
            analysis.cause = FailureCause.TIMEOUT
            analysis.confidence = 0.95
            return analysis

        # Check for system errors
        if error_message and any(x in error_message.lower() for x in ["error", "exception", "failed"]):
            analysis.cause = FailureCause.SYSTEM_ERROR
            analysis.confidence = 0.8
            return analysis

        # Analyze audio quality issues
        snr = metrics.get("snr_db", 15.0)
        energy = metrics.get("energy", 0.1)
        duration = metrics.get("duration_seconds", 1.0)

        # Background noise
        if snr < self.SNR_NOISE_THRESHOLD:
            analysis.cause = FailureCause.BACKGROUND_NOISE
            analysis.confidence = min(0.9, (self.SNR_NOISE_THRESHOLD - snr) / 10)
            analysis.contributing_factors.append(f"SNR: {snr:.1f} dB (noisy environment)")
            analysis.suggested_adjustments.append("Move to a quieter location")
            analysis.suggested_adjustments.append("Speak closer to microphone")

        # Low audio quality / energy
        elif energy < self.ENERGY_LOW_THRESHOLD:
            analysis.cause = FailureCause.SPEECH_TOO_QUIET
            analysis.confidence = 0.8
            analysis.contributing_factors.append(f"Audio energy very low: {energy:.3f}")
            analysis.suggested_adjustments.append("Speak louder")
            analysis.suggested_adjustments.append("Check microphone is not muted")

        # Speech too short
        elif duration < self.DURATION_MIN_SECONDS:
            analysis.cause = FailureCause.SPEECH_TOO_SHORT
            analysis.confidence = 0.85
            analysis.contributing_factors.append(f"Speech duration: {duration:.2f}s")
            analysis.suggested_adjustments.append("Speak a longer phrase")

        # Different microphone (detected via frequency characteristics)
        elif metrics.get("mic_signature_mismatch", False):
            analysis.cause = FailureCause.DIFFERENT_MICROPHONE
            analysis.confidence = 0.75
            analysis.contributing_factors.append("Microphone characteristics different from baseline")
            analysis.suggested_adjustments.append("I'll learn your voice on this microphone")

        # Voice different (low confidence but not quality issue)
        elif confidence < self.CONFIDENCE_MISMATCH_THRESHOLD and snr >= self.SNR_NOISE_THRESHOLD:
            analysis.cause = FailureCause.VOICE_DIFFERENT
            analysis.confidence = 0.7
            analysis.contributing_factors.append(f"Voice match: {confidence:.0%}")
            analysis.suggested_adjustments.append("Speak naturally as you normally would")

        # Unknown speaker (very low confidence)
        elif confidence < 0.3:
            analysis.cause = FailureCause.UNKNOWN_SPEAKER
            analysis.confidence = 0.8
            analysis.contributing_factors.append("Voice does not match registered profile")

        # Default: low audio quality
        else:
            analysis.cause = FailureCause.LOW_AUDIO_QUALITY
            analysis.confidence = 0.5
            analysis.contributing_factors.append("Audio quality below optimal")

        analysis.audio_metrics = metrics
        return analysis

    async def determine_retry_strategy(
        self,
        analysis: FailureAnalysis,
        attempt_number: int = 1,
        max_attempts: int = 3,
        user_id: str = "",
    ) -> RetryDecision:
        """
        Determine the best retry strategy based on failure analysis.

        Args:
            analysis: Failure analysis result
            attempt_number: Current attempt number (1-based)
            max_attempts: Maximum allowed attempts
            user_id: User ID for profile lookup

        Returns:
            RetryDecision with strategy and guidance
        """
        # Security failures - no retry
        if analysis.cause in (FailureCause.REPLAY_DETECTED, FailureCause.SPOOFING_DETECTED):
            return RetryDecision(
                should_retry=False,
                strategy=RetryStrategy.SECURITY_LOCKOUT,
                urgency=RetryUrgency.NOT_RECOMMENDED,
                guidance_text=self._get_security_message(analysis.cause),
            )

        # Unknown speaker - escalate to manual
        if analysis.cause == FailureCause.UNKNOWN_SPEAKER:
            return RetryDecision(
                should_retry=False,
                strategy=RetryStrategy.ESCALATE_TO_MANUAL,
                urgency=RetryUrgency.NOT_RECOMMENDED,
                guidance_text=(
                    "I don't recognize this voice. Please use password "
                    "authentication to unlock."
                ),
            )

        # Too many attempts - escalate
        if attempt_number >= max_attempts:
            return RetryDecision(
                should_retry=False,
                strategy=RetryStrategy.ALTERNATIVE_AUTH,
                urgency=RetryUrgency.AFTER_ADJUSTMENT,
                max_attempts=max_attempts,
                guidance_text=(
                    "I've tried several times but can't verify your voice. "
                    "Would you like to try a security question or use your password?"
                ),
                alternative_method="challenge_question",
            )

        # Background noise - guided retry
        if analysis.cause == FailureCause.BACKGROUND_NOISE:
            return RetryDecision(
                should_retry=True,
                strategy=RetryStrategy.GUIDED_RETRY,
                urgency=RetryUrgency.AFTER_ADJUSTMENT,
                max_attempts=max_attempts - attempt_number + 1,
                delay_seconds=0.5,
                guidance_text=(
                    "I'm having trouble hearing you clearly - there's some "
                    "background noise. Could you try again, maybe speak a bit "
                    "louder and closer to the microphone?"
                ),
                adjustments_needed=["reduce_noise", "move_closer"],
            )

        # Speech too quiet
        if analysis.cause == FailureCause.SPEECH_TOO_QUIET:
            return RetryDecision(
                should_retry=True,
                strategy=RetryStrategy.GUIDED_RETRY,
                urgency=RetryUrgency.IMMEDIATE,
                max_attempts=max_attempts - attempt_number + 1,
                guidance_text=(
                    "I couldn't hear you clearly. Could you speak a bit louder? "
                    "Also check that your microphone isn't muted."
                ),
                adjustments_needed=["speak_louder", "check_mute"],
            )

        # Speech too short
        if analysis.cause == FailureCause.SPEECH_TOO_SHORT:
            return RetryDecision(
                should_retry=True,
                strategy=RetryStrategy.GUIDED_RETRY,
                urgency=RetryUrgency.IMMEDIATE,
                max_attempts=max_attempts - attempt_number + 1,
                guidance_text=(
                    "I need to hear a bit more to verify your voice. "
                    "Please say 'unlock my screen' one more time."
                ),
                adjustments_needed=["longer_phrase"],
            )

        # Different microphone - calibration
        if analysis.cause == FailureCause.DIFFERENT_MICROPHONE:
            return RetryDecision(
                should_retry=True,
                strategy=RetryStrategy.MICROPHONE_CALIBRATION,
                urgency=RetryUrgency.AFTER_ADJUSTMENT,
                max_attempts=2,
                delay_seconds=0.5,
                guidance_text=(
                    "I notice you're using a different microphone than usual. "
                    "Let me recalibrate - say 'unlock my screen' one more time "
                    "while I adjust for this microphone's characteristics."
                ),
                adjustments_needed=["microphone_adaptation"],
            )

        # Voice different (health, stress, etc.)
        if analysis.cause == FailureCause.VOICE_DIFFERENT:
            return RetryDecision(
                should_retry=True,
                strategy=RetryStrategy.GUIDED_RETRY,
                urgency=RetryUrgency.IMMEDIATE,
                max_attempts=max_attempts - attempt_number + 1,
                guidance_text=(
                    "Your voice sounds a bit different today. Could you try "
                    "speaking naturally, as you normally would? Or if you prefer, "
                    "I can verify you through other factors."
                ),
                adjustments_needed=["natural_speech"],
                alternative_method="behavioral_fusion",
            )

        # System error - delayed retry
        if analysis.cause == FailureCause.SYSTEM_ERROR:
            return RetryDecision(
                should_retry=True,
                strategy=RetryStrategy.IMMEDIATE_RETRY,
                urgency=RetryUrgency.DELAYED,
                max_attempts=2,
                delay_seconds=2.0,
                guidance_text=(
                    "I encountered a technical issue. Let me try again in a moment..."
                ),
            )

        # Timeout - immediate retry
        if analysis.cause == FailureCause.TIMEOUT:
            return RetryDecision(
                should_retry=True,
                strategy=RetryStrategy.IMMEDIATE_RETRY,
                urgency=RetryUrgency.IMMEDIATE,
                max_attempts=2,
                guidance_text=(
                    "That took too long. Please try again - speak as soon "
                    "as you hear the prompt."
                ),
            )

        # Default: guided retry
        return RetryDecision(
            should_retry=True,
            strategy=RetryStrategy.GUIDED_RETRY,
            urgency=RetryUrgency.IMMEDIATE,
            max_attempts=max_attempts - attempt_number + 1,
            guidance_text=(
                "I'm having trouble verifying your voice. "
                "Please try again, speaking clearly."
            ),
        )

    def _get_security_message(self, cause: FailureCause) -> str:
        """Get security-related failure message."""
        if cause == FailureCause.REPLAY_DETECTED:
            return (
                "Security alert: I detected characteristics consistent with "
                "a voice recording rather than a live person. Access denied. "
                "If you're the legitimate user, please speak live to the microphone."
            )
        elif cause == FailureCause.SPOOFING_DETECTED:
            return (
                "Security alert: Unusual audio characteristics detected. "
                "This attempt has been logged. Please use manual authentication."
            )
        return "Security verification failed."

    async def adapt_for_new_microphone(
        self,
        audio_data: bytes,
        user_id: str,
        device_id: str,
        device_name: str = "Unknown",
    ) -> MicrophoneProfile:
        """
        Adapt voice model for a new microphone.

        Args:
            audio_data: Audio sample from new microphone
            user_id: User ID
            device_id: Device identifier
            device_name: Human-readable device name

        Returns:
            MicrophoneProfile with learned characteristics
        """
        async with self._lock:
            profile = self._microphone_profiles.get(device_id)

            if profile is None:
                profile = MicrophoneProfile(
                    device_id=device_id,
                    device_name=device_name,
                )
                self._microphone_profiles[device_id] = profile

            # Analyze audio characteristics (simplified)
            # In production, this would do proper frequency response analysis
            audio_array = self._bytes_to_array(audio_data)
            energy = float(sum(x ** 2 for x in audio_array) / len(audio_array)) ** 0.5

            # Update profile with exponential moving average
            alpha = 0.3 if profile.samples_collected < 3 else 0.1
            profile.noise_floor = (1 - alpha) * profile.noise_floor + alpha * (energy * 0.1)
            profile.samples_collected += 1
            profile.last_updated = time.time()

            logger.info(
                f"[IntelligentRetryManager] Updated microphone profile: "
                f"{device_name} (samples: {profile.samples_collected})"
            )

            return profile

    def _bytes_to_array(self, audio_data: bytes) -> List[float]:
        """Convert audio bytes to float array."""
        try:
            import numpy as np
            arr = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            return arr.tolist()
        except Exception:
            return [0.0] * 1000

    def get_progressive_guidance(
        self,
        attempt_number: int,
        failure_analyses: List[FailureAnalysis],
    ) -> str:
        """
        Generate progressive guidance based on multiple failed attempts.

        Args:
            attempt_number: Current attempt number
            failure_analyses: History of failure analyses

        Returns:
            Progressive guidance text
        """
        if not failure_analyses:
            return "Please try again."

        if attempt_number == 1:
            # First retry - specific guidance
            analysis = failure_analyses[-1]
            return self._get_first_retry_guidance(analysis)

        elif attempt_number == 2:
            # Second retry - more detailed
            analysis = failure_analyses[-1]
            return self._get_second_retry_guidance(analysis, failure_analyses)

        else:
            # Third+ retry - offer alternatives
            return self._get_final_retry_guidance(failure_analyses)

    def _get_first_retry_guidance(self, analysis: FailureAnalysis) -> str:
        """Get guidance for first retry."""
        if analysis.cause == FailureCause.BACKGROUND_NOISE:
            return (
                "Almost got it - could you say that one more time? "
                "Maybe a bit louder?"
            )
        elif analysis.cause == FailureCause.SPEECH_TOO_QUIET:
            return "I couldn't hear you well. Could you speak up a bit?"
        elif analysis.cause == FailureCause.VOICE_DIFFERENT:
            return "Your voice sounds a bit different. Try speaking naturally."

        return "Almost got it - could you try one more time?"

    def _get_second_retry_guidance(
        self,
        analysis: FailureAnalysis,
        history: List[FailureAnalysis],
    ) -> str:
        """Get guidance for second retry."""
        # Check for pattern in failures
        noise_count = sum(1 for a in history if a.cause == FailureCause.BACKGROUND_NOISE)

        if noise_count >= 2:
            return (
                "Still having trouble with background noise. Let me adjust... "
                "[Applying aggressive noise filtering] "
                "Try once more, speak right into the microphone."
            )

        if analysis.cause == FailureCause.VOICE_DIFFERENT:
            return (
                "I'm still having trouble matching your voice. This could be "
                "the microphone or your voice sounding different today. "
                "Would you like to try again or use password instead?"
            )

        return (
            "I'm still having trouble verifying. Let's try one more time - "
            "speak clearly right into the microphone."
        )

    def _get_final_retry_guidance(self, history: List[FailureAnalysis]) -> str:
        """Get guidance when retries are exhausted."""
        return (
            "I've tried several times but can't verify your voice today. "
            "No worries though, let's fix it:\n\n"
            "1. Quick fix: Use your password this time, and I'll "
            "re-learn your voice from this session\n"
            "2. Alternative: Answer a security question\n\n"
            "What would you prefer?"
        )


# =============================================================================
# Singleton Access
# =============================================================================

_manager_instance: Optional[IntelligentRetryManager] = None
_manager_lock = asyncio.Lock()


async def get_intelligent_retry_manager() -> IntelligentRetryManager:
    """Get the singleton retry manager."""
    global _manager_instance

    async with _manager_lock:
        if _manager_instance is None:
            _manager_instance = IntelligentRetryManager()
        return _manager_instance


async def analyze_and_decide_retry(
    confidence: float,
    audio_metrics: Optional[Dict[str, float]] = None,
    attempt_number: int = 1,
    **kwargs,
) -> Tuple[FailureAnalysis, RetryDecision]:
    """Convenience function to analyze failure and get retry decision."""
    manager = await get_intelligent_retry_manager()
    analysis = await manager.analyze_failure(
        confidence=confidence,
        audio_metrics=audio_metrics,
        **kwargs,
    )
    decision = await manager.determine_retry_strategy(
        analysis=analysis,
        attempt_number=attempt_number,
    )
    return analysis, decision
